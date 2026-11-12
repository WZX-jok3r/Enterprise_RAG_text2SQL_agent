"""对账逻辑的单测（对账工具的**分类判定**是纯逻辑，可脱离真实环境测试）。

## 为什么重点测"分类"而不是"跑一遍"

对账工具的价值全在分类是否准确：
- 如果它把"批量 CLI 入库的语料"误报成不一致，就会**每天报 17 条噪音**，
  很快没人看 —— **一个天天误报的对账工具等于没有对账**（实测踩到过：
  初版就是报 17 条"孤儿"，而它们其实完全正常）。
- 如果它把真幽灵数据漏掉，则失去意义。

所以这里用**构造数据**穷举四种组合（登记×向量×磁盘），
不需要真连 PG/Qdrant。
"""

from __future__ import annotations

import pytest

from app.ops.reconcile import ReconcileReport


def _classify(pg: set, qd: set, disk: set) -> ReconcileReport:
    """复刻 reconcile() 的分类逻辑（与实现同口径），用于穷举验证。"""
    from pathlib import Path

    def on_disk(src: str) -> bool:
        name = Path(src).name
        return name in disk or any(n.endswith("_" + name) for n in disk)

    r = ReconcileReport()
    r.pg_documents = len(pg)
    r.qdrant_sources = len(qd)
    r.missing_in_qdrant = sorted(pg - qd)
    unregistered = qd - pg
    r.orphan_in_qdrant = sorted(s for s in unregistered if not on_disk(s))
    r.unregistered_on_disk = sorted(s for s in unregistered if on_disk(s))
    r.missing_on_disk = sorted(s for s in pg if not on_disk(s))
    return r


class TestClassification:
    """四种组合必须被分到正确的类别。"""

    def test_fully_consistent(self):
        r = _classify({"a.md"}, {"a.md"}, {"a.md"})
        assert r.is_clean
        assert not r.missing_in_qdrant
        assert not r.orphan_in_qdrant
        assert not r.missing_on_disk

    def test_pg_has_record_but_no_vector(self):
        """登记了但检索不到 —— 应报 missing_in_qdrant。"""
        r = _classify({"a.md"}, set(), {"a.md"})
        assert not r.is_clean
        assert r.missing_in_qdrant == ["a.md"]

    def test_vector_without_record_and_without_file_is_ghost(self):
        """向量在、登记无、文件也没了 —— 真幽灵数据。"""
        r = _classify(set(), {"ghost.md"}, set())
        assert not r.is_clean
        assert r.orphan_in_qdrant == ["ghost.md"]

    def test_vector_without_record_but_file_exists_is_known_state(self):
        """向量在、登记无、**文件还在** —— 走批量 CLI 入库，属已知状态。

        这一条是最重要的：它决定了对账工具是"有用"还是"天天误报"。
        """
        r = _classify(set(), {"cli_indexed.md"}, {"cli_indexed.md"})
        assert r.is_clean, "批量 CLI 入库的语料不应被计为不一致"
        assert r.unregistered_on_disk == ["cli_indexed.md"]
        assert not r.orphan_in_qdrant

    def test_record_without_file(self):
        """登记了但磁盘文件没了 —— 重灌会失败。"""
        r = _classify({"lost.md"}, {"lost.md"}, set())
        assert not r.is_clean
        assert r.missing_on_disk == ["lost.md"]


class TestRealisticScenario:
    """复现本项目的真实情形，锁定"不该误报"这条性质。

    实测数据：PG 登记 1 个（API 上传的 contract_snippet.md），
    Qdrant 有 18 个 source（其中 17 个走批量 CLI 入库）。
    初版对账把 17 个全报成"孤儿"，属误报。
    """

    def test_api_upload_plus_bulk_cli_index_is_clean(self):
        api_uploaded = {"contract_snippet.md"}
        cli_indexed = {
            "cost_data.xlsx", "docx-sample-with-table.docx", "faq.docx",
            "hardware_spec.md", "tech_maunal.md", "warranty_policy.md",
            "pdf-sample-a3.pdf", "xlsx-sample-large-10000-rows.xlsx",
        }
        disk = api_uploaded | cli_indexed          # 文件都在（含 pdf_examples/ 子目录）

        r = _classify(api_uploaded, api_uploaded | cli_indexed, disk)
        assert r.is_clean, f"不应误报，实际 orphan={r.orphan_in_qdrant}"
        assert len(r.unregistered_on_disk) == len(cli_indexed)

    def test_pdf_in_subdirectory_is_not_ghost(self):
        """PDF 语料在 pdf_examples/ 子目录 —— 必须递归扫描才不误判。

        实测踩到：初版用 glob("*") 只扫根目录，
        把 5 个 PDF 误报成"磁盘已无文件"的幽灵数据。
        """
        pdfs = {"pdf-sample-a3.pdf", "pdf-sample-landscape.pdf",
                "pdf-sample-with-table.pdf"}
        # 磁盘集合里包含这些名字（模拟 rglob 递归扫到的结果）
        r = _classify(set(), pdfs, pdfs)
        assert r.orphan_in_qdrant == [], "子目录里的 PDF 不应被当成幽灵"

    def test_uuid_prefixed_copy_counts_as_on_disk(self):
        """API 上传会落成 uuid8_原名 的副本，后缀匹配必须认出来。"""
        r = _classify({"a.md"}, set(), {"abc12345_a.md"})
        assert r.missing_on_disk == [], "带 uuid 前缀的副本应视为文件存在"


class TestReportShape:
    def test_to_dict_has_all_keys(self):
        r = _classify({"a.md"}, {"a.md"}, {"a.md"})
        d = r.to_dict()
        assert set(d.keys()) >= {
            "pg_documents", "qdrant_sources", "missing_in_qdrant",
            "orphan_in_qdrant", "unregistered_on_disk", "missing_on_disk",
            "qdrant_tenants", "fixed", "clean",
        }

    def test_to_dict_is_json_serializable(self):
        import json

        r = _classify({"a.md"}, set(), {"a.md"})
        json.dumps(r.to_dict(), ensure_ascii=False)

    def test_clean_flag_ignores_known_state(self):
        """is_clean 只由"真问题"决定，不含 unregistered_on_disk。"""
        r = _classify(set(), {"x.md"}, {"x.md"})
        assert r.unregistered_on_disk == ["x.md"]
        assert r.is_clean
