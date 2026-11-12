"""
loader - 多格式文档加载解析核心（优化版 v4）

包结构（2026-11 拆分，解析行为零变化）：
- base.py       : Document / compute_file_hash / _data_to_markdown_table / logger（零重依赖）
- text.py       : .txt / .md 解析
- docx.py       : .docx 解析（段落 / 表格 / 图片 OCR）
- xlsx.py       : .xlsx 解析（openpyxl 优先，pandas 降级）
- pdf_page.py   : PDF 单页处理（预处理 / 分栏重排 / 页码提取 / OCR 兜底）
- pdf_tables.py : PDF 表格三级降级链 + 无线表格疑似判定
- pdf_marker.py : marker 深度识别兜底（整文件级子进程）
- pdf_loader.py : PDF 解析编排（每页一个 Document + 双页码系统）
- _compat/      : 按域探测可选依赖（pdf / docx / xlsx / ocr），避免一个库拖累另一域
- __main__.py   : `python -m app.ingestion.loader` 冒烟入口
- 本文件        : 配置快照 + 统一入口（load_file / scan / load_all / 序列化）

依赖关系（按需加载，2026-11 Phase 6）：
- 本模块只 import base.py（零重依赖）与 app.core.config；
- 解析域由 FORMAT_LOADER_MAP 的 (模块路径, 函数名) 路由在 load_file 时导入：
  “只取哈希函数 / 只扫目录”不得连带拉起任何解析库；
- 配置快照 KNOWLEDGE_BASE_DIR / SUPPORTED_EXTENSIONS / PDF_SUBDIR 必须与读取它们的
  load_file / scan_knowledge_base / load_all_documents 同处本模块
  （tests/test_loader_public_surface.py 断言 `func.__globals__ is vars(loader)`）；
- USE_MARKER_FOR_PDF 属于 pdf_loader（与读取者 load_pdf 同模块），HAS_* 能力标志
  属于 _compat/ —— 本模块都不再导出，任何一条再导出都会把 pymupdf/pandas 拉进门面。

优化内容（细节见各子模块）：
  1. PDF: 表格结构化提取（pdfplumber优先 + get_tables降级 + dict兜底）
       + 双栏检测与文本重排 + 旋转校正 + OCR兜底
       + 多页长文本页码信息注入（text内容前加【第X页/Y】前缀）
  2. DOCX: 改进表格提取（Markdown表格格式）+ 段落结构保留
  3. TXT: 自动编码检测（utf-8/gbk/latin-1）
  4. XLSX: 改进为openpyxl方案，支持合并单元格识别
  5. 修复 fitz 弃用警告，兼容新旧版 PyMuPDF
  6. 修复 pdfplumber options 参数兼容性问题
  7. 修复 get_tables() 检测方法（hasattr 实际检测）
  8. 修复降级链逻辑（确保各策略正确串联）
  9. 降低冗余日志级别（每页降级日志改为DEBUG，避免INFO刷屏）

依赖要求：
  pip install "pymupdf>=1.23.0" python-docx openpyxl pandas
可选依赖（增强能力）：
  pip install pytesseract          # PDF OCR兜底（扫描件）
  pip install pdfplumber           # PDF表格备选提取方案（强烈推荐安装）
"""

import importlib
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.core.config import settings

from .base import Document, compute_file_hash, logger

# 本模块唯一的对外名字集合：解析器不在其中（它们走路由按需导入）。
__all__ = [
    "Document",
    "compute_file_hash",
    "load_file",
    "scan_knowledge_base",
    "load_all_documents",
    "documents_to_json",
    "documents_from_json",
    "FORMAT_LOADER_MAP",
    "KNOWLEDGE_BASE_DIR",
    "SUPPORTED_EXTENSIONS",
    "PDF_SUBDIR",
]

# 配置兼容层：沿用原 UPPER_CASE 常量名，避免改动下方逻辑
KNOWLEDGE_BASE_DIR = settings.knowledge_base_dir
SUPPORTED_EXTENSIONS = settings.supported_extensions
PDF_SUBDIR = settings.pdf_subdir


