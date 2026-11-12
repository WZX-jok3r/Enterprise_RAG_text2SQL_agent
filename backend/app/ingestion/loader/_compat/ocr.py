"""loader._compat.ocr - OCR 依赖探测（PDF 页面 OCR 与 DOCX 图片 OCR 共用）

两个消费方：
- loader/__init__.py 的 _try_ocr_page：PDF 页面经 pymupdf 渲染成 PNG 后 OCR
- loader/docx.py 的 _ocr_image_bytes：DOCX 内嵌图片按 blob 解码后 OCR

拆成独立模块而不是各域各探测一份，是因为两者都需要 pytesseract：
重复探测会让 HAS_TESSERACT 出现两个真相源，任一处被替换后另一处仍按旧值走，
排查起来非常费时间。

Pillow 只有 DOCX 图片路径需要（PDF 路径直接用 pixmap 的 png 字节）。
"""

HAS_TESSERACT = False
pytesseract = None
try:
    import pytesseract

    HAS_TESSERACT = True
except ImportError:
    pass

HAS_PIL = False
try:
    from PIL import Image  # noqa: F401  （仅用于能力探测，实际解码在函数内延迟导入）

    HAS_PIL = True
except ImportError:
    pass
