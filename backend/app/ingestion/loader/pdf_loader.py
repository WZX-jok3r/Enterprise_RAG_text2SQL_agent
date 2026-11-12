"""
loader.pdf_loader - PDF 解析编排

每页一个 Document（含物理/逻辑双页码系统），末尾按需追加一份
parser="marker" 的 Markdown 文档作为无线表格兜底。
从 loader.py 迁出，函数体逐字未改。
"""

from pathlib import Path
from typing import List

from app.core.config import settings

from ._compat.pdf import (
    HAS_PDFPLUMBER,
    HAS_PYMUPDF,
    PYMUPDF_GET_TABLES_AVAILABLE,
    _pdfplumber_version,
    pdfplumber,
    pymupdf,
)
from .base import Document, logger
from .pdf_marker import _convert_pdf_with_marker
from .pdf_page import (
    _try_ocr_page,
    detect_columns,
    extract_logical_page_number,
    preprocess_page,
    reorder_text_blocks_by_column,
)
from .pdf_tables import _detect_likely_table_pdf, extract_tables_from_page

# marker 兜底开关：必须与读取它的 load_pdf 同模块。
# 拆包后若把快照留在门面、由 load_pdf 隔着模块读，monkeypatch 门面同名变量会
# 静默失效（函数读的是自己 __globals__）。门面只做别名再导出。
USE_MARKER_FOR_PDF = settings.use_marker_for_pdf



