"""
pdfplumber 句柄复用契约（性能优化不得改变提取结果，也不得泄漏文件句柄）。

背景：extract_tables_with_pdfplumber 原先是每页 pdfplumber.open() 一次。pdfminer 的
文档结构解析是这部分的主要开销，逐页重开把 O(N) 的解析做成了 O(N²)
（62 页样张实测 10.3s → 2.8s）。这里用调用计数锁死三件事：

1. 每份 PDF 只 open 一次；
2. 解析结束必然 close（Windows 下句柄不关会锁住文件）；
3. open 失败只试一次 —— 后续页面跳过该策略，而不是每页重试一个注定失败的 open。

用计数而不是耗时断言，避免机器/负载导致 flaky。
"""

import pymupdf
import pytest

from app.ingestion.loader import pdf_loader, pdf_tables

PAGES = 6


@pytest.fixture
def text_only_pdf(tmp_path):
    """纯文本多页 PDF：pdfplumber 会逐页执行但提取不到表格。"""
    path = tmp_path / "plain-6-pages.pdf"
    doc = pymupdf.open()
    for i in range(PAGES):
        page = doc.new_page()
        page.insert_text((72, 100), f"Page {i + 1}: plain paragraph, no ruling lines at all.")
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def open_counter(monkeypatch):
    """统计 pdfplumber.open 次数，并给返回的句柄包一层 close 计数。"""
    recorded = {"open": 0, "close": 0}
    real_open = pdf_tables.pdfplumber.open

    def counting_open(*args, **kwargs):
        recorded["open"] += 1
        document = real_open(*args, **kwargs)
        real_close = document.close

        def counting_close():
            recorded["close"] += 1
            return real_close()

        document.close = counting_close
        return document

    # pdf_tables.pdfplumber 与 pdf_loader.pdfplumber 是同一个模块对象，
    # 因此这一处 patch 对两条调用路径同时生效。
    monkeypatch.setattr(pdf_tables.pdfplumber, "open", counting_open)
    return recorded


def test_handle_opened_once_per_document(text_only_pdf, open_counter):
    pdf_loader.load_pdf(text_only_pdf)

    assert open_counter["open"] == 1, (
        f"整份文件应复用一份 pdfplumber 句柄，实际 open {open_counter['open']} 次（{PAGES} 页）"
    )


def test_handle_closed_after_parse(text_only_pdf, open_counter):
    pdf_loader.load_pdf(text_only_pdf)

    assert open_counter["close"] == 1, "句柄必须在解析结束时关闭"
    # 行为层佐证：句柄没关时 Windows 会锁住文件，重命名直接 PermissionError。
    text_only_pdf.replace(text_only_pdf.with_name("renamed.pdf"))


def test_open_failure_is_not_retried_per_page(text_only_pdf, monkeypatch):
    attempts = {"n": 0}

    def boom(*args, **kwargs):
        attempts["n"] += 1
        raise OSError("simulated pdfplumber failure")

    monkeypatch.setattr(pdf_tables.pdfplumber, "open", boom)
    docs = pdf_loader.load_pdf(text_only_pdf)

    assert attempts["n"] == 1, f"open 失败后不应逐页重试，实际尝试 {attempts['n']} 次"
    assert len(docs) == PAGES, "pdfplumber 不可用时应降级到后续策略，而不是丢页"


def test_standalone_call_keeps_auto_open_behaviour(text_only_pdf, open_counter):
    """不传句柄时保持单页调用的旧行为：自己打开、自己关闭。"""
    tables = pdf_tables.extract_tables_with_pdfplumber(text_only_pdf, 0)

    assert tables == []
    assert (open_counter["open"], open_counter["close"]) == (1, 1)


def test_explicit_none_skips_strategy_without_opening(text_only_pdf, open_counter):
    """调用方明确传 None 表示"本文件没有句柄"，应直接跳过而不是偷偷 open。"""
    tables = pdf_tables.extract_tables_with_pdfplumber(text_only_pdf, 0, pdf=None)

    assert tables == []
    assert open_counter["open"] == 0
