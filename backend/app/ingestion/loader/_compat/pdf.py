"""loader._compat.pdf - PyMuPDF / pdfplumber 探测（仅 PDF 域需要）

顺带承担两件"导入即发生"的副作用，因为它们都属于"本次能不能用 PDF 库"：
- 抑制 fitz 弃用警告（新旧版 PyMuPDF 都会打）
- 用 hasattr 实测 get_tables() 是否可用，而不是只比版本号

从 loader.py 迁出，探测逻辑逐字未改。
"""

import warnings

# 抑制 fitz 弃用警告（兼容新旧版 PyMuPDF）
warnings.filterwarnings("ignore", message=".*The `fitz` API is deprecated.*")
warnings.filterwarnings("ignore", message=".*fitz is deprecated.*")

# 兼容写法：优先 import pymupdf（新版），降级到 import fitz（旧版）
HAS_PYMUPDF = False
pymupdf = None
try:
    import pymupdf

    HAS_PYMUPDF = True
except ImportError:
    try:
        import fitz as pymupdf

        HAS_PYMUPDF = True
    except ImportError:
        pass

HAS_PDFPLUMBER = False
pdfplumber = None
_pdfplumber_version = "unknown"
try:
    import pdfplumber

    HAS_PDFPLUMBER = True
    _pdfplumber_version = getattr(pdfplumber, "__version__", "unknown")
except ImportError:
    pass

# 检测 PyMuPDF 版本是否支持 get_tables()
# 使用 hasattr 实际检测，而非仅依赖版本号
PYMUPDF_GET_TABLES_AVAILABLE = False
if HAS_PYMUPDF:
    _check_doc = None
    try:
        _check_doc = pymupdf.open()
        _test_page = _check_doc[0]
        PYMUPDF_GET_TABLES_AVAILABLE = hasattr(_test_page, 'get_tables') and callable(_test_page.get_tables)
        _check_doc.close()
    except Exception:
        version_str = getattr(pymupdf, "__version__", "")
        if version_str:
            version_parts = version_str.split(".")
            major = int(version_parts[0]) if len(version_parts) > 0 else 0
            minor = int(version_parts[1]) if len(version_parts) > 1 else 0
            if major > 1 or (major == 1 and minor >= 23):
                PYMUPDF_GET_TABLES_AVAILABLE = True
        if _check_doc is not None:
            try:
                _check_doc.close()
            except Exception:
                pass