def load_pdf(file_path: Path) -> List[Document]:
    """
    加载 .pdf 文件（优化版 v4）+ 双页码系统支持

    优化点：
    1. 页面旋转校正 - 处理横向/旋转页面
    2. 表格结构化提取（Markdown格式）- 保留行列关系
       - 优先使用 pdfplumber（精度最高，兼容多版本）
       - 降级到 PyMuPDF get_tables()（>= 1.23.0，hasattr检测）
       - 再降级到 dict 模式兜底
    3. 双栏布局检测与文本重排 - 解决多栏排版文字交错
    4. A3/横向等特殊纸张适配 - 记录页面元信息
    5. OCR兜底（扫描件）- 处理图片型PDF
    6. 【v4新增】多页长文本页码信息注入 - text内容前加【第X页/Y】前缀
    7. 修复 fitz 弃用警告
    8. 修复 pdfplumber options 参数兼容性问题
    9. 修复 get_tables() 检测方法
    10. 降低冗余日志级别 - 每页降级日志改为DEBUG，避免INFO刷屏
    11. 【v5新增】双页码系统 - 支持物理页码和逻辑页码
    12. 【perf】pdfplumber 文档句柄整份文件复用一份
        （逐页重开会让 pdfminer 的文档解析从 O(N) 退化成 O(N²)）

    参数:
        file_path: PDF 文件路径
    返回:
        List[Document]: 解析后的文档列表，每页一个 Document
    """
    logger.info(f"[PDF] 解析: {file_path.name}")

    # 打印环境信息，便于排查
    if HAS_PYMUPDF:
        ver = getattr(pymupdf, "__version__", "unknown")
        logger.info(f"[PDF] PyMuPDF版本: {ver}, get_tables可用: {PYMUPDF_GET_TABLES_AVAILABLE}")
    if HAS_PDFPLUMBER:
        logger.info(f"[PDF] pdfplumber 已安装 (v{_pdfplumber_version})，将优先使用其进行表格提取")
    if not HAS_PDFPLUMBER:
        logger.warning("[PDF] pdfplumber 未安装！建议安装以获得更好的表格提取效果: pip install pdfplumber")

    documents = []
    pdfplumber_doc = None  # 放在 try 之外，保证 finally 能关闭任一分支打开的句柄
    try:
        doc = pymupdf.open(file_path)
        total_pages = len(doc)
        extracted_table = False  # 标记整篇是否成功提取到表格

        # pdfplumber 文档句柄：整份文件只 open 一次。
        # pdfminer 的文档结构解析是这部分的主要开销（62 页样张实测 166ms/页），
        # 逐页重开等于把 O(N) 的解析做成 O(N²)。open 失败只警告一次，
        # 后续页面以 pdf=None 跳过该策略，避免每页重试一个注定失败的 open。
        if HAS_PDFPLUMBER:
            try:
                pdfplumber_doc = pdfplumber.open(file_path)
            except Exception as e:
                logger.warning(f"[PDF] pdfplumber 打开失败，本次解析跳过该策略: {e}")

        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            # Step 1: 页面预处理
            page_info = preprocess_page(page)
            page_contents = []

            if page_info["need_ocr"]:
                # 扫描件/图片型PDF -> OCR兜底
                logger.info(f"[PDF] 页面{page_num + 1}检测到图片型内容，尝试OCR")
                ocr_text = _try_ocr_page(page)
                if ocr_text:
                    page_contents.append({
                        "type": "text",
                        "content": ocr_text,
                    })
                else:
                    logger.warning(f"[PDF] 页面{page_num + 1}OCR失败")
            else:
                # 普通PDF -> 多策略提取
                # 2a: 提取表格（渐进式降级：pdfplumber -> get_tables -> dict）
                tables = extract_tables_from_page(page, file_path, page_num, pdf=pdfplumber_doc)
                if tables:
                    extracted_table = True
                for table_info in tables:
                    page_contents.append({
                        "type": "table",
                        "content": table_info["content"],
                    })

                # 2b: 提取正文文本
                text_dict = page.get_text("dict")
                blocks = text_dict.get("blocks", [])
                # 过滤掉表格块（type=1为表格块）
                text_blocks = [b for b in blocks if b.get("type") == 0]
                if not text_blocks:
                    text = page.get_text("text").strip()
                    if text:
                        page_contents.append({"type": "text", "content": text})
                else:
                    # 2c: 双栏检测与重排
                    columns = detect_columns(page)
                    ordered_blocks = reorder_text_blocks_by_column(text_blocks, columns)
                    paragraphs = []
                    for block in ordered_blocks:
                        lines = []
                        for line in block.get("lines", []):
                            line_text = ""
                            for span in line.get("spans", []):
                                line_text += span.get("text", "")
                            lines.append(line_text)
                        para_text = "\n".join(lines).strip()
                        if para_text:
                            paragraphs.append(para_text)
                    if paragraphs:
                        page_contents.append({
                            "type": "text",
                            "content": "\n\n".join(paragraphs),
                        })

            # Step 2: 组装页面内容 + 注入页码信息（v4 新增） + 双页码系统（v5 新增）
            if page_contents:
                table_parts = [c["content"] for c in page_contents if c["type"] == "table"]
                text_parts = [c["content"] for c in page_contents if c["type"] == "text"]
                combined_parts = table_parts + text_parts
                combined_text = "\n\n".join(combined_parts)

                if combined_text.strip():
                    # 【v5 新增】双页码系统：提取逻辑页码
                    logical_page = extract_logical_page_number(combined_text)
                    has_logical_page = logical_page is not None
                    display_page = logical_page if has_logical_page else (page_num + 1)

                    # 确定使用的页码标签（优先使用逻辑页码）
                    if has_logical_page:
                        page_num_label = f"【第{display_page}页(逻辑)/{total_pages}页】"
                        page_note = f"文档标注页码:{logical_page}, 文件物理页码:{page_num + 1}"
                    else:
                        page_num_label = f"【第{display_page}页(物理)/{total_pages}页】"
                        page_note = f"文件物理页码:{page_num + 1}, 未找到内容标注页码"

                    annotated_text = page_num_label + "\n" + page_note + "\n" + combined_text.strip()

                    # 构建双页码元数据
                    metadata = {
                        "source": file_path.name,
                        "full_path": str(file_path),
                        "format": "pdf",
                        "type": "page",
                        "page_number": display_page,  # 显示页码（优先逻辑页码）
                        "total_pages": total_pages,
                        "is_landscape": page_info["is_landscape"],
                        "page_width": page_info["page_width"],
                        "page_height": page_info["page_height"],
                        # v5 新增：双页码系统字段
                        "physical_page": page_num + 1,  # 物理页码（文件结构）
                        "logical_page": logical_page,  # 逻辑页码（内容标注）
                        "has_logical_page": has_logical_page,
                        "page_numbering_system": "logical" if has_logical_page else "physical",
                        "page_numbering_source": "content_marker" if has_logical_page else "file_structure",
                        "page_label": page_num_label.replace("【", "").replace("】", ""),
                    }

                    documents.append(Document(
                        text=annotated_text,
                        metadata=metadata,
                    ))

        # === marker 深度表格识别兜底（文档级判定，每份 PDF 至多一次） ===
        # 必须放在页循环之外，三个理由：
        # 1) 这是文档级决策，与本页无关。放在循环里会让 _detect_likely_table_pdf
        #    对同一份文档重复全量扫描 N 次 → O(N²)（62 页样张实测多花 4.5s）；
        # 2) extracted_table 是跨页累加的标志，只有循环结束后才是
        #    “整篇是否提取到表格”的真值。循环内读它得到的是“前 k 页是否提取到”，
        #    导致含表格的 PDF 在前几页各触发一次 marker，
        #    追加多份内容相同的 parser="marker" 文档（真实数据重复，不只是变慢）；
        # 3) marker 转换本身是整文件级子进程调用，没有“按页做”的语义。
        # 触发条件：三级表格提取全失败 + 疑似含无线/多列表格 + 开关开启。
        # marker 结果作为独立 Document(parser="marker") 追加（不删除按页结果），
        # 保证页码类问题仍可由按页块命中；表格类问题由 marker 的 Markdown 块命中。
        # chunk_strategy 已预留路由：parser=="marker" → 自动走 md 表格感知切分。
        if (not extracted_table and USE_MARKER_FOR_PDF
                and _detect_likely_table_pdf(doc)):
            logger.info(f"[PDF] 疑似无线表格且三级提取失败，尝试 marker 深度识别: {file_path.name}")
            md_text = _convert_pdf_with_marker(file_path)
            if md_text:
                documents.append(Document(
                    text=md_text,
                    metadata={
                        "source": file_path.name,
                        "full_path": str(file_path),
                        "format": "pdf",
                        "type": "markdown",
                        "parser": "marker",
                        "total_pages": total_pages,
                    },
                ))
                logger.info(f"[PDF] marker 转换成功，追加 Markdown 文档块: {file_path.name}")
            else:
                logger.info(f"[PDF] marker 未安装或转换失败，保持原有按页解析结果")

        doc.close()

        if not documents:
            logger.warning(f"[PDF] 未提取到文本内容（可能是扫描件/图片PDF）: {file_path.name}")

        # PDF 解析统计汇总
        logger.info(
            f"[PDF] 解析完成: {file_path.name} | "
            f"总页数: {total_pages} | "
            f"成功提取文档块: {len(documents)}"
        )
        return documents

    except Exception as e:
        logger.error(f"[PDF] 解析失败 {file_path.name}: {e}")
        return []
    finally:
        # 必须在所有分支关闭：Windows 下未关闭的句柄会锁住文件，
        # 导致后续入库/搬移操作报 PermissionError。
        if pdfplumber_doc is not None:
            try:
                pdfplumber_doc.close()
            except Exception:
                pass
