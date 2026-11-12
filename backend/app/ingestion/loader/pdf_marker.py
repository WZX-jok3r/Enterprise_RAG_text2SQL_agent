"""
loader.pdf_marker - marker 深度表格识别（整文件级子进程兜底）

只在"三级表格提取全失败 + 疑似无线表格 + 开关开启"时被 pdf_loader 调用，
未安装 marker 时静默返回 None，调用方保持按页解析结果。
从 loader.py 迁出，函数体逐字未改。
"""

from pathlib import Path
from typing import Optional

from .base import logger



def _convert_pdf_with_marker(file_path, timeout: int = 900) -> Optional[str]:
    """
    用 marker（marker_single CLI + --disable_ocr 纯文本层）把 PDF 转成 Markdown。

    为什么用 CLI 而非 Python API：
    - marker 2.0 的 Python API 默认 balanced 模式会初始化/下载 OCR VLM 大模型，
      国内网络下容易卡死；CLI 的 --disable_ocr 走纯文本层 + pdftext 表格重建，
      CPU 可跑、不需要 OCR 模型，对 digital PDF（有文本层）即可结构化无线表格。
    - 复用本机已安装的 marker_single 命令，行为与手工验证完全一致。

    成功返回 Markdown 文本，失败或不可用返回 None（调用方优雅降级）。
    """
    import shutil
    import subprocess
    import sys
    import tempfile

    # 定位 marker_single 可执行文件（优先 PATH，回退到当前解释器的 Scripts 目录）
    marker_cmd = shutil.which("marker_single")
    if not marker_cmd:
        scripts_dir = Path(sys.executable).parent
        for cand in ("marker_single", "marker_single.exe"):
            p = scripts_dir / cand
            if p.exists():
                marker_cmd = str(p)
                break
    if not marker_cmd:
        logger.info("[PDF] 未找到 marker_single 命令，跳过 marker 深度表格识别")
        return None

    out_dir = tempfile.mkdtemp(prefix="marker_out_")
    cmd = [marker_cmd, str(file_path), "--output_dir", out_dir, "--disable_ocr"]
    logger.info(f"[PDF] 调用 marker 识别无线表格: {Path(file_path).name}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            logger.warning(f"[PDF] marker 转换失败: {(result.stderr or '')[-400:]}")
            return None
        md_files = sorted(Path(out_dir).rglob("*.md"))
        if not md_files:
            logger.warning("[PDF] marker 未产出 markdown 文件")
            return None
        return md_files[0].read_text(encoding="utf-8").strip() or None
    except subprocess.TimeoutExpired:
        logger.warning(f"[PDF] marker 转换超时（>{timeout}s），跳过")
        return None
    except Exception as e:
        logger.warning(f"[PDF] marker 调用失败: {e}")
        return None
