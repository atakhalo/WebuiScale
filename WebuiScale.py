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
import json
import time
import threading
import queue
import ctypes
from ctypes import wintypes
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


# ==================== 任务构建与进度持久化 ====================

def build_tasks(sources, output_dir, recursive=True):
    """将源(文件或文件夹列表)展开为任务列表。

    每个任务: (rel_path, source_path, out_path)。
    - 单个文件: 直接输出到输出目录
    - 文件夹: 源文件夹名作为输出目录的一级文件夹名, 内部保留相对结构
    """
    tasks = []
    for source in sources:
        if os.path.isfile(source):
            rel_path = os.path.basename(source)
            tasks.append((rel_path, source, os.path.join(output_dir, rel_path)))
        elif os.path.isdir(source):
            folder_name = os.path.basename(os.path.normpath(source))
            base = os.path.join(output_dir, folder_name)
            for f in scan_images(source, recursive):
                rel = os.path.relpath(f, source)
                tasks.append((os.path.join(folder_name, rel), f, os.path.join(base, rel)))
    return tasks


def save_progress(filepath, progress_data):
    """保存进度数据到 JSON 文件"""
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(progress_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠ 保存进度失败: {e}")


def load_progress(filepath):
    """读取进度 JSON, 不存在或损坏时返回 None"""
    try:
        if os.path.isfile(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return None


def validate_resume_state(tasks, done, saved_total, saved_next):
    """校验上次进度是否仍然有效。

    返回 (valid, reason)：
    - valid=True 且 done<len(tasks): 从 done 处继续
    - valid=True 且 done==len(tasks): 已全部完成
    - valid=False: 忽略上次进度，从头开始
    """
    total = len(tasks)
    if done <= 0:
        return True, ''
    if saved_total != total:
        return False, f'任务总数不一致({saved_total}→{total})'
    if done > total:
        return False, f'已完成数({done})超过总数({total})'
    if done == total:
        return True, '上次已全部完成'
    if not saved_next:
        return False, '缺少下一张路径'
    expected = tasks[done][1]
    if os.path.normcase(os.path.abspath(expected)) != os.path.normcase(os.path.abspath(saved_next)):
        return False, '下一张路径不匹配'
    return True, ''


class StateStore:
    """进度/参数持久化。任何解析后的状态(来源、输出、已完成数量、会话ID)随进度一起保存。

    一个会话 ID 对应一次运行(某次点击开始处理)。会话内完成的图片数作为进度增量保存。
    未完成的运行(会话状态 active)在下次启动时被恢复。
    """

    FILE = 'data-user.json'

    def __init__(self, work_dir=None):
        self.work_dir = work_dir or os.path.dirname(os.path.abspath(__file__))
        self.path = os.path.join(self.work_dir, self.FILE)

    def load(self):
        return load_progress(self.path)

    def save(self, data):
        save_progress(self.path, data)


# ==================== 图片扫描 ====================

SUPPORTED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.tif'}


def _fmt_seconds(seconds):
    """将秒数格式化为 mm:ss / h:mm:ss"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


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
    """放大处理工作器，在新线程中运行。

    支持：
    - 暂停：当前张处理完成后停止，等待『继续』
    - 进度持久化：每处理完一张将会话完成数写入 data-user.json
    - 断点恢复：从上次未完成会话恢复（跳过已完成图片）
    """

    def __init__(self, log_callback, progress_callback, finish_callback, state_callback=None):
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.finish_callback = finish_callback
        self.state_callback = state_callback
        self._cancel = False
        self._pause = False
        self._pause_cond = threading.Condition()
        self._pause_requested = False

    def cancel(self):
        self._cancel = True
        self._pause_cond.acquire()
        self._pause_cond.notify_all()
        self._pause_cond.release()

    def pause(self):
        """请求在当前图片处理完成后暂停"""
        self._pause_requested = True

    def resume(self):
        """解除暂停，继续处理下一张"""
        self._pause_requested = False
        self._pause = False
        self._pause_cond.acquire()
        self._pause_cond.notify_all()
        self._pause_cond.release()

    def is_paused(self):
        return self._pause_requested or self._pause

    def _wait_if_paused(self):
        """若请求暂停则阻塞，直到 resume 或 cancel"""
        if not self._pause_requested:
            return
        self.log_callback("⏸ 已暂停，等待『继续』...")
        with self._pause_cond:
            while self._pause_requested and not self._cancel:
                self._pause_cond.wait()
            if self._cancel:
                return
            self._pause = False

    def run(self, config, state_data):
        """执行放大任务"""
        self._cancel = False
        self._pause = False
        self._pause_requested = False
        try:
            self._do_run(config, state_data)
        except Exception as e:
            self.log_callback(f"❌ 处理过程中发生错误: {str(e)}")
            self.log_callback(traceback.format_exc())
        finally:
            self.finish_callback()

    def _do_run(self, config, state_data):
        store = StateStore()
        session_id = state_data.get('session_id', '')

        api = webuiapi.WebUIApi(host=config.host, port=config.port)
        self.log_callback(f"🔗 已连接到 WebUI: {config.host}:{config.port}")

        # 日志输出质量设置
        mode_label = '无损' if config.save_mode == 'lossless' else '有损'
        self.log_callback(f"💾 保存模式: {mode_label}"
                          f"{f', 质量: {config.quality}%' if config.save_mode == 'lossy' else ''}"
                          f", WebP method={config.webp_method}")

        # 构建任务列表
        tasks = build_tasks(config.sources, config.output_dir, config.recursive)
        total = len(tasks)
        if total == 0:
            self.log_callback("❌ 未找到支持的图片文件")
            return
        self.log_callback(f"📂 共 {len(config.sources)} 个源, 生成 {total} 个任务")

        # 断点恢复: 校验上次进度是否仍然有效
        done = state_data.get('done', 0)
        saved_total = state_data.get('total', 0)
        saved_next = state_data.get('next_path', '')

        valid_resume, reason = validate_resume_state(tasks, done, saved_total, saved_next)
        if not valid_resume and done > 0:
            self.log_callback(f"⚠ 上次进度无效({reason})，从头开始")

        resume_index = done if valid_resume else 0
        if valid_resume and done > 0:
            self.log_callback(f"♻️ 检测到上次进度 {done}/{total}, 从第 {resume_index + 1} 张继续")

        # 更新状态记录（总数、下一张路径）
        state_data['total'] = total
        state_data['done'] = resume_index
        state_data['next_path'] = tasks[resume_index][1] if resume_index < total else ''

        # 恢复会话状态（用于 UI 显示暂停/继续按钮状态）
        if self.state_callback:
            self.state_callback({
                'total': total, 'done': resume_index, 'resumed': valid_resume,
                'session_id': session_id,
            })

        for idx in range(resume_index, total):
            if self._cancel:
                self.log_callback("⏹ 已取消")
                return

            # 检查暂停（当前张完成后不再继续）
            self._wait_if_paused()
            if self._cancel:
                self.log_callback("⏹ 已取消")
                return

            image_path = tasks[idx][1]
            out_path = tasks[idx][2]
            rel_path = tasks[idx][0]

            os.makedirs(os.path.dirname(out_path), exist_ok=True)

            # 记录本张开始时间
            item_start = time.time()

            self.log_callback(f"\n[{idx + 1}/{total}] {rel_path}")
            self.progress_callback(idx, total)

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
                self.log_callback(f"   ⏱ 本张耗时: {_fmt_seconds(time.time() - item_start)}")

            except Exception as e:
                self.log_callback(f"   ❌ 处理失败: {str(e)}")
                self.log_callback(f"   ⏱ 本张耗时: {_fmt_seconds(time.time() - item_start)}")
                if config.debug:
                    self.log_callback(traceback.format_exc())

            # 每处理完一张: 进度 +1 并持久化
            new_done = idx + 1
            state_data['done'] = new_done
            state_data['total'] = total
            state_data['next_path'] = tasks[new_done][1] if new_done < total else ''
            self.progress_callback(new_done, total)
            self._save_state(store, state_data)

        if not self._cancel:
            self.progress_callback(total, total)
            self.log_callback(f"\n{'=' * 40}")
            self.log_callback(f"🎉 全部处理完成！共处理 {total} 张图片")
            # 完成后清空会话状态
            state_data['session_id'] = ''
            state_data['done'] = 0
            self._save_state(store, state_data)
            if self.state_callback:
                self.state_callback({'total': 0, 'done': 0, 'resumed': False, 'session_id': ''})

    def _save_state(self, store, state_data):
        if self.state_callback:
            self.state_callback({
                'total': state_data.get('total', 0),
                'done': state_data['done'],
                'resumed': False,
                'session_id': state_data.get('session_id', ''),
            })
        store.save(state_data)


# ==================== 配置 ====================

class Config:
    """处理配置"""
    def __init__(self):
        self.mode = 'file'             # 'file' | 'folder'
        self.sources = []              # 源路径列表（文件或文件夹，可多选）
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

def select_folders_native(parent_hwnd=0):
    """Windows 原生「选择文件夹（可多选）」对话框。

    使用 IFileOpenDialog + FOS_PICKFOLDERS + FOS_ALLOWMULTISELECT，
    通过 ctypes 直接调用 COM，无需额外依赖。
    返回选中的文件夹绝对路径列表；取消返回 []。
    """
    if not sys.platform.startswith('win'):
        return None

    class GUID(ctypes.Structure):
        """GUID 结构体（Windows）"""
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        def __init__(self, guid_str=None):
            super().__init__()
            if guid_str:
                s = guid_str.strip('{}').replace('-', '')
                self.Data1 = int(s[0:8], 16)
                self.Data2 = int(s[8:12], 16)
                self.Data3 = int(s[12:16], 16)
                self.Data4 = (ctypes.c_ubyte * 8)(*[int(s[i:i + 2], 16) for i in range(16, 32, 2)])

    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.HRESULT
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.HRESULT
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None

    # CLSID_FileOpenDialog / IID_IFileOpenDialog
    CLSID_FileOpenDialog = GUID('{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}')
    IID_IFileOpenDialog = GUID('{D57C7288-D4AD-4768-BE02-9D969532D960}')

    FOS_PICKFOLDERS = 0x00000020
    FOS_FORCEFILESYSTEM = 0x00000040
    FOS_PATHMUSTEXIST = 0x00000800
    FOS_ALLOWMULTISELECT = 0x00000200
    SIGDN_FILESYSPATH = 0x80058000
    # HRESULT 为有符号，0x800704C7 即 -2147023673
    ERROR_CANCELLED = -2147023673

    ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED，重复调用返回 S_FALSE 无碍

    try:
        pfd = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_FileOpenDialog), None, 1,
            ctypes.byref(IID_IFileOpenDialog), ctypes.byref(pfd))
        if hr != 0 or not pfd.value:
            return []

        # 从 vtable 取出需要的 COM 方法
        def method_at(ptr, index, restype, *argtypes):
            vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
            return proto(vtbl[index])

        try:
            Show = method_at(pfd, 3, ctypes.HRESULT, wintypes.HWND)
            SetOptions = method_at(pfd, 9, ctypes.HRESULT, wintypes.DWORD)
            GetResults = method_at(pfd, 27, ctypes.HRESULT, ctypes.POINTER(ctypes.c_void_p))
            Release = method_at(pfd, 2, ctypes.HRESULT)

            SetOptions(pfd, FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST
                       | FOS_ALLOWMULTISELECT)

            hr = Show(pfd, wintypes.HWND(parent_hwnd))
            if hr == ERROR_CANCELLED:
                return []
            if hr != 0:
                return []

            arr = ctypes.c_void_p()
            if GetResults(pfd, ctypes.byref(arr)) != 0 or not arr.value:
                return []

            try:
                ArrGetCount = method_at(arr, 7, ctypes.HRESULT, ctypes.POINTER(wintypes.UINT))
                ArrGetItemAt = method_at(arr, 8, ctypes.HRESULT, wintypes.UINT,
                                         ctypes.POINTER(ctypes.c_void_p))
                ArrRelease = method_at(arr, 2, ctypes.HRESULT)

                count = wintypes.UINT()
                if ArrGetCount(arr, ctypes.byref(count)) != 0:
                    return []

                paths = []
                for i in range(count.value):
                    item = ctypes.c_void_p()
                    if ArrGetItemAt(arr, i, ctypes.byref(item)) != 0 or not item.value:
                        continue
                    try:
                        ItemGetDisplayName = method_at(item, 5, ctypes.HRESULT,
                                                       wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
                        psz = wintypes.LPWSTR()
                        if ItemGetDisplayName(item, wintypes.DWORD(SIGDN_FILESYSPATH),
                                              ctypes.byref(psz)) == 0 and psz.value:
                            paths.append(psz.value)
                            ole32.CoTaskMemFree(psz)
                    finally:
                        ItemRelease = method_at(item, 2, ctypes.HRESULT)
                        ItemRelease(item)
                return paths
            finally:
                ArrRelease(arr)
        finally:
            Release(pfd)
    finally:
        ole32.CoUninitialize()


class UpscaleGUI:
    """主界面"""

    def __init__(self, root):
        self.root = root
        self.root.title("WebUI 图片放大工具")
        self.root.geometry("820x760")
        self.root.minsize(720, 660)

        self.config = Config()
        self.worker = None
        self.worker_thread = None
        self.log_queue = queue.Queue()
        self.state_store = StateStore()
        self._saved_state = None
        self._resumed = False
        self._session_total = 0
        self._session_done = 0
        self._start_time = None
        self._last_item_time = None
        self._pause_start = None

        self._build_ui()
        self._poll_log_queue()
        # 初始化质量控件的状态
        self._on_quality_mode_change()
        # 尝试恢复上次未完成的进度
        self._try_restore()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 界面构建 ----------

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use('vista' if 'vista' in style.theme_names() else 'clam')

        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 分页: 设置 / 进度
        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill=tk.BOTH, expand=True)

        settings_tab = ttk.Frame(notebook, padding="10")
        progress_tab = ttk.Frame(notebook, padding="10")
        notebook.add(settings_tab, text=" 设置 ")
        notebook.add(progress_tab, text=" 进度 ")

        # ===== 输入源 =====
        source_frame = ttk.LabelFrame(settings_tab, text="输入源", padding="10")
        source_frame.pack(fill=tk.X, pady=(0, 5))

        self.mode_var = tk.StringVar(value='file')
        ttk.Radiobutton(source_frame, text="单张图片", variable=self.mode_var,
                        value='file', command=self._on_mode_change).grid(row=0, column=0, sticky=tk.W, padx=(0, 15))
        ttk.Radiobutton(source_frame, text="文件夹（可多选）", variable=self.mode_var,
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
        output_frame = ttk.LabelFrame(settings_tab, text="输出设置", padding="10")
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
        quality_frame = ttk.LabelFrame(settings_tab, text="保存质量", padding="10")
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
        param_frame = ttk.LabelFrame(settings_tab, text="放大参数", padding="10")
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
        conn_frame = ttk.LabelFrame(settings_tab, text="WebUI 连接", padding="10")
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

        # ===== 进度页: 操作按钮 + 进度 =====
        btn_frame = ttk.Frame(progress_tab)
        btn_frame.pack(fill=tk.X, pady=(0, 10))

        self.start_btn = ttk.Button(btn_frame, text="▶ 开始处理", command=self._start_upscale, width=14)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.pause_btn = ttk.Button(btn_frame, text="⏸ 暂停", command=self._pause_upscale,
                                    state=tk.DISABLED, width=10)
        self.pause_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.resume_btn = ttk.Button(btn_frame, text="▶ 继续", command=self._resume_upscale,
                                     state=tk.DISABLED, width=10)
        self.resume_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.reset_btn = ttk.Button(btn_frame, text="↺ 重置", command=self._reset_upscale,
                                    state=tk.NORMAL, width=10)
        self.reset_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.cancel_btn = ttk.Button(btn_frame, text="⏹ 取消", command=self._cancel_upscale,
                                     state=tk.DISABLED, width=10)
        self.cancel_btn.pack(side=tk.LEFT)

        # 进度条
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(btn_frame, variable=self.progress_var, maximum=100, length=200)
        self.progress_bar.pack(side=tk.RIGHT, padx=(10, 0))

        self.progress_label = ttk.Label(btn_frame, text="")
        self.progress_label.pack(side=tk.RIGHT, padx=(5, 0))

        # 时间统计（独立行，避免被压缩）
        self.time_label = ttk.Label(progress_tab, text="", foreground="#555")
        self.time_label.pack(fill=tk.X, pady=(0, 10))

        # ===== 进度页: 日志输出 =====
        log_frame = ttk.LabelFrame(progress_tab, text="处理日志", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True)

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
        """浏览输入路径（文件夹模式使用 Windows 原生多选对话框）"""
        is_folder = self.mode_var.get() == 'folder'
        if is_folder:
            win_paths = select_folders_native(self.root.winfo_id())
            if win_paths is None:
                # 非 Windows 环境退回单选框
                path = filedialog.askdirectory(title="选择图片文件夹")
                if path:
                    self._set_source_paths([path])
            elif win_paths:
                self._set_source_paths(win_paths)
                self._log(f"📂 已选择 {len(win_paths)} 个文件夹")
        else:
            path = filedialog.askopenfilename(
                title="选择图片",
                filetypes=[("图片文件", "*.png *.jpg *.jpeg *.webp *.bmp *.tiff"), ("所有文件", "*.*")]
            )
            if path:
                self._set_source_paths([path])

    def _set_source_paths(self, paths):
        """更新路径框显示（多选时用分号分隔）"""
        self.source_path_var.set(" ; ".join(paths))

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
            # 时间统计: 已用 / 平均每张 / 预计剩余（独立行显示）
            elapsed = (time.time() - self._start_time) if self._start_time else 0
            if current > 0 and elapsed > 0:
                avg = elapsed / current
                remain = avg * (total - current)
                text = (f"已用 ⏱ {_fmt_seconds(elapsed)}   "
                        f"平均 {_fmt_seconds(avg)}/张   "
                        f"预计剩余 {_fmt_seconds(remain)}")
            else:
                text = f"已用 ⏱ {_fmt_seconds(elapsed)}"
            self.time_label.config(text=text)

    def _on_finish(self):
        """处理完成回调（可由工作线程调用）"""
        self.root.after(0, self._do_finish)

    def _do_finish(self):
        self.start_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)
        self.pause_btn.config(state=tk.DISABLED)
        self.resume_btn.config(state=tk.DISABLED)
        self.reset_btn.config(state=tk.NORMAL)
        self.worker = None
        self.worker_thread = None
        # 输出总耗时
        if self._start_time:
            total_sec = time.time() - self._start_time
            self._log(f"⏱ 本次处理总耗时: {_fmt_seconds(total_sec)}")
            self._start_time = None
            self._last_item_time = None

    def _on_state(self, state):
        """工作线程上报会话状态（总/已完成/是否已恢复），主线程更新 UI"""
        self.root.after(0, lambda: self._do_state(state))

    def _do_state(self, state):
        total = state.get('total', 0)
        done = state.get('done', 0)
        self._session_total = total
        self._session_done = done
        if total > 0:
            self._update_progress(done, total)
        if state.get('resumed'):
            self._resumed = True
        # 运行中: 暂停/重置/取消可用
        if self.worker is not None or self.worker_thread is not None:
            self.pause_btn.config(state=tk.NORMAL)
            self.resume_btn.config(state=tk.DISABLED)
            self.reset_btn.config(state=tk.NORMAL)
            self.cancel_btn.config(state=tk.NORMAL)
            # 若正在暂停状态，进入等待
            if self.worker and self.worker.is_paused():
                self.pause_btn.config(state=tk.DISABLED)
                self.resume_btn.config(state=tk.NORMAL)

    # ---------- 启动 / 取消 ----------

    def _parse_sources(self):
        """从路径框解析源路径列表（分号分隔，支持多选）"""
        text = self.source_path_var.get().strip()
        if not text:
            return []
        parts = [p.strip() for p in text.split(';') if p.strip()]
        return parts

    def _start_upscale(self):
        """开始处理"""
        # 读取配置
        self.config.mode = self.mode_var.get()
        self.config.sources = self._parse_sources()
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
        if not self.config.sources:
            self._log("❌ 请选择输入路径")
            return
        if not self.config.output_dir:
            self._log("❌ 请设置输出目录")
            return

        for src in self.config.sources:
            if not os.path.exists(src):
                self._log(f"❌ 输入路径不存在: {src}")
                return

        # 创建输出目录
        try:
            os.makedirs(self.config.output_dir, exist_ok=True)
        except Exception as e:
            self._log(f"❌ 无法创建输出目录: {e}")
            return

        # 准备会话状态（新会话或恢复）
        if self._resumed and self._saved_state:
            state_data = self._saved_state
            self._resumed = False
            self._saved_state = None
        else:
            import uuid
            state_data = {
                'version': 1,
                'active': True,
                'session_id': uuid.uuid4().hex,
                'done': 0,
                'sources': self.config.sources,
                'output_dir': self.config.output_dir,
                'recursive': self.config.recursive,
                'mode': self.config.mode,
                # 放大参数
                'target_width': self.config.target_width,
                'target_height': self.config.target_height,
                'scale_factor': self.config.scale_factor,
                'upscaler_name': self.config.upscaler_name,
                # 保存质量
                'save_mode': self.config.save_mode,
                'quality': self.config.quality,
                'webp_method': self.config.webp_method,
                'preserve_metadata': self.config.preserve_metadata,
                # WebUI 连接
                'host': self.config.host,
                'port': self.config.port,
                'debug': self.config.debug,
                'date': time.strftime('%Y-%m-%d %H:%M:%S'),
            }
            self._session_total = 0
            self._session_done = 0

        # 启动工作线程
        self.worker = UpscaleWorker(
            log_callback=self._log,
            progress_callback=self._update_progress,
            finish_callback=self._on_finish,
            state_callback=self._on_state,
        )

        self.worker_thread = threading.Thread(
            target=self.worker.run, args=(self.config, state_data), daemon=True
        )

        self.start_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.pause_btn.config(state=tk.NORMAL)
        self.resume_btn.config(state=tk.DISABLED)
        self.reset_btn.config(state=tk.NORMAL)
        self.progress_var.set(0)
        self.progress_label.config(text="0/0")
        self.time_label.config(text="")
        self.log_text.delete(1.0, tk.END)

        # 时间统计: 记录开始时间
        self._start_time = time.time()

        self._log("=" * 40)
        self._log("🚀 开始处理...")
        self._log(f"📂 输入源 {len(self.config.sources)} 个")

        self.worker_thread.start()

    def _pause_upscale(self):
        """暂停：当前张处理完成后停止"""
        if self.worker:
            self.worker.pause()
            self.pause_btn.config(state=tk.DISABLED)
            self.resume_btn.config(state=tk.NORMAL)
            self._pause_start = time.time()
            self._log("⏸ 已请求暂停，当前图片处理完成后停止...")

    def _resume_upscale(self):
        """继续：解除暂停"""
        if self.worker:
            self.worker.resume()
            self.pause_btn.config(state=tk.NORMAL)
            self.resume_btn.config(state=tk.DISABLED)
            # 扣除暂停时长，使时间统计只计处理时间
            if self._pause_start:
                pause_sec = time.time() - self._pause_start
                self._start_time += pause_sec
                self._pause_start = None
            self._log("▶ 继续处理...")

    def _reset_upscale(self):
        """重置：清除恢复状态和已保存进度，进度归零"""
        if self.worker:
            self.worker.cancel()
        self._resumed = False
        self._saved_state = None
        self._session_total = 0
        self._session_done = 0
        self._start_time = None
        self._last_item_time = None
        self._pause_start = None
        # 清空保存的进度
        try:
            if os.path.exists(self.state_store.path):
                os.remove(self.state_store.path)
        except Exception:
            pass
        self.progress_var.set(0)
        self.progress_label.config(text="0/0")
        self.time_label.config(text="")
        self.log_text.delete(1.0, tk.END)
        self._log("↺ 已重置进度")
        self._do_finish()

    def _try_restore(self):
        """启动时尝试读取上次未完成的进度（含验证）"""
        data = self.state_store.load()
        if not data or not data.get('active') or not data.get('session_id'):
            return
        # 验证源路径仍存在
        sources = data.get('sources') or []
        if not sources or not all(os.path.exists(s) for s in sources):
            self._log("⚠ 上次进度的源路径已失效，忽略并从头开始")
            return
        # 验证输出目录存在（不存在则创建）
        output_dir = data.get('output_dir', '')
        if output_dir:
            try:
                os.makedirs(output_dir, exist_ok=True)
            except Exception:
                self._log("⚠ 上次进度的输出目录无法创建，忽略并从头开始")
                return
        # 验证已保存的下一张路径仍存在
        next_path = data.get('next_path', '')
        if next_path and not os.path.exists(next_path):
            self._log("⚠ 上次进度的下一张图片已不存在，忽略并从头开始")
            return
        # 恢复输入/输出等参数
        if sources:
            self._set_source_paths(sources)
            self.mode_var.set(data.get('mode', 'folder'))
            self._on_mode_change()
        if output_dir:
            self.output_dir_var.set(output_dir)
        if 'recursive' in data:
            self.recursive_var.set(data['recursive'])
        # 恢复放大参数
        if 'target_width' in data:
            self.width_var.set(str(data['target_width']))
        if 'target_height' in data:
            self.height_var.set(str(data['target_height']))
        if 'scale_factor' in data:
            self.scale_var.set(str(data['scale_factor']))
        if 'upscaler_name' in data:
            self.upscaler_var.set(data['upscaler_name'])
        # 恢复质量参数
        if 'save_mode' in data:
            self.save_mode_var.set(data['save_mode'])
            self._on_quality_mode_change()
        if 'quality' in data:
            self.quality_var.set(data['quality'])
            self.quality_label.config(text=f"{data['quality']}%")
        if 'webp_method' in data:
            self.webp_method_var.set(str(data['webp_method']))
        if 'preserve_metadata' in data:
            self.preserve_meta_var.set(data['preserve_metadata'])
        # 恢复连接参数
        if 'host' in data:
            self.host_var.set(data['host'])
        if 'port' in data:
            self.port_var.set(str(data['port']))
        if 'debug' in data:
            self.debug_var.set(data['debug'])
        self._saved_state = data
        self._resumed = True
        self._session_total = data.get('done', 0)
        self._session_done = data.get('done', 0)
        self._log(f"♻️ 检测到上次未完成的进度（已完成 {data.get('done', 0)} 张），"
                  f"点击『开始处理』可从断点继续")

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
