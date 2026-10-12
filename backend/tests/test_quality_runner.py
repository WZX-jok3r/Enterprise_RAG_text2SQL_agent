import json

import pytest

from eval.quality import CapturingRetriever, capture, dump_jsonl, evaluate, load_jsonl, main
from eval.quality_judge import HumanReview, ReviewedJudge
from eval.quality_metrics import response_hash
from eval.quality_models import Context, GoldEvidence, QualityCase, QualityVerdict, ResponseRecord


def sample():
    case = QualityCase(id="q", question="质保多久？", category="fact", answerable=True,
                       reference_answer="24个月", gold_evidence=[GoldEvidence(source="a", quote="24个月")])
    record = ResponseRecord(id="q", answer="24个月【参考1】", contexts=[Context(id=1, source="a", text="24个月")])
    review = HumanReview(id="q", reviewer="fixture", response_sha256=response_hash(record),
                        verdict=QualityVerdict(correctness="correct", refusal="none", reason="一致",
                            claims=[{"text": "24个月", "verdict": "supported", "evidence_ids": [1], "reason": "支持"}],
                            citations=[{"context_id": 1, "supported": True, "reason": "支持"}]))
    return case, record, review


def test_full_pass_and_missing_case_cannot_shrink_denominator():
    c, r, v = sample()
    judge = ReviewedJudge([v], fixture=True)
    assert evaluate([c], [r], judge)["summary"]["gate_passed"]
    c2 = c.model_copy(update={"id": "missing"})
    report = evaluate([c, c2], [r], judge)
    assert report["summary"]["pass_rate"] == 0.5
    assert report["summary"]["errors"] == 1
    assert report["summary"]["correctness"]["coverage"] == 0.5
    assert not report["summary"]["gate_passed"]


def test_without_semantic_judgment_no_accuracy_or_gate_pass():
    c, r, _ = sample()
    s = evaluate([c], [r])["summary"]
    assert s["unjudged"] == 1 and s["correctness"]["mean"] is None
    assert s["false_refusal_rate"] is None
    assert not s["gate_passed"]


def test_provider_failure_is_error_not_correct_refusal():
    c, r, v = sample()
    r.error = "provider failed"
    s = evaluate([c], [r], ReviewedJudge([v]))["summary"]
    assert s["errors"] == 1 and s["judged"] == 0
    assert s["passed"] == 0
    assert s["false_refusal_rate"] is None


def test_uncited_answers_fail_default_policy():
    c, r, v = sample()
    r.answer = "24个月"
    v.response_sha256 = response_hash(r)
    v.verdict.citations = []
    judge = ReviewedJudge([v])
    assert not evaluate([c], [r], judge)["summary"]["gate_passed"]
    assert evaluate([c], [r], judge, require_citations=False)["summary"]["gate_passed"]


def test_response_hash_mismatch_is_an_error_in_report():
    c, r, v = sample()
    r.answer = "36个月"
    s = evaluate([c], [r], ReviewedJudge([v]))["summary"]
    assert s["errors"] == 1 and not s["gate_passed"]


def test_duplicate_or_extra_response_rejected():
    c, r, _ = sample()
    with pytest.raises(ValueError):
        evaluate([c], [r, r])
    with pytest.raises(ValueError):
        evaluate([c], [r.model_copy(update={"id": "extra"})])


def test_strict_loader_and_cli_exit_codes(tmp_path):
    c, r, v = sample()
    dataset, responses, reviews, out = [tmp_path / x for x in ["cases.jsonl", "responses.jsonl", "reviews.jsonl", "report.json"]]
    dump_jsonl(dataset, [c]); dump_jsonl(responses, [r]); dump_jsonl(reviews, [v])
    args = ["--dataset", str(dataset), "--responses", str(responses), "--output", str(out)]
    assert main(args) == 2  # unjudged is not a successful quality run
    assert main(args + ["--reviews", str(reviews), "--fixture-reviews"]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["judge"]["kind"] == "calibration_fixture"
    v.verdict.correctness = "incorrect"
    dump_jsonl(reviews, [v])
    assert main(args + ["--reviews", str(reviews)]) == 1
    dataset.write_text(dataset.read_text(encoding="utf-8") + "{broken\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid"):
        load_jsonl(dataset, QualityCase)


def test_capture_uses_full_context_and_single_retrieval():
    from app.rag.pipeline import RAGPipeline
    class Retriever:
        calls = 0
        def search(self, query, top_k=None, filter_dict=None):
            self.calls += 1
            return [{"text": "x" * 250 + "关键证据24个月", "metadata": {"source": "a"}, "score": 0.9}], 0
    class LLM:
        model = "fixture"
        def complete(self, messages):
            return "24个月【参考1】", {}
    c, _, _ = sample()
    source = Retriever()
    recorder = CapturingRetriever(source)
    pipeline = RAGPipeline(retriever=recorder, llm=LLM())
    records = capture([c], pipeline, recorder, 5)
    assert source.calls == 1
    assert records[0].contexts[0].text.endswith("关键证据24个月")
    assert len(records[0].contexts[0].text) > 200
