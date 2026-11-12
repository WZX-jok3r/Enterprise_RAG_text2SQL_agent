"""
loader.pdf_page - PDF 单页处理：预处理、分栏检测与重排、页码提取、OCR 兜底

只消费 PyMuPDF Page 对象的接口，不参与表格提取（pdf_tables）与文件级编排（pdf_loader）。
从 loader.py 迁出，函数体逐字未改。
"""

from typing import Any, Dict, List, Optional, Tuple

from ._compat.ocr import HAS_TESSERACT, pytesseract
from ._compat.pdf import pymupdf
from .base import logger



def preprocess_page(page) -> Dict[str, Any]:
    """
    页面预处理：
    1. 检测并校正旋转角度
    2. 检测纸张尺寸(A3/A4/横向)，记录元信息
    3. 检测是否为图片型PDF(触发OCR兜底)
    返回: dict 包含 rotation, is_landscape, page_width, page_height,
          text_length, has_images, need_ocr
    """
    rotation = page.rotation  # 0, 90, 180, 270
    # 获取页面尺寸
    rect = page.rect
    width, height = rect.width, rect.height
    # 标准化：确保 width <= height 时为 portrait，反之为 landscape
    is_landscape = width > height
    # 如果页面旋转了90/270度且是横向，需要交换宽高
    if rotation in (90, 270):
        is_landscape = not is_landscape
    # 检测是否为图片型页面（文本极少但页面有内容）
    text = page.get_text("text").strip()
    text_length = len(text)
    # 检查页面是否有图片
    images = page.get_images(full=True)
    has_images = len(images) > 0
    # 判断是否需要OCR：文本很少但有图片，或文本极少
    need_ocr = (text_length < 20 and has_images) or (text_length < 5 and page.get_text("blocks"))
    return {
        "rotation": rotation,
        "is_landscape": is_landscape,
        "page_width": width,
        "page_height": height,
        "text_length": text_length,
        "has_images": has_images,
        "need_ocr": need_ocr,
    }


def detect_columns(page, threshold=0.45) -> List[Tuple[float, float]]:
    """
    检测页面分栏布局
    参数:
        page: PyMuPDF page 对象
        threshold: 分栏间隙比例阈值
    返回:
        [(col_start_x, col_end_x), ...] 列的x坐标范围列表
    """
    text_dict = page.get_text("dict")
    blocks = text_dict.get("blocks", [])
    block_x_ranges = []
    for block in blocks:
        if block.get("type") == 0:  # 文本块
            rect = block.get("rect", None)
            if rect is not None:
                block_x_ranges.append((rect[0], rect[2]))
    if not block_x_ranges:
        return []
    page_width = page.rect.width
    # 检测分栏间隙：找 x1 最大值集中的区域
    x1_values = sorted([x1 for x0, x1 in block_x_ranges])
    if not x1_values:
        return []
    # 找空白间隙（某个x区间内没有文本块跨越）
    gaps = []
    gap_threshold = page_width * 0.08  # 间隙至少占页面8%才认为是分栏
    # 将所有块的左右边界排序
    all_edges = []
    for x0, x1 in block_x_ranges:
        all_edges.append((x0, 'start'))
        all_edges.append((x1, 'end'))
    all_edges.sort(key=lambda e: e[0])
    # 扫描找间隙
    active_count = 0
    gap_start = None
    for edge_x, edge_type in all_edges:
        if edge_type == 'start':
            if active_count == 0 and gap_start is not None:
                gap_width = edge_x - gap_start
                if gap_width > gap_threshold and gap_start > 0:
                    gaps.append(gap_start)
            active_count += 1
        else:
            active_count -= 1
            if active_count == 0:
                gap_start = edge_x
    if gaps:
        gaps.sort()
        # 过滤掉太靠近边缘的间隙
        gaps = [g for g in gaps if g > page_width * 0.05 and g < page_width * 0.95]
        col_starts = [0] + gaps + [page_width]
        columns = []
        for i in range(len(col_starts) - 1):
            col_start = col_starts[i]
            col_end = col_starts[i + 1]
            if col_end - col_start > page_width * 0.1:
                columns.append((col_start, col_end))
    else:
        columns = [(0, page_width)]
    return columns


