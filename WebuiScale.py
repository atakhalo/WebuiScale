#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
WebUI 批量图片放大工具
- 图形界面（基于 tkinter）
- 支持单张图片和文件夹批量处理（含递归）
- 支持复制所有元数据（EXIF、PNG tEXt 等）
"""

import os
import sys
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
from pathlib import Path
import traceback

import webuiapi
from PIL import Image
import piexif


# ==================== 元数据处理 ====================

def extract_metadata(image_path):
    """提取图片元数据（EXIF + PNG tEXt 块）"""
    metadata = {}
    ext = os.path.splitext(image_path)[1].lower()

    try:
        img = Image.open(image_path)

        # PNG: 提取 tEXt 块信息（如 WebUI 的 parameters、ComfyUI 的 prompt/workflow）
        if ext == '.png':
            png_info = {}
            for key, value in img.info.items():
                if key not in ('dpi', 'gamma', 'icc_profile', 'chromaticity', 'exif'):
                    # Pillow 要求 tEXt 的值必须为字符串
                    if isinstance(value, bytes):
                        try:
                            value = value.decode('utf-8', errors='replace')
                        except Exception:
                            continue
                    png_info[key] = value
            if png_info:
                metadata['png_info'] = png_info

        # EXIF（适用于 PNG/JPEG/WebP）
        if 'exif' in img.info and img.info['exif']:
            metadata['exif'] = img.info['exif']

        img.close()
    except Exception as e:
        print(f"   ⚠ 读取元数据警告: {e}")

    # 对非 PNG 格式，额外用 piexif 提取 EXIF（更可靠）
    if ext != '.png' and 'exif' not in metadata:
        try:
            exif_dict = piexif.load(image_path)
            if any(v for k, v in exif_dict.items() if k != 'thumbnail' and v):
                metadata['exif'] = piexif.dump(exif_dict)
        except Exception:
            pass

    return metadata


def save_with_metadata(image, output_path, metadata, save_mode='lossy', quality=90, webp_method=6):
    """保存图片并嵌入元数据"""
    ext = os.path.splitext(output_path)[1].lower()
    is_lossless = (save_mode == 'lossless')

    if ext == '.png':
        png_info = metadata.get('png_info', {}).copy()
        if 'exif' in metadata:
            exif_data = metadata['exif']
            if isinstance(exif_data, bytes):
                png_info['exif'] = exif_data
        image.save(
            output_path, format='PNG',
            pnginfo=png_info if png_info else None,
            optimize=is_lossless,
        )

    elif ext == '.webp':
        exif = metadata.get('exif')
        # 传给 Save 的 exif 不能为 None，否则 Pillow 报错
        exif_bytes = exif if exif else piexif.dump({})
        if is_lossless:
            image.save(output_path, format='WEBP', lossless=True,
                       method=webp_method, exif=exif_bytes)
        else:
            image.save(output_path, format='WEBP', quality=quality,
                       method=webp_method, exif=exif_bytes)

    elif ext in ('.jpg', '.jpeg'):
        exif = metadata.get('exif')
        kwargs = {'format': 'JPEG'}
        if is_lossless:
            # JPEG 不支持真正无损，使用最高质量 100 + 优化
            kwargs.update({'quality': 100, 'optimize': True, 'subsampling': 0})
        else:
            kwargs.update({'quality': quality, 'optimize': True})
        if exif:
            kwargs['exif'] = exif
        image.save(output_path, **kwargs)
    else:
        image.save(output_path)


# ==================== 图片扫描 ====================

SUPPORTED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.tif'}


def scan_images(folder_path, recursive=True):
    """扫描文件夹中的图片文件"""
    images = []
    folder = Path(folder_path)

    if recursive:
        for f in folder.rglob('*'):
            if f.suffix.lower() in SUPPORTED_EXTENSIONS and f.is_file():
                images.append(str(f))
    else:
        for f in folder.iterdir():
            if f.suffix.lower() in SUPPORTED_EXTENSIONS and f.is_file():
                images.append(str(f))

    return sorted(images)


# ==================== 放大处理（工作线程） ====================

class UpscaleWorker:
    """放大处理工作器，在新线程中运行"""

    def __init__(self, log_callback, progress_callback, finish_callback):
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.finish_callback = finish_callback
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self, config):
        """执行放大任务"""
        self._cancel = False
        try:
            self._do_run(config)
        except Exception as e:
            self.log_callback(f"❌ 处理过程中发生错误: {str(e)}")
            self.log_callback(traceback.format_exc())
        finally:
            self.finish_callback()

    def _do_run(self, config):
        api = webuiapi.WebUIApi(host=config.host, port=config.port)
        self.log_callback(f"🔗 已连接到 WebUI: {config.host}:{config.port}")

        # 日志输出质量设置
        mode_label = '无损' if config.save_mode == 'lossless' else '有损'
        self.log_callback(f"💾 保存模式: {mode_label}"
                          f"{f', 质量: {config.quality}%' if config.save_mode == 'lossy' else ''}"
                          f", WebP method={config.webp_method}")

        # 收集所有待处理图片
        tasks = []
        source_path = config.source_path

        if config.mode == 'file':
            if not os.path.isfile(source_path):
                self.log_callback(f"❌ 文件不存在: {source_path}")
                return
            tasks.append(source_path)
        else:
            if not os.path.isdir(source_path):
                self.log_callback(f"❌ 文件夹不存在: {source_path}")
                return
            self.log_callback(f"📂 正在扫描文件夹: {source_path}")
            tasks = scan_images(source_path, config.recursive)
            if not tasks:
                self.log_callback("❌ 未找到支持的图片文件")
                return
            self.log_callback(f"📂 共找到 {len(tasks)} 张图片")

        total = len(tasks)
        for idx, image_path in enumerate(tasks):
            if self._cancel:
                self.log_callback("⏹ 已取消")
                return

            self.progress_callback(idx, total)

            # 计算相对路径（用于输出目录结构）
            if config.mode == 'file' or not config.recursive:
                rel_path = os.path.basename(image_path)
            else:
                rel_path = os.path.relpath(image_path, source_path)

            # 确定输出路径
            out_path = os.path.join(config.output_dir, rel_path)

            # 创建输出目录
            os.makedirs(os.path.dirname(out_path), exist_ok=True)

            self.log_callback(f"\n[{idx + 1}/{total}] {rel_path}")

            try:
                # 提取元数据
                metadata = {}
                if config.preserve_metadata:
                    metadata = extract_metadata(image_path)
                    if metadata:
                        self.log_callback(f"   📋 已提取元数据")

                # 读取原图
                original_image = Image.open(image_path)
                self.log_callback(f"   原始尺寸: {original_image.width} x {original_image.height}")

                # 确定目标尺寸
                width = config.target_width
                height = config.target_height

                if width == 0 and height == 0:
                    # 按缩放系数放大
                    scale = config.scale_factor
                    width = int(original_image.width * scale)
                    height = int(original_image.height * scale)
                    resize_mode = 0  # 按缩放系数
                    self.log_callback(f"   放大系数: {scale}x → {width} x {height}")
                else:
                    resize_mode = 1  # 按目标尺寸
                    self.log_callback(f"   目标尺寸: {width} x {height}")

                # 调用 WebUI API 放大
                result = api.extra_single_image(
                    image=original_image,
                    upscaler_1=config.upscaler_name,
                    resize_mode=resize_mode,
                    upscaling_resize_w=width,
                    upscaling_resize_h=height,
                )

                upscaled_image = result.image
                self.log_callback(f"   放大后尺寸: {upscaled_image.width} x {upscaled_image.height}")

                # 保存图片（含元数据 + 质量设置）
                if config.preserve_metadata:
                    save_with_metadata(upscaled_image, out_path, metadata,
                                       save_mode=config.save_mode, quality=config.quality,
                                       webp_method=config.webp_method)
                else:
                    save_with_metadata(upscaled_image, out_path, {},
                                       save_mode=config.save_mode, quality=config.quality,
                                       webp_method=config.webp_method)

                self.log_callback(f"   ✅ 已保存 → {out_path}")

            except Exception as e:
                self.log_callback(f"   ❌ 处理失败: {str(e)}")
                if config.debug:
                    self.log_callback(traceback.format_exc())

        if not self._cancel:
            self.progress_callback(total, total)
            self.log_callback(f"\n{'=' * 40}")
            self.log_callback(f"🎉 全部处理完成！共处理 {total} 张图片")


# ==================== 配置 ====================

class Config:
    """处理配置"""
    def __init__(self):
        self.mode = 'file'             # 'file' | 'folder'
        self.source_path = ''
        self.output_dir = ''
        self.target_width = 0
        self.target_height = 0
        self.scale_factor = 2.0
        self.upscaler_name = '4x-AnimeSharp'
        self.host = '127.0.0.1'
        self.port = 7860
        self.recursive = True
        self.preserve_metadata = True
        self.debug = False
        self.save_mode = 'lossy'       # 'lossy' | 'lossless'
        self.quality = 90              # 1-100, lossy 时有效
        self.webp_method = 6           # 0-6, WebP 压缩方法（6=最慢但最小）


# ==================== GUI ====================

class UpscaleGUI:
    """主界面"""

    def __init__(self, root):
        self.root = root
        self.root.title("WebUI 图片放大工具")
        self.root.geometry("780x720")
        self.root.minsize(680, 620)

        self.config = Config()
        self.worker = None
        self.worker_thread = None
        self.log_queue = queue.Queue()

        self._build_ui()
        self._poll_log_queue()
        # 初始化质量控件的状态
        self._on_quality_mode_change()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 界面构建 ----------

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use('vista' if 'vista' in style.theme_names() else 'clam')

        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # ===== 输入源 =====
        source_frame = ttk.LabelFrame(main_frame, text="输入源", padding="10")
        source_frame.pack(fill=tk.X, pady=(0, 5))

        self.mode_var = tk.StringVar(value='file')
        ttk.Radiobutton(source_frame, text="单张图片", variable=self.mode_var,
                        value='file', command=self._on_mode_change).grid(row=0, column=0, sticky=tk.W, padx=(0, 15))
        ttk.Radiobutton(source_frame, text="文件夹", variable=self.mode_var,
                        value='folder', command=self._on_mode_change).grid(row=0, column=1, sticky=tk.W)

        ttk.Label(source_frame, text="路径:").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        self.source_path_var = tk.StringVar()
        ttk.Entry(source_frame, textvariable=self.source_path_var, width=60).grid(
            row=1, column=1, sticky=tk.EW, pady=(8, 0), padx=(5, 5))
        ttk.Button(source_frame, text="浏览...", command=self._browse_source).grid(row=1, column=2, pady=(8, 0))

        self.recursive_var = tk.BooleanVar(value=True)
        self.recursive_cb = ttk.Checkbutton(source_frame, text="递归扫描子目录", variable=self.recursive_var)

        source_frame.columnconfigure(1, weight=1)
        # 初始时如果是文件模式则隐藏递归复选框
        self._on_mode_change()

        # ===== 输出设置 =====
        output_frame = ttk.LabelFrame(main_frame, text="输出设置", padding="10")
        output_frame.pack(fill=tk.X, pady=5)

        ttk.Label(output_frame, text="输出目录:").grid(row=0, column=0, sticky=tk.W)
        self.output_dir_var = tk.StringVar(value=os.path.join(os.getcwd(), "output"))
        ttk.Entry(output_frame, textvariable=self.output_dir_var, width=60).grid(
            row=0, column=1, sticky=tk.EW, padx=(5, 5))
        ttk.Button(output_frame, text="浏览...", command=self._browse_output).grid(row=0, column=2)

        self.preserve_meta_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(output_frame, text="保留原图元数据（EXIF、PNG tEXt 信息等）",
                        variable=self.preserve_meta_var).grid(row=1, column=1, sticky=tk.W, pady=(5, 0))

        output_frame.columnconfigure(1, weight=1)

        # ===== 保存质量 =====
        quality_frame = ttk.LabelFrame(main_frame, text="保存质量", padding="10")
        quality_frame.pack(fill=tk.X, pady=5)

        self.save_mode_var = tk.StringVar(value='lossy')
        ttk.Radiobutton(quality_frame, text="有损", variable=self.save_mode_var,
                        value='lossy', command=self._on_quality_mode_change).grid(row=0, column=0, sticky=tk.W)
        ttk.Radiobutton(quality_frame, text="无损", variable=self.save_mode_var,
                        value='lossless', command=self._on_quality_mode_change).grid(row=0, column=1, sticky=tk.W, padx=(15, 0))

        ttk.Label(quality_frame, text="质量:").grid(row=0, column=2, sticky=tk.W, padx=(20, 5))
        self.quality_var = tk.IntVar(value=90)
        self.quality_scale = ttk.Scale(quality_frame, from_=1, to=100, variable=self.quality_var,
                                       orient=tk.HORIZONTAL, length=200,
                                       command=self._on_quality_scale)
        self.quality_scale.grid(row=0, column=3, sticky=tk.W)

        self.quality_label = ttk.Label(quality_frame, text="90%", width=5)
        self.quality_label.grid(row=0, column=4, sticky=tk.W, padx=(5, 0))

        ttk.Label(quality_frame, text="（仅对有损模式有效）").grid(row=0, column=5, sticky=tk.W, padx=(5, 0))

        # 格式说明
        hint = ("• PNG: 有损=标准无损; 无损=无损压缩 "
                "• WebP: 有损=质量; 无损=无损 "
                "• JPEG: 有损=质量; 无损=质量100+颜色保持")
        ttk.Label(quality_frame, text=hint, foreground="#999",
                  font=('', 8)).grid(row=1, column=0, columnspan=7, sticky=tk.W, pady=(5, 0))

        # WebP 压缩方法
        row3 = ttk.Frame(quality_frame)
        row3.grid(row=2, column=0, columnspan=7, sticky=tk.W, pady=(5, 0))

        ttk.Label(row3, text="  压缩方法:").pack(side=tk.LEFT, padx=(16, 4))
        self.webp_method_var = tk.StringVar(value="6")
        method_combo = ttk.Combobox(row3, textvariable=self.webp_method_var, width=8,
                                    values=list(range(7)), state="readonly")
        method_combo.pack(side=tk.LEFT)
        ttk.Label(row3, text="(0=最快, 6=压缩率最高)", foreground="gray").pack(side=tk.LEFT, padx=4)

        quality_frame.columnconfigure(6, weight=1)

        # ===== 放大参数 =====
        param_frame = ttk.LabelFrame(main_frame, text="放大参数", padding="10")
        param_frame.pack(fill=tk.X, pady=5)

        # 第一行: 目标尺寸 + 交换 + 提示
        ttk.Label(param_frame, text="目标宽度:").grid(row=0, column=0, sticky=tk.W)
        self.width_var = tk.StringVar(value="0")
        ttk.Entry(param_frame, textvariable=self.width_var, width=8).grid(row=0, column=1, sticky=tk.W, padx=(5, 3))

        # ttk.Label(param_frame, text="×").grid(row=0, column=2, sticky=tk.W)

        ttk.Label(param_frame, text="目标高度:").grid(row=0, column=2, sticky=tk.W)
        self.height_var = tk.StringVar(value="0")
        ttk.Entry(param_frame, textvariable=self.height_var, width=8).grid(row=0, column=3, sticky=tk.W, padx=(5, 5))

        ttk.Button(param_frame, text="↔ 交换", width=7,
                   command=self._swap_wh).grid(row=1, column=0, sticky=tk.W, padx=(0, 10))

        ttk.Label(param_frame, text="（宽高填 0 则使用下方缩放系数）",
                  foreground="#888").grid(row=0, column=4, sticky=tk.W)

        # 第二行: 预设按钮
        presets = [("2560×1440", 2560, 1440), ("1920×1080", 1920, 1080), ("3840×2160", 3840, 2160)]
        for i, (label, w, h) in enumerate(presets):
            btn = ttk.Button(param_frame, text=label, width=12,
                             command=lambda w=w, h=h: self._set_preset(w, h))
            btn.grid(row=1, column=i+1, sticky=tk.W, padx=(0, 5), pady=(0, 3))

        # 第三行: 缩放系数 + 算法
        ttk.Label(param_frame, text="缩放系数:").grid(row=2, column=0, sticky=tk.W, pady=(3, 0))
        self.scale_var = tk.StringVar(value="2.0")
        ttk.Entry(param_frame, textvariable=self.scale_var, width=8).grid(row=2, column=1, sticky=tk.W, padx=(5, 15), pady=(3, 0))

        ttk.Label(param_frame, text="放大算法:").grid(row=2, column=2, sticky=tk.W, pady=(3, 0))
        self.upscaler_var = tk.StringVar(value="4x-UltraSharp")
        upscalers = ["4x-AnimeSharp", "4x-UltraSharp", "R-ESRGAN 4x+", "R-ESRGAN 4x Anime6B",
                     "SwinIR 4x", "ESRGAN_4x", "LDSR", "ScuNET", "None"]
        ttk.Combobox(param_frame, textvariable=self.upscaler_var, values=upscalers, width=22).grid(
            row=2, column=3, sticky=tk.W, padx=(5, 5), pady=(3, 0))

        ttk.Label(param_frame, text="⚠ 请自主检查webui是否支持", foreground="#888").grid(
            row=2, column=4, sticky=tk.W, padx=(2, 0), pady=(3, 0))

        param_frame.columnconfigure(4, weight=1)

        # ===== WebUI 连接 =====
        conn_frame = ttk.LabelFrame(main_frame, text="WebUI 连接", padding="10")
        conn_frame.pack(fill=tk.X, pady=5)

        ttk.Label(conn_frame, text="地址:").grid(row=0, column=0, sticky=tk.W)
        self.host_var = tk.StringVar(value="127.0.0.1")
        ttk.Entry(conn_frame, textvariable=self.host_var, width=15).grid(row=0, column=1, sticky=tk.W, padx=(5, 15))

        ttk.Label(conn_frame, text="端口:").grid(row=0, column=2, sticky=tk.W)
        self.port_var = tk.StringVar(value="7860")
        ttk.Entry(conn_frame, textvariable=self.port_var, width=8).grid(row=0, column=3, sticky=tk.W, padx=(5, 5))

        self.debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(conn_frame, text="调试模式（显示详细错误）", variable=self.debug_var).grid(
            row=0, column=4, sticky=tk.W, padx=(15, 0))

        conn_frame.columnconfigure(4, weight=1)

        # ===== 操作按钮 + 进度 =====
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=10)

        self.start_btn = ttk.Button(btn_frame, text="▶ 开始处理", command=self._start_upscale, width=14)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.cancel_btn = ttk.Button(btn_frame, text="⏹ 取消", command=self._cancel_upscale,
                                     state=tk.DISABLED, width=10)
        self.cancel_btn.pack(side=tk.LEFT)

        # 进度条
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(btn_frame, variable=self.progress_var, maximum=100, length=200)
        self.progress_bar.pack(side=tk.RIGHT, padx=(10, 0))

        self.progress_label = ttk.Label(btn_frame, text="")
        self.progress_label.pack(side=tk.RIGHT, padx=(5, 0))

        # ===== 日志输出 =====
        log_frame = ttk.LabelFrame(main_frame, text="处理日志", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(5, 0))

        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, height=15,
                                                  font=('Consolas', 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ---------- 事件处理 ----------

    def _on_mode_change(self):
        """切换文件/文件夹模式时显示/隐藏递归选项"""
        is_folder = self.mode_var.get() == 'folder'
        if is_folder:
            self.recursive_cb.grid(row=2, column=1, sticky=tk.W, pady=(3, 0))
        else:
            self.recursive_cb.grid_remove()

    def _on_quality_mode_change(self):
        """切换有损/无损时启用/禁用质量滑块"""
        is_lossy = self.save_mode_var.get() == 'lossy'
        state = tk.NORMAL if is_lossy else tk.DISABLED
        self.quality_scale.config(state=state)

    def _on_quality_scale(self, value):
        """质量滑块拖动时更新百分比标签"""
        val = int(float(value))
        self.quality_label.config(text=f"{val}%")

    def _set_preset(self, w, h):
        """设置预设分辨率"""
        self.width_var.set(str(w))
        self.height_var.set(str(h))

    def _swap_wh(self):
        """交换宽高值"""
        w = self.width_var.get()
        h = self.height_var.get()
        self.width_var.set(h)
        self.height_var.set(w)

    def _browse_source(self):
        """浏览输入路径"""
        is_folder = self.mode_var.get() == 'folder'
        if is_folder:
            path = filedialog.askdirectory(title="选择图片文件夹")
        else:
            path = filedialog.askopenfilename(
                title="选择图片",
                filetypes=[("图片文件", "*.png *.jpg *.jpeg *.webp *.bmp *.tiff"), ("所有文件", "*.*")]
            )
        if path:
            self.source_path_var.set(path)

    def _browse_output(self):
        """浏览输出目录"""
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_dir_var.set(path)

    # ---------- 日志与进度（线程安全） ----------

    def _log(self, message):
        """添加日志（可由工作线程调用）"""
        self.log_queue.put(message)

    def _poll_log_queue(self):
        """轮询日志队列并在主线程更新 GUI"""
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_text.insert(tk.END, message + '\n')
                self.log_text.see(tk.END)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    def _update_progress(self, current, total):
        """更新进度（可由工作线程调用）"""
        self.root.after(0, lambda: self._do_update_progress(current, total))

    def _do_update_progress(self, current, total):
        if total > 0:
            pct = (current / total) * 100
            self.progress_var.set(pct)
            self.progress_label.config(text=f"{current}/{total}")

    def _on_finish(self):
        """处理完成回调（可由工作线程调用）"""
        self.root.after(0, self._do_finish)

    def _do_finish(self):
        self.start_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)
        self.worker = None
        self.worker_thread = None

    # ---------- 启动 / 取消 ----------

    def _start_upscale(self):
        """开始处理"""
        # 读取配置
        self.config.mode = self.mode_var.get()
        self.config.source_path = self.source_path_var.get().strip()
        self.config.output_dir = self.output_dir_var.get().strip()
        self.config.recursive = self.recursive_var.get()
        self.config.preserve_metadata = self.preserve_meta_var.get()
        self.config.debug = self.debug_var.get()
        self.config.save_mode = self.save_mode_var.get()
        self.config.quality = self.quality_var.get()
        self.config.webp_method = int(self.webp_method_var.get())

        # 解析尺寸
        try:
            self.config.target_width = int(self.width_var.get())
            self.config.target_height = int(self.height_var.get())
        except ValueError:
            self._log("❌ 目标宽度/高度必须为整数")
            return

        # 解析缩放系数
        try:
            self.config.scale_factor = float(self.scale_var.get())
        except ValueError:
            self._log("❌ 缩放系数必须为数字")
            return

        self.config.upscaler_name = self.upscaler_var.get()
        self.config.host = self.host_var.get().strip()

        try:
            self.config.port = int(self.port_var.get())
        except ValueError:
            self._log("❌ 端口必须为整数")
            return

        # 验证输入
        if not self.config.source_path:
            self._log("❌ 请选择输入路径")
            return
        if not self.config.output_dir:
            self._log("❌ 请设置输出目录")
            return

        if not os.path.exists(self.config.source_path):
            self._log(f"❌ 输入路径不存在: {self.config.source_path}")
            return

        # 创建输出目录
        try:
            os.makedirs(self.config.output_dir, exist_ok=True)
        except Exception as e:
            self._log(f"❌ 无法创建输出目录: {e}")
            return

        # 启动工作线程
        self.worker = UpscaleWorker(
            log_callback=self._log,
            progress_callback=self._update_progress,
            finish_callback=self._on_finish,
        )

        self.worker_thread = threading.Thread(
            target=self.worker.run, args=(self.config,), daemon=True
        )

        self.start_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.progress_var.set(0)
        self.progress_label.config(text="0/0")
        self.log_text.delete(1.0, tk.END)

        self._log("=" * 40)
        self._log("🚀 开始处理...")

        self.worker_thread.start()

    def _cancel_upscale(self):
        """取消处理"""
        if self.worker:
            self.worker.cancel()
            self._log("⏹ 正在取消...")

    def _on_close(self):
        """关闭窗口"""
        if self.worker:
            self.worker.cancel()
        self.root.destroy()


# ==================== 入口 ====================

def main():
    root = tk.Tk()
    app = UpscaleGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
