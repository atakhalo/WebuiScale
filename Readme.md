# WebUI Scale

WebUIScale，SD WebUI 图片放大助手，支持递归子文件夹，保留生成元数据

## 使用
1. SD WebUI 带 api 参数 启动
	1. 修改 webui-user.bat 增加 `--api` 参数
	2.  如 `set COMMANDLINE_ARGS=--uv --theme dark --api`
2. 运行程序
3. 在「设置」页选择图片或文件夹（文件夹可多选），调整参数
4. 切到「进度」页，点击「开始处理」，可暂停/继续/重置/取消

## 截图
<img src="DocImage/view1.webp" height="300" alt="参数页">
<img src="DocImage/view2.webp" height="300" alt="进度页">

## 功能

- **界面**：设置 / 进度 两个标签页
- **输入**：支持单张图片或整个文件夹（可多选文件夹；支持递归子目录）
- **多文件夹输出**：单文件夹时按目录输出；多选文件夹时，每个源文件夹名作为输出目录下的一级子文件夹
- **预设**：一键填入 2560×1440 / 1920×1080 / 3840×2160，支持交换宽高
- **缩放**：指定目标尺寸或按比例放大
- **算法**：可选常见放大算法（4x-AnimeSharp、4x-UltraSharp、R-ESRGAN 等）或自己输入
- **元数据**：保留原图 EXIF 及 PNG tEXt 信息（WebUI parameters、ComfyUI prompt/workflow）
- **保存质量**：有损（百分比 1–100）/ 无损，WebP 压缩方法 0–6 可选
- **进度**：实时日志 + 进度条，支持暂停/继续/重置/取消
- **断点恢复**：未完成时自动记录到 `data-user.json`（含全部参数与下一张路径），下次启动自动恢复；恢复时校验任务总数与下一张路径，失败则从头开始
- **时间统计**：每张处理耗时、已用时间、平均每张、预计剩余

## 提示
- 宽高填 0 时使用「缩放系数」按比例放大
- 放大算法中的 `4x-UltraSharp` 等需确认 WebUI 已安装对应模型
- 处理大量图片时 WebUI 需有足够显存
- 暂停后点击「继续」才会处理下一张

## 开发相关 
### 依赖
```bash
pip install webuiapi piexif
```
