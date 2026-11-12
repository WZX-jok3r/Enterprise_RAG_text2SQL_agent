"""
loader.pdf_tables - PDF 表格提取的三级降级链

pdfplumber（精度最高）→ PyMuPDF get_tables()（>= 1.23，hasattr 实测）→ dict 模式兜底。
另含 _detect_likely_table_pdf：判定是否值得交给 marker 做深度识别（文档级，每次解析
只应调用一次 —— 它对全文逐页做 x0 聚类，放在页循环里会退化成 O(N²)）。
从 loader.py 迁出，函数体逐字未改。
"""

from pathlib import Path
from typing import Dict, List

from ._compat.pdf import HAS_PDFPLUMBER, PYMUPDF_GET_TABLES_AVAILABLE, pdfplumber
from .base import _data_to_markdown_table, logger



def extract_tables_with_pdfplumber(file_path: Path, page_num: int) -> List[Dict]:
    """
    首选方案：使用 pdfplumber 提取表格（精度最高）
    pdfplumber 基于 PDF 的绘图指令提取表格，对表格结构识别最准确

    兼容处理：不同版本的 pdfplumber 参数名可能不同
    - 旧版: extract_tables() 无参数
    - 新版: extract_tables(table_settings={...})
    """
    if not HAS_PDFPLUMBER:
        return []
    tables = []
    try:
        with pdfplumber.open(file_path) as pdf:
            if page_num >= len(pdf.pages):
                return []
            page = pdf.pages[page_num]

            # 尝试不同的参数方式调用 extract_tables
            extracted_tables = None
            try:
                # 方式1: 不带参数（最兼容）
                extracted_tables = page.extract_tables()
            except TypeError:
                try:
                    # 方式2: table_settings 参数（新版 pdfplumber）
                    extracted_tables = page.extract_tables(
                        table_settings={
                            "vertical_strategy": "text",
                            "horizontal_strategy": "text",
                        }
                    )
                except (TypeError, AttributeError):
                    try:
                        # 方式3: options 参数（中间版本）
                        extracted_tables = page.extract_tables(
                            options={
                                "vertical_strategy": "text",
                                "horizontal_strategy": "text",
                            }
                        )
                    except (TypeError, AttributeError):
                        # 方式4: 什么都不传，用默认设置
                        extracted_tables = page.extract_tables()

            if extracted_tables:
                for table_data in extracted_tables:
                    if table_data and len(table_data) > 0:
                        # 清理单元格内容
                        cleaned_table = []
                        for row in table_data:
                            cleaned_row = [
                                str(cell).strip() if cell is not None else ""
                                for cell in row
                            ]
                            cleaned_table.append(cleaned_row)
                        md_table = _data_to_markdown_table(cleaned_table)
                        tables.append({
                            "type": "table",
                            "content": md_table,
                            "rows": len(cleaned_table),
                            "cols": len(cleaned_table[0]) if cleaned_table else 0,
                            "method": "pdfplumber",
                        })
    except Exception as e:
        logger.warning(f"[PDF] pdfplumber 表格提取失败: {e}")
    return tables


def extract_tables_with_get_tables(page) -> List[Dict]:
    """
    备选方案：使用 PyMuPDF >= 1.23.0 的 page.get_tables() 提取表格

    注意：使用 hasattr 实际检测，而非仅依赖版本号
    """
    tables = []
    if not hasattr(page, 'get_tables') or not callable(page.get_tables):
        logger.debug("[PDF] page.get_tables() 方法不存在，跳过")
        return tables

    try:
        page_tables = page.get_tables()
        if page_tables:
            for table in page_tables:
                data = table.extract()
                if data and len(data) > 0:
                    md_table = _data_to_markdown_table(data)
                    tables.append({
                        "type": "table",
                        "content": md_table,
                        "rows": len(data),
                        "cols": len(data[0]) if data else 0,
                        "method": "pymupdf_get_tables",
                    })
    except AttributeError as e:
        logger.debug(f"[PDF] get_tables() 调用失败(AttributeError): {e}")
    except Exception as e:
        logger.warning(f"[PDF] get_tables() 提取失败: {e}")
    return tables