# ============================================================================
# 统一加载入口
# ============================================================================

# 格式 → (模块路径, 函数名)
# 用字符串而不是真函数引用：持引用等于在导入期把四个解析域全部拉起，
# 按需加载就无从谈起（曾因此 `from app.ingestion.loader import compute_file_hash`
# 要 738ms / 1142 个模块）。
FORMAT_LOADER_MAP: Dict[str, Tuple[str, str]] = {
    ".txt": ("app.ingestion.loader.text", "load_txt"),
    ".md": ("app.ingestion.loader.text", "load_md"),
    ".docx": ("app.ingestion.loader.docx", "load_docx"),
    ".xlsx": ("app.ingestion.loader.xlsx", "load_xlsx"),
    ".pdf": ("app.ingestion.loader.pdf_loader", "load_pdf"),
}


def _resolve_parser(ext: str):
    """按路由表取出真正的解析函数。模块导入发生在首次调用时，之后命中 sys.modules。"""
    route = FORMAT_LOADER_MAP.get(ext)
    if route is None:
        return None
    module_path, func_name = route
    return getattr(importlib.import_module(module_path), func_name)


def load_file(file_path: Path) -> List[Document]:
    """根据文件后缀调用对应解析器（解析域按需导入，未用到的格式不会被加载）"""
    ext = file_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        logger.warning(f"不支持的文件格式: {ext} ({file_path.name})")
        return []
    parser = _resolve_parser(ext)
    if parser is None:
        return []
    return parser(file_path)


def scan_knowledge_base() -> List[Path]:
    """
    扫描 knowledge_base 目录，收集所有支持的文件
    PDF 文件放在 pdf_examples/ 子目录中
    """
    files = []
    if not KNOWLEDGE_BASE_DIR.exists():
        logger.error(f"知识库目录不存在: {KNOWLEDGE_BASE_DIR}")
        return files

    # 扫描根目录（非 pdf 文件）
    for ext in SUPPORTED_EXTENSIONS:
        if ext == ".pdf":
            continue  # pdf 单独处理
        files.extend(KNOWLEDGE_BASE_DIR.glob(f"*{ext}"))

    # 扫描 pdf_examples/ 子目录
    pdf_dir = KNOWLEDGE_BASE_DIR / PDF_SUBDIR
    if pdf_dir.exists():
        files.extend(pdf_dir.glob("*.pdf"))
    else:
        logger.warning(f"PDF 子目录不存在: {pdf_dir}")

    return sorted(files)


def load_all_documents() -> Dict[str, Any]:
    """
    加载知识库中所有文档
    返回: {
        "documents": List[Document],
        "stats": {"total_files": int, "total_chunks": int, "by_format": dict}
    }
    """
    file_paths = scan_knowledge_base()
    all_docs: List[Document] = []
    stats = {"total_files": 0, "total_chunks": 0, "by_format": {}}

    for fp in file_paths:
        docs = load_file(fp)
        if docs:
            all_docs.extend(docs)
            stats["total_files"] += 1
            stats["total_chunks"] += len(docs)
            fmt = fp.suffix.lower().lstrip(".")
            stats["by_format"][fmt] = stats["by_format"].get(fmt, 0) + 1

    logger.info(f"知识库加载完成: {stats}")
    return {"documents": all_docs, "stats": stats}


# ============================================================================
# 序列化工具（用于 processed_cache）
# ============================================================================

def documents_to_json(docs: List[Document]) -> str:
    """Document 列表序列化为 JSON 字符串"""
    return json.dumps([doc.to_dict() for doc in docs], ensure_ascii=False, indent=2)


def documents_from_json(json_str: str) -> List[Document]:
    """JSON 字符串反序列化为 Document 列表"""
    data = json.loads(json_str)
    return [Document.from_dict(item) for item in data]
