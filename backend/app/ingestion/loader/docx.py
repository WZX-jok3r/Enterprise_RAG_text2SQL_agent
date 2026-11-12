"""
loader.docx - DOCX 解析（.docx）

- 段落文本按段落合并为一个 Document（type="paragraph"）
- 每个表格单独一个 Document（Markdown 表格）
- 内嵌图片（正文内联 + 页眉/页脚）逐个提取，OCR 成功则作为 Document(type="image")

依赖：python-docx（必需）；pytesseract + Pillow（可选，仅图片 OCR 用）。
从 loader.py 迁出，函数体逐字未改。
"""

from pathlib import Path
from typing import Any, Dict, List

from ._compat.docx import HAS_DOCX, DocxDocument
from ._compat.ocr import HAS_PIL, HAS_TESSERACT, pytesseract
from .base import Document, logger


def _extract_docx_images(doc, file_path: Path) -> List[Dict[str, Any]]:
    """
    提取 .docx 文档中内嵌的全部图片（含正文内联图 + 页眉/页脚图）

    python-docx 的 inline_shapes 只能拿到正文内联图片的计数和大小，
    拿不到图片二进制，且会遗漏页眉/页脚(header/footer)中的图片。
    正确做法是双轨采集：
      轨道1: inline_shapes 逐个取 rId（保证正文内联图按出现顺序）
      轨道2: doc.part.rels 遍历所有 image 关系（兜底补全页眉/页脚图）
    两轨去重合并，保证不同类型图片都不丢失。

    Returns:
        List[Dict]: 每项包含 {"index", "blob", "ext", "location"} 的图片信息列表
                    location: "inline"(正文内联) 或 "header_footer"(页眉/页脚) 或 "unknown"
    """
    images = []
    seen_r_ids = set()  # 去重：同一 rId 只提取一次

    # ---- 轨道1: 正文内联图（按出现顺序） ----
    try:
        shapes = doc.inline_shapes
        for idx, shape in enumerate(shapes):
            try:
                # inline shape 的 XML 路径: inline → graphic → graphicData → pic → blipFill → blip
                blip = shape._inline.graphic.graphicData.pic.blipFill.blip
                r_id = blip.embed  # 例如 "rId5"
                if r_id in seen_r_ids:
                    continue
                seen_r_ids.add(r_id)
                image_part = doc.part.related_parts[r_id]
                blob = image_part.blob
                ext = image_part.content_type.split("/")[-1] if "/" in image_part.content_type else "png"
                images.append({"index": len(images), "blob": blob, "ext": ext, "location": "inline"})
            except (KeyError, AttributeError, IndexError) as e:
                logger.debug(f"[DOCX] 跳过无法访问的内联图 {idx}: {e}")
                continue
    except Exception as e:
        logger.warning(f"[DOCX] 遍历 inline_shapes 失败: {e}")

    # ---- 轨道2: 遍历全部关系，兜底补全页眉/页脚等图片 ----
    try:
        for rel_id, rel in doc.part.rels.items():
            if "image" not in rel.reltype:
                continue
            if rel_id in seen_r_ids:
                continue
            seen_r_ids.add(rel_id)
            try:
                blob = rel.target_part.blob
                ext = rel.target_part.content_type.split("/")[-1] if "/" in rel.target_part.content_type else "png"
                # 通过 target_ref 判断位置: 页眉/页脚图片通常位于 header/footer 部件
                target_ref = getattr(rel, "target_ref", "") or ""
                if "header" in target_ref.lower() or "footer" in target_ref.lower():
                    location = "header_footer"
                else:
                    location = "unknown"
                images.append({"index": len(images), "blob": blob, "ext": ext, "location": location})
            except Exception as e:
                logger.debug(f"[DOCX] 跳过无法访问的 rel 图片 {rel_id}: {e}")
                continue
    except Exception as e:
        logger.warning(f"[DOCX] 遍历 rels 失败: {e}")

    if images:
        logger.info(f"[DOCX] 提取到 {len(images)} 张图片: {file_path.name}")
    return images


def _ocr_image_bytes(blob: bytes) -> str:
    """
    OCR 识别单张图片的二进制数据

    依赖: pytesseract + Pillow
    如果 tesseract 未安装或识别失败，返回空字符串（调用方降级处理）。
    """
    if not HAS_TESSERACT or not HAS_PIL:
        return ""
    try:
        import io
        from PIL import Image

        img = Image.open(io.BytesIO(blob))
        # 统一转 RGB（处理 PNG 透明通道/灰度图，避免 OCR 报错）
        if img.mode != "RGB":
            img = img.convert("RGB")
        # 双倍缩放提升小图/低分辨率图片的 OCR 准确率
        width, height = img.size
        img = img.resize((width * 2, height * 2), Image.LANCZOS)
        text = pytesseract.image_to_string(img, lang="chi_sim+eng")
        return text.strip()
    except Exception as e:
        logger.warning(f"[DOCX] 图片 OCR 失败: {e}")
        return ""