def reorder_text_blocks_by_column(blocks: List[dict], columns: List[Tuple[float, float]]) -> List[dict]:
    """按列优先顺序重排文本块"""
    if len(columns) <= 1:
        return sorted(blocks, key=lambda b: (b.get("rect", [0, 0, 0, 0])[1], b.get("rect", [0, 0, 0, 0])[0]))
    column_blocks = [[] for _ in columns]
    for block in blocks:
        rect = block.get("rect", [0, 0, 0, 0])
        block_x = rect[0]
        assigned = False
        for i, (col_start, col_end) in enumerate(columns):
            if col_start <= block_x <= col_end:
                column_blocks[i].append(block)
                assigned = True
                break
        if not assigned:
            column_blocks[0].append(block)
    ordered_blocks = []
    for col_blks in column_blocks:
        col_blks.sort(key=lambda b: (b.get("rect", [0, 0, 0, 0])[1], b.get("rect", [0, 0, 0, 0])[0]))
        ordered_blocks.extend(col_blks)
    return ordered_blocks


def _try_ocr_page(page) -> str:
    """尝试对页面进行OCR识别"""
    if not HAS_TESSERACT:
        logger.warning("pytesseract 未安装，跳过OCR（可执行 pip install pytesseract 安装）")
        return ""
    try:
        mat = pymupdf.Matrix(2.0, 2.0)  # 2倍缩放提高OCR精度
        pix = page.get_pixmap(matrix=mat)
        img_data = pix.tobytes("png")
        text = pytesseract.image_to_string(img_data, lang="chi_sim+eng")
        return text.strip()
    except ImportError:
        logger.warning("pytesseract 未安装，跳过OCR")
        return ""
    except Exception as e:
        logger.error(f"OCR失败: {e}")
        return ""


def extract_logical_page_number(text: str) -> Optional[int]:
    """
    从页面文本中提取逻辑页码（内容中标注的页码）

    常见页码格式:
    - "Page 39 of 50" (西式页码)
    - "第39页/50" (中文页码)
    - "P.39" (简写)

    返回: 逻辑页码 (如39)，未找到返回None
    """
    import re

    # 页脚/页眉常见的页码模式
    page_patterns = [
        (r'Page\s+(\d+)\s+of\s+\d+', 1),  # Page 39 of 50
        (r'第\s*(\d+)\s*页\s*[^\d]*\d+', 1),  # 第39页/50
        (r'(\d+)\s*/\s*\d+\s*[页Pp]', 1),  # 39/50页, 39/50P
        (r'[Pp]\.\s*(\d+)', 1),  # P.39, p.39
        (r'页码[:：]?\s*(\d+)', 1),  # 页码:39
        (r'^(\d+)\s*$', 1),  # 单独的数字行（可能是页码）
    ]

    # 检查文本的末尾部分（页脚通常在这里）
    lines = text.strip().split('\n')

    # 优先检查最后几行（页脚区域）
    footer_lines = lines[-5:] if len(lines) >= 5 else lines

    for line in footer_lines:
        line_clean = line.strip()
        if not line_clean:
            continue

        for pattern, group_idx in page_patterns:
            match = re.search(pattern, line_clean, re.IGNORECASE)
            if match:
                try:
                    page_num = int(match.group(group_idx))
                    # 简单验证：页码应该在合理范围内（1-999）
                    if 1 <= page_num <= 999:
                        logger.debug(f"[PDF] 提取到逻辑页码: {page_num} (模式: {pattern})")
                        return page_num
                except (ValueError, IndexError):
                    continue

    # 如果页脚没找到，搜索整个文本（但优先级较低）
    whole_text = text.strip()
    for pattern, group_idx in page_patterns:
        matches = list(re.finditer(pattern, whole_text, re.IGNORECASE))
        if matches:
            # 取最后一个匹配（通常页脚在文档末尾）
            match = matches[-1]
            try:
                page_num = int(match.group(group_idx))
                if 1 <= page_num <= 999:
                    logger.debug(f"[PDF] 从全文提取逻辑页码: {page_num}")
                    return page_num
            except (ValueError, IndexError):
                continue

    return None