def extract_tables_with_dict(page) -> List[Dict]:
    """
    兜底方案：通过 dict 模式提取表格（兼容旧版本 PyMuPDF）
    通过文本块的坐标信息检测表格结构

    改进版：使用坐标聚类检测网格状表格结构
    - 先按 y 坐标聚类分行
    - 再按 x 坐标聚类分列
    - 构建二维矩阵
    """
    tables = []
    try:
        text_dict = page.get_text("dict")
        blocks = text_dict.get("blocks", [])

        # 收集所有文本块的坐标和内容信息
        text_blocks = []
        for block in blocks:
            if block.get("type") == 0:  # 文本块
                rect = block.get("rect", None)
                if rect is not None:
                    block_text = ""
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            block_text += span.get("text", "")
                    block_text = block_text.strip()
                    if block_text:
                        text_blocks.append({
                            "x0": rect[0],
                            "y0": rect[1],
                            "x1": rect[2],
                            "y1": rect[3],
                            "text": block_text,
                            "center_x": (rect[0] + rect[2]) / 2,
                            "center_y": (rect[1] + rect[3]) / 2,
                        })

        if len(text_blocks) < 4:
            return tables

        # Step 1: 按 y 坐标聚类分行
        y_threshold = 8.0
        sorted_by_y = sorted(text_blocks, key=lambda b: b["center_y"])

        rows = []
        current_row = [sorted_by_y[0]]
        current_y_center = sorted_by_y[0]["center_y"]

        for block in sorted_by_y[1:]:
            if abs(block["center_y"] - current_y_center) <= y_threshold:
                current_row.append(block)
            else:
                rows.append(current_row)
                current_row = [block]
                current_y_center = block["center_y"]
        if current_row:
            rows.append(current_row)

        # Step 2: 如果有至少2行且多数行有多个块，尝试检测表格结构
        if len(rows) >= 2:
            multi_block_rows = [row for row in rows if len(row) >= 2]
            if len(multi_block_rows) >= 2:
                # Step 3: 按 x 坐标聚类分列
                all_x_centers = []
                for row in rows:
                    for block in row:
                        all_x_centers.append(block["center_x"])
                all_x_centers = sorted(set(round(x, 1) for x in all_x_centers))

                # 聚类 x 坐标
                x_threshold = 10.0
                columns = []
                if all_x_centers:
                    current_col = [all_x_centers[0]]
                    current_x = all_x_centers[0]
                    for x in all_x_centers[1:]:
                        if abs(x - current_x) <= x_threshold:
                            current_col.append(x)
                        else:
                            columns.append(sum(current_col) / len(current_col))
                            current_col = [x]
                            current_x = x
                    columns.append(sum(current_col) / len(current_col))

                # Step 4: 构建表格矩阵
                if len(columns) >= 2:
                    table_data = []
                    for row in rows:
                        row_sorted = sorted(row, key=lambda b: b["center_x"])
                        table_row = [""] * len(columns)
                        for block in row_sorted:
                            best_col = 0
                            best_dist = float("inf")
                            for ci, col_x in enumerate(columns):
                                dist = abs(block["center_x"] - col_x)
                                if dist < best_dist:
                                    best_dist = dist
                                    best_col = ci
                            if best_col < len(table_row):
                                if table_row[best_col]:
                                    table_row[best_col] += " " + block["text"]
                                else:
                                    table_row[best_col] = block["text"]
                        table_data.append(table_row)

                    if table_data:
                        md_table = _data_to_markdown_table(table_data)
                        tables.append({
                            "type": "table",
                            "content": md_table,
                            "rows": len(table_data),
                            "cols": len(table_data[0]) if table_data else 0,
                            "method": "dict_fallback_improved",
                        })
    except Exception as e:
        logger.warning(f"[PDF] dict模式表格提取失败: {e}")
    return tables


def extract_tables_from_page(page, file_path: Path, page_num: int) -> List[Dict]:
    """
    表格提取主函数 - 渐进式降级策略：
    1. pdfplumber（精度最高，兼容不同版本参数）
    2. PyMuPDF get_tables()（实际hasattr检测）
    3. dict模式兜底（改进版坐标聚类）

    注意：file_path 参数仅在 pdfplumber 方案时需要
    """
    # 策略1: pdfplumber（最推荐，表格识别精度最高）
    if HAS_PDFPLUMBER:
        tables = extract_tables_with_pdfplumber(file_path, page_num)
        if tables:
            logger.info(f"[PDF] 页面{page_num + 1}通过 pdfplumber 提取到 {len(tables)} 个表格")
            return tables
        else:
            logger.debug(f"[PDF] 页面{page_num + 1} pdfplumber 未提取到表格，尝试下一策略")

    # 策略2: PyMuPDF get_tables()（实际检测方法是否存在）
    if PYMUPDF_GET_TABLES_AVAILABLE:
        tables = extract_tables_with_get_tables(page)
        if tables:
            logger.info(f"[PDF] 页面{page_num + 1}通过 pymupdf.get_tables() 提取到 {len(tables)} 个表格")
            return tables
        else:
            logger.debug(f"[PDF] 页面{page_num + 1} get_tables() 未提取到表格，尝试下一策略")

    # 策略3: dict模式兜底
    logger.debug(f"[PDF] 页面{page_num + 1} 表格提取降级到 dict 模式")
    tables = extract_tables_with_dict(page)
    if tables:
        logger.info(f"[PDF] 页面{page_num + 1} 通过 dict 模式提取到 {len(tables)} 个表格块")
    else:
        logger.debug(f"[PDF] 页面{page_num + 1} dict 模式也未提取到表格")
    return tables


def _detect_likely_table_pdf(doc) -> bool:
    """
    启发式：检测 PDF 是否疑似含"多列/无线表格"。
    依据：文本 span 的左边缘 x0 是否聚类成多个列带（>=3），且多数页面满足。
    - 普通段落文本 x0 基本一致（1 个列带）→ 不触发
    - 无线表格/多列表格 x0 聚成多个列带 → 触发 marker 兜底
    返回 True 表示疑似含表格，值得尝试 marker 深度识别。
    """
    try:
        col_band_counts = []
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            x0s = []
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        t = span.get("text", "").strip()
                        if t:
                            x0s.append(span.get("bbox", [0, 0, 0, 0])[0])
            if not x0s:
                continue
            xs = sorted(set(round(x, 1) for x in x0s))
            clusters = []
            cur = [xs[0]]
            for x in xs[1:]:
                if x - cur[-1] <= 8.0:
                    cur.append(x)
                else:
                    clusters.append(len(cur))
                    cur = [x]
            clusters.append(len(cur))
            col_band_counts.append(len(clusters))
        if not col_band_counts:
            return False
        multi = sum(1 for c in col_band_counts if c >= 3)
        return (multi / len(col_band_counts)) >= 0.5
    except Exception:
        return False