def load_docx(file_path: Path) -> List[Document]:
    """
    加载 .docx 文件
    - 段落文本按段落拆分，保留段落结构
    - 表格单独提取，保留行列结构（Markdown表格格式）
    - 图片单独提取，OCR 识别文字后作为独立 Document（type="image"）
    """
    logger.info(f"[DOCX] 解析: {file_path.name}")
    documents = []
    if not HAS_DOCX:
        logger.error("python-docx 未安装，请执行: pip install python-docx")
        return []
    try:
        doc = DocxDocument(file_path)
        # 1. 提取段落文本（过滤空段落）
        paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        if paragraphs:
            para_text = "\n".join(paragraphs)
            documents.append(
                Document(
                    text=para_text,
                    metadata={
                        "source": file_path.name,
                        "full_path": str(file_path),
                        "format": "docx",
                        "type": "paragraph",
                        "paragraph_count": len(paragraphs),
                    },
                )
            )
        # 2. 提取表格（每个表格作为一个独立 Document，标准 Markdown 格式）
        # v5 改进: 输出标准 Markdown 表格（表头行 + 分隔行 + 数据行），
        # 与 PDF 表格格式对齐，从而复用 chunk 层的 split_markdown_table 分行切分逻辑
        for table_idx, table in enumerate(doc.tables):
            md_lines = []
            for row_idx, row in enumerate(table.rows):
                cells = [cell.text.strip() for cell in row.cells]
                if row_idx == 0:
                    # 第一行作为表头 + 分隔行（标准 Markdown 表格结构）
                    md_lines.append("| " + " | ".join(cells) + " |")
                    md_lines.append("| " + " | ".join(["---"] * len(cells)) + " |")
                else:
                    md_lines.append("| " + " | ".join(cells) + " |")
            if md_lines:
                table_text = "\n".join(md_lines)
                documents.append(
                    Document(
                        text=table_text,
                        metadata={
                            "source": file_path.name,
                            "full_path": str(file_path),
                            "format": "docx",
                            "type": "table",
                            "table_index": table_idx,
                            "row_count": len(md_lines) - 1,  # 去掉分隔行
                            "col_count": len(table.columns),
                        },
                    )
                )
        # 3. 提取图片 + OCR（v5 P1 改进）
        # P1: 不再仅计数，而是真正提取图片二进制并 OCR 识别文字，
        # 每张图片生成独立的 Document（type="image"），便于后续切片与检索
        try:
            images = _extract_docx_images(doc, file_path)
            for img in images:
                idx = img["index"]
                ext = img["ext"]
                blob = img["blob"]
                location = img.get("location", "unknown")

                # OCR 识别图片中的文字（仅一次，结果在下方统计中复用）
                ocr_text = _ocr_image_bytes(blob)
                has_ocr = bool(ocr_text)

                if has_ocr:
                    # OCR 成功：直接使用识别出的文字作为检索内容
                    text = f"[DOCX图片{idx} OCR内容]\n{ocr_text}"
                else:
                    # OCR 失败：保留图片位置占位，避免图片信息完全丢失
                    text = f"[DOCX图片{idx}：未能识别出文字内容]"

                documents.append(
                    Document(
                        text=text,
                        metadata={
                            "source": file_path.name,
                            "full_path": str(file_path),
                            "format": "docx",
                            "type": "image",
                            "image_index": idx,
                            "image_ext": ext,
                            "image_location": location,  # inline / header_footer / unknown
                            "has_ocr": has_ocr,
                            "ocr_text": ocr_text,  # 无 OCR 则为空串
                        },
                    )
                )

            if images:
                # OCR 已在上面逐图执行过一次，此处直接复用 has_ocr 结果，避免重复 OCR 浪费算力
                ocr_count = sum(1 for d in documents if d.metadata.get("type") == "image" and d.metadata.get("has_ocr"))
                logger.info(f"[DOCX] 提取 {len(images)} 张图片，OCR成功 {ocr_count} 张: {file_path.name}")
        except Exception as e:
            logger.warning(f"[DOCX] 图片提取失败: {file_path.name}: {e}")

        if not documents:
            logger.warning(f"[DOCX] 未提取到有效内容: {file_path.name}")
        return documents
    except Exception as e:
        logger.error(f"[DOCX] 解析失败 {file_path.name}: {e}")
        return []
