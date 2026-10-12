import pytest
from pydantic import ValidationError

from eval.quality_metrics import citation_ids, diagnostics, response_hash
from eval.quality_models import Context, GoldEvidence, QualityCase, ResponseRecord


def case():
    return QualityCase(id="q1", question="V1.0 质保多久？", category="version",
                       answerable=True, reference_answer="24个月",
                       gold_evidence=[GoldEvidence(source="policy.md", quote="V1.0质保24个月")],
                       required_facts=["24个月"])


def test_same_source_wrong_passage_does_not_count_as_evidence():
    r = ResponseRecord(id="q1", answer="质保24个月【参考1】",
                       contexts=[Context(id=1, source="policy.md", text="电源为12V")])
    d = diagnostics(case(), r)
    assert d["evidence_recall"] == 0
    assert d["citation_id_validity"] == 1  # Format validity is NOT support.
    assert "correctness" not in d and "faithfulness" not in d


def test_keyword_overlap_is_only_diagnostic():
    r = ResponseRecord(id="q1", answer="不是24个月，而是36个月。")
    d = diagnostics(case(), r)
    assert d["fact_coverage_diagnostic"] == 1
    assert "passed" not in d


def test_evidence_requires_source_and_quote_and_reports_invalid_citation():
    r = ResponseRecord(id="q1", answer="24个月【参考2】",
                       contexts=[Context(id=1, source="other.md", text="V1.0质保24个月")])
    d = diagnostics(case(), r)
    assert d["evidence_recall"] == 0
    assert d["invalid_citation_ids"] == [2]


def test_reference_number_formats_and_hash_binding():
    assert citation_ids("【参考1】[参考2][3]，24个月") == {1, 2, 3}
    a = ResponseRecord(id="q1", answer="24个月")
    b = ResponseRecord(id="q1", answer="36个月")
    assert response_hash(a) != response_hash(b)


def test_invalid_dataset_and_duplicate_context_ids_fail_closed():
    with pytest.raises(ValidationError):
        QualityCase(id="q", question="q", category="x", answerable=True, reference_answer="a")
    with pytest.raises(ValidationError):
        ResponseRecord(id="q", answer="a", contexts=[
            Context(id=1, source="a", text="a"), Context(id=1, source="b", text="b")])
    with pytest.raises(ValidationError):
        Context(id="1", source="a", text="a")


def test_no_knowledge_metrics_are_not_zero_or_perfect_by_default():
    q = QualityCase(id="q", question="未收录？", category="no_knowledge", answerable=False,
                    reference_answer="应说明资料不足")
    r = ResponseRecord(id="q", answer="根据现有资料无法回答。")
    d = diagnostics(q, r)
    assert d["evidence_recall"] is None and d["citation_id_validity"] is None
