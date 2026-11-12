"""
PDF marker 兜底的调用次数契约。

回归背景：marker 兜底块曾被写在按页循环内部，于是
1) _detect_likely_table_pdf 对同一份文档重复扫描 N 次（62 页样张多花 4.5s，O(N²)）；
2) 一旦命中，每页都会追加一份内容相同的 parser="marker" 文档；
3) 循环内读到的 extracted_table 只是"前 k 页是否提取到表格"的半成品，
   含表格的 PDF 会在前几页各触发一次 marker。

这里用调用计数把"每份文档至多判定一次、至多追加一次"锁死，与耗时无关。
"""

import pymupdf
import pytest

from app.ingestion import loader

PAGES = 4


@pytest.fixture
def text_only_pdf(tmp_path):
    """生成纯文本多页 PDF：三级表格提取必然失败，从而进入 marker 兜底判定。"""
    path = tmp_path / "text-only-4-pages.pdf"
    doc = pymupdf.open()
    for i in range(PAGES):
        page = doc.new_page()
        page.insert_text(
            (72, 100),
            f"Page {i + 1}: internal knowledge base handbook, plain paragraph text only.",
        )
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def calls(monkeypatch):
    recorded = {"detect": 0, "convert": 0}

    def fake_detect(doc):
        recorded["detect"] += 1
        return True

    def fake_convert(file_path, timeout=900):
        recorded["convert"] += 1
        return "# marker\n\n| 列A | 列B |\n| --- | --- |\n| 1 | 2 |"

    monkeypatch.setattr(loader, "_detect_likely_table_pdf", fake_detect)
    monkeypatch.setattr(loader, "_convert_pdf_with_marker", fake_convert)
    monkeypatch.setattr(loader, "USE_MARKER_FOR_PDF", True)
    return recorded


def test_suspicion_detection_runs_once_per_document(text_only_pdf, calls):
    loader.load_pdf(text_only_pdf)
    assert calls["detect"] == 1, (
        f"疑似表格检测应是文档级一次，实际调用 {calls['detect']} 次（文档 {PAGES} 页）"
    )
    assert calls["convert"] == 1


def test_marker_document_appended_exactly_once(text_only_pdf, calls):
    docs = loader.load_pdf(text_only_pdf)
    page_docs = [d for d in docs if d.metadata.get("type") == "page"]
    marker_docs = [d for d in docs if d.metadata.get("parser") == "marker"]

    assert len(page_docs) == PAGES
    assert len(marker_docs) == 1, f"marker 文档应只追加一份，实际 {len(marker_docs)} 份"
    assert docs[-1] is marker_docs[0], "marker 文档应在全部按页文档之后追加"
    assert marker_docs[0].metadata["type"] == "markdown"
    assert "| 列A | 列B |" in marker_docs[0].text


def test_switch_off_skips_detection_entirely(text_only_pdf, calls, monkeypatch):
    monkeypatch.setattr(loader, "USE_MARKER_FOR_PDF", False)
    docs = loader.load_pdf(text_only_pdf)

    assert calls["detect"] == 0, "开关关闭时不该做启发式扫描"
    assert calls["convert"] == 0
    assert not [d for d in docs if d.metadata.get("parser") == "marker"]
