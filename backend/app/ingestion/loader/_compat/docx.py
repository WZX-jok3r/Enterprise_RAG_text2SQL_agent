"""loader._compat.docx - python-docx 探测（仅 DOCX 域需要）"""

HAS_DOCX = False
DocxDocument = None
try:
    from docx import Document as DocxDocument

    HAS_DOCX = True
except ImportError:
    pass
