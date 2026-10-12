import json
from types import SimpleNamespace

import pytest

from eval.quality_judge import HumanReview, LLMJudge, ReviewedJudge, semantic_metrics, validate_verdict
from eval.quality_metrics import response_hash
from eval.quality_models import Context, GoldEvidence, QualityCase, QualityVerdict, ResponseRecord


def sample():
    case = QualityCase(id="q", question="质保多久？", category="fact", answerable=True,
                       reference_answer="24个月", gold_evidence=[GoldEvidence(source="a", quote="24个月")])
    record = ResponseRecord(id="q", answer="质保24个月【参考1】",
                            contexts=[Context(id=1, source="a", text="质保24个月")])
    raw = {"correctness": "correct", "refusal": "none", "reason": "一致",
           "claims": [{"text": "质保24个月", "verdict": "supported", "evidence_ids": [1], "reason": "原文"}],
           "citations": [{"context_id": 1, "supported": True, "reason": "支持"}]}
    return case, record, raw


def test_correctness_and_faithfulness_are_independent():
    c, r, raw = sample()
    raw["claims"][0].update(verdict="unsupported", evidence_ids=[])
    raw["citations"][0]["supported"] = False
    m = semantic_metrics(c, r, QualityVerdict.model_validate(raw))
    assert m["correctness"] == 1 and m["faithfulness"] == 0 and m["citation_accuracy"] == 0


@pytest.mark.parametrize("change", [
    lambda d: d.update(citations=[]),
    lambda d: d["claims"][0].update(text="质保36个月"),
    lambda d: d["claims"][0].update(evidence_ids=[9]),
    lambda d: d["claims"][0].update(evidence_ids=[]),
    lambda d: d.update(claims=[]),
    lambda d: d.update(refusal="appropriate"),
])
def test_invalid_judgments_fail_closed(change):
    c, r, raw = sample()
    change(raw)
    with pytest.raises(ValueError):
        validate_verdict(c, r, QualityVerdict.model_validate(raw))


def test_hash_prevents_reusing_review_for_changed_answer():
    c, r, raw = sample()
    judge = ReviewedJudge([HumanReview(id="q", response_sha256=response_hash(r),
                                      reviewer="reviewer", verdict=QualityVerdict.model_validate(raw))])
    assert judge.judge(c, r).correctness == "correct"
    with pytest.raises(ValueError):
        judge.judge(c, r.model_copy(update={"answer": "质保36个月"}))


def test_refusal_with_hallucination_is_not_appropriate():
    c = QualityCase(id="q", question="未知设备？", category="no_knowledge", answerable=False,
                    reference_answer="资料不足")
    r = ResponseRecord(id="q", answer="无法回答，但估计36个月。")
    v = QualityVerdict(correctness="incorrect", refusal="none", reason="编造",
                       claims=[{"text": "估计36个月", "verdict": "unsupported", "evidence_ids": [], "reason": "无证据"}],
                       citations=[])
    m = semantic_metrics(c, r, v)
    assert not m["appropriate_refusal"] and m["faithfulness"] == 0


def test_llm_request_and_strict_output_without_secret_in_metadata():
    c, r, raw = sample()
    seen = {}
    def post(url, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(raw)}}]})
    judge = LLMJudge(model="independent-judge", api_key="secret", base_url="https://example.test/v1",
                     client=SimpleNamespace(post=post))
    assert judge.judge(c, r).correctness == "correct"
    assert seen["json"]["temperature"] == 0
    assert "secret" not in json.dumps(judge.metadata)
    assert seen["json"]["messages"][0]["role"] == "system"


@pytest.mark.parametrize("body", ["not json", '{"correctness":"correct"}'])
def test_malformed_judge_output_is_error_not_pass(body):
    c, r, _ = sample()
    client = SimpleNamespace(post=lambda *a, **k: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {
            "choices": [{"finish_reason": "stop", "message": {"content": body}}]}))
    with pytest.raises(ValueError):
        LLMJudge(model="judge", api_key="key", base_url="https://example.test", client=client).judge(c, r)


def test_input_overflow_does_not_call_model_or_truncate_evidence():
    c, r, _ = sample()
    client = SimpleNamespace(post=lambda *a, **k: pytest.fail("Must not call judge"))
    with pytest.raises(ValueError, match="budget"):
        LLMJudge(model="judge", api_key="key", base_url="https://example.test",
                 max_input_chars=10, client=client).judge(c, r)


def test_appropriate_refusal_label_cannot_hide_unsupported_claim():
    c = QualityCase(id="q", question="未知设备？", category="no_knowledge", answerable=False,
                    reference_answer="资料不足")
    r = ResponseRecord(id="q", answer="无法回答，但估计36个月。")
    v = QualityVerdict(correctness="correct", refusal="appropriate", reason="错误标签",
                       claims=[{"text": "估计36个月", "verdict": "unsupported", "evidence_ids": [], "reason": "无证据"}],
                       citations=[])
    with pytest.raises(ValueError, match="Appropriate refusal"):
        validate_verdict(c, r, v)
