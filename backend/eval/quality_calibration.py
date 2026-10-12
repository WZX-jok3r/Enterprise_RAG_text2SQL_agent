"""Synthetic evaluator self-check. Scores here are NOT live model quality."""
import argparse
import json
from pathlib import Path

from eval.quality import evaluate, load_jsonl
from eval.quality_judge import HumanReview, ReviewedJudge
from eval.quality_metrics import response_hash
from eval.quality_models import Context, QualityCase, QualityVerdict, ResponseRecord


def build_probes(cases):
    base, unknown = cases[0], cases[-1]
    probes = [
        ("correct", "V1.0质保24个月【参考1】", True, "correct", "none", "supported", True),
        ("wrong_number", "V1.0质保36个月【参考1】", False, "incorrect", "none", "contradicted", False),
        ("keyword_trap", "V1.0不是24个月，而是36个月【参考1】", False, "incorrect", "none", "contradicted", False),
        ("wrong_evidence", "V1.0质保24个月【参考1】", False, "correct", "none", "unsupported", False),
        ("bad_citation", "V1.0质保24个月【参考9】", False, "correct", "none", "supported", False),
        ("missing_citation", "V1.0质保24个月", False, "correct", "none", "supported", None),
        ("false_refusal", "根据现有资料无法回答。", False, "incorrect", "unnecessary", None, None),
        ("true_refusal", "现有资料未给出V2.0的期限，无法确认。", True, "correct", "appropriate", None, None),
        ("refusal_plus_hallucination", "无法回答，但估计V2.0质保36个月。", False, "incorrect", "none", "unsupported", None),
    ]
    selected, records, reviews, expected = [], [], [], {}
    for name, answer, should_pass, correctness, refusal, support, cite_support in probes:
        id = "probe-" + name
        is_unknown = name in {"true_refusal", "refusal_plus_hallucination"}
        case = (unknown if is_unknown else base).model_copy(update={"id": id})
        contexts = [] if is_unknown else [Context(id=1, source="contract_snippet.md",
                    text="电源输出12V。" if name == "wrong_evidence" else "V1.0质保24个月。")]
        record = ResponseRecord(id=id, answer=answer, contexts=contexts)
        factual = answer.split("【")[0] if support else None
        claims = [] if factual is None else [{
            "text": factual, "verdict": support,
            "evidence_ids": [1] if support in {"supported", "contradicted"} else [],
            "reason": "人工构造的评测器自检标签"}]
        citations = [] if cite_support is None else [{
            "context_id": 9 if name == "bad_citation" else 1, "supported": cite_support,
            "reason": "人工构造的引用关系"}]
        verdict = QualityVerdict(correctness=correctness, refusal=refusal, claims=claims,
                                 citations=citations, reason="合成正反例，不是真实模型结果")
        selected.append(case); records.append(record)
        reviews.append(HumanReview(id=id, response_sha256=response_hash(record),
                                   reviewer="synthetic-evaluator-fixture", verdict=verdict))
        expected[id] = should_pass
    return selected, records, reviews, expected


def run_self_check(dataset: Path) -> dict:
    cases = load_jsonl(dataset, QualityCase)
    selected, records, reviews, expected = build_probes(cases)
    report = evaluate(selected, records, ReviewedJudge(reviews, fixture=True))
    mismatches = [r["id"] for r in report["results"] if r["passed"] != expected[r["id"]]]
    report["self_check"] = {"expected": expected, "mismatches": mismatches,
                            "passed": not mismatches and report["summary"]["errors"] == 0}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Synthetic answer-quality evaluator self-check")
    parser.add_argument("--dataset", type=Path, default=Path(__file__).parent / "data/answer_quality_v1.jsonl")
    parser.add_argument("--output", type=Path, default=Path(__file__).parents[2] / "output/quality_self_check.json")
    args = parser.parse_args(argv)
    report = run_self_check(args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["self_check"], ensure_ascii=False))
    return 0 if report["self_check"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
