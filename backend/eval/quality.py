"""Answer-quality evaluation, independent of the frozen retrieval runner.

python -m eval.quality --help
Exit 0: all cases passed; 1: quality failures; 2: invalid/incomplete evaluation.
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from eval.quality_judge import HumanReview, LLMJudge, ReviewedJudge, RUBRIC_VERSION, semantic_metrics
from eval.quality_metrics import diagnostics, response_hash
from eval.quality_models import Context, QualityCase, ResponseRecord


def load_jsonl(path: Path, model):
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                rows.append(model.model_validate_json(line))
            except ValueError as exc:
                raise ValueError(f"{path.name}:{number}: invalid {model.__name__}") from exc
    if not rows:
        raise ValueError(f"Empty dataset: {path.name}")
    ids = [r.id for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate IDs in {path.name}")
    return rows


def dump_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(r.model_dump_json() + "\n" for r in rows), encoding="utf-8")


def content_hash(rows) -> str:
    value = json.dumps([r.model_dump() for r in rows], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def evaluate(cases: list[QualityCase], records: list[ResponseRecord], judge=None,
             *, require_citations: bool = True) -> dict:
    if not cases or len({c.id for c in cases}) != len(cases):
        raise ValueError("Dataset must be nonempty with unique IDs")
    index = {r.id: r for r in records}
    if len(index) != len(records) or set(index) - {c.id for c in cases}:
        raise ValueError("Duplicate or unexpected response IDs")
    results = []
    for case in cases:
        row = {"id": case.id, "question": case.question, "category": case.category,
               "answerable": case.answerable, "reference_answer": case.reference_answer,
               "gold_evidence": [e.model_dump() for e in case.gold_evidence],
               "status": "unjudged", "passed": None}
        results.append(row)
        try:
            record = index.get(case.id)
            if record is None:
                raise ValueError("Missing response")
            row.update(response=record.model_dump(), response_sha256=response_hash(record))
            if record.error:
                raise ValueError("Response generation failed: " + record.error)
            row["diagnostics"] = diagnostics(case, record)
            if judge is None:
                continue
            verdict = judge.judge(case, record)
            metrics = semantic_metrics(case, record, verdict)
            row.update(status="judged", verdict=verdict.model_dump(), metrics=metrics)
            if case.answerable:
                row["passed"] = (metrics["correctness"] == 1 and metrics["faithfulness"] == 1
                                 and not metrics["refusal_detected"])
                if require_citations:
                    row["passed"] = (row["passed"] and metrics["citation_accuracy"] == 1
                                     and metrics["citation_coverage"] == 1)
            else:
                row["passed"] = (metrics["correctness"] == 1 and metrics["appropriate_refusal"]
                                 and metrics["unsupported_claims"] == 0)
            if row["diagnostics"]["invalid_citation_ids"]:
                row["passed"] = False
        except Exception as exc:
            # Keep the case in the denominator. Never turn provider failure into refusal.
            row.update(status="error", passed=False, error=f"{type(exc).__name__}: {exc}")

    judged = [r for r in results if r["status"] == "judged"]
    answerable = [r for r in results if r["answerable"]]
    unknown = [r for r in results if not r["answerable"]]
    def summary_metric(key, eligible):
        values = [r["metrics"][key] for r in eligible if r["status"] == "judged"
                  and r["metrics"][key] is not None]
        return {"mean": sum(values) / len(values) if values else None,
                "scored_cases": len(values), "eligible_cases": len(eligible),
                "coverage": len(values) / len(eligible) if eligible else None}
    refusals = [r for r in judged if r["metrics"]["refusal_detected"]]
    correct_refusals = sum(r["metrics"]["appropriate_refusal"] for r in judged)
    errors = sum(r["status"] == "error" for r in results)
    unjudged = sum(r["status"] == "unjudged" for r in results)
    passed = sum(r["passed"] is True for r in results)
    return {
        "schema_version": 1, "rubric_version": RUBRIC_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_sha256": content_hash(cases), "responses_sha256": content_hash(records),
        "judge": judge.metadata if judge is not None else {"kind": "none"},
        "policy": {"require_citations": require_citations},
        "summary": {"total": len(cases), "judged": len(judged), "errors": errors,
                    "unjudged": unjudged, "passed": passed,
                    "failed": sum(r["passed"] is False for r in results),
                    "pass_rate": passed / len(cases), "semantic_coverage": len(judged) / len(cases),
                    "correctness": summary_metric("correctness", results),
                    "faithfulness": summary_metric("faithfulness", answerable),
                    "citation_accuracy": summary_metric("citation_accuracy", answerable),
                    "citation_coverage": summary_metric("citation_coverage", answerable),
                    "refusal_precision": correct_refusals / len(refusals) if refusals else None,
                    "refusal_recall": correct_refusals / len(unknown)
                                      if unknown and any(r["status"] == "judged" for r in unknown) else None,
                    "false_refusal_rate": sum(r["metrics"]["unnecessary_refusal"] for r in judged)
                                          / len(answerable)
                                          if answerable and any(r["status"] == "judged" for r in answerable) else None,
                    "gate_passed": not errors and not unjudged and passed == len(cases)},
        "by_category": {cat: {"total": sum(r["category"] == cat for r in results),
                              "passed": sum(r["category"] == cat and r["passed"] is True for r in results)}
                        for cat in sorted({c.category for c in cases})},
        "results": results,
    }


class CapturingRetriever:
    """Capture exactly the chunks used by the existing pipeline, not previews."""
    def __init__(self, retriever):
        self.retriever = retriever
        self.contexts = []

    def search(self, query, top_k=None, filter_dict=None):
        chunks, dropped = self.retriever.search(query, top_k=top_k, filter_dict=filter_dict)
        self.contexts = [Context(id=i, source=str(c["metadata"]["source"]), text=c["text"])
                         for i, c in enumerate(chunks, 1)]
        return chunks, dropped


def capture(cases, pipeline, recorder, top_k, filter_dict=None):
    records = []
    for case in cases:
        recorder.contexts = []
        try:
            result = pipeline.query(case.question, top_k=top_k, filter_dict=filter_dict)
            answer = result["answer"]
            # The production client returns an error string instead of raising.
            if answer.startswith(("抱歉，调用模型时出错", "抱歉，模型响应格式异常")):
                raise ValueError("Production model returned a provider error")
            records.append(ResponseRecord(id=case.id, answer=answer, contexts=recorder.contexts))
        except Exception as exc:
            records.append(ResponseRecord(id=case.id, answer="[generation error]",
                                          contexts=recorder.contexts, error=type(exc).__name__))
    return records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Answer quality: full evidence + semantic judgment")
    parser.add_argument("--dataset", type=Path, default=Path(__file__).parent / "data/answer_quality_v1.jsonl")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--responses", type=Path, help="Replay full-context response JSONL")
    mode.add_argument("--live", action="store_true", help="Capture current RAG answers and full evidence")
    reviewers = parser.add_mutually_exclusive_group()
    reviewers.add_argument("--reviews", type=Path, help="Response-hash-bound human judgments JSONL")
    reviewers.add_argument("--judge-model", help="Explicit semantic judge model (one call per case)")
    parser.add_argument("--fixture-reviews", action="store_true", help="Label reviews as synthetic calibration fixtures")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--tenant-id")
    parser.add_argument("--allow-uncited", action="store_true", help="Diagnostic policy; recorded in report")
    parser.add_argument("--output", type=Path, default=Path(__file__).parents[2] / "output/answer_quality.json")
    args = parser.parse_args(argv)
    try:
        if (args.limit is not None and args.limit <= 0) or args.top_k <= 0:
            raise ValueError("limit/top-k must be positive")
        if args.fixture_reviews and args.reviews is None:
            raise ValueError("--fixture-reviews requires --reviews")
        cases = load_jsonl(args.dataset, QualityCase)
        if args.limit:
            cases = cases[:args.limit]
        judge = None
        if args.reviews:
            judge = ReviewedJudge(load_jsonl(args.reviews, HumanReview), fixture=args.fixture_reviews)
        elif args.judge_model:
            from app.core.config import settings
            judge = LLMJudge(model=args.judge_model,
                             api_key=os.getenv("QUALITY_JUDGE_API_KEY") or settings.siliconflow_api_key,
                             base_url=os.getenv("QUALITY_JUDGE_BASE_URL") or settings.siliconflow_base_url)
        if args.live:
            from app.core.config import settings
            from app.core.security import apply_tenant_acl
            from app.rag.pipeline import RAGPipeline
            from app.rag.retriever import Retriever
            recorder = CapturingRetriever(Retriever())
            pipeline = RAGPipeline(retriever=recorder)
            records = capture(cases, pipeline, recorder, args.top_k,
                              apply_tenant_acl(None, args.tenant_id))
            generation = {"kind": "live_rag", "model": settings.llm_model, "top_k": args.top_k,
                          "retrieval_mode": settings.retrieval_mode, "rerank_model": settings.rerank_model,
                          "rerank_enabled": settings.rerank_enabled,
                          "embedding_model": settings.embedding_model,
                          "collection": settings.qdrant_collection_name,
                          "generation_temperature": settings.llm_temperature,
                          "tenant_id": args.tenant_id}
            dump_jsonl(args.output.with_suffix(".responses.jsonl"), records)
        else:
            records = load_jsonl(args.responses, ResponseRecord)
            if args.limit:
                records = [r for r in records if r.id in {c.id for c in cases}]
            generation = {"kind": "offline_replay", "input": args.responses.name}
        report = evaluate(cases, records, judge, require_citations=not args.allow_uncited)
        report["generation"] = generation
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["summary"], ensure_ascii=False))
        if report["summary"]["errors"] or report["summary"]["unjudged"]:
            return 2
        return 0 if report["summary"]["gate_passed"] else 1
    except Exception as exc:
        print(f"Evaluation error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
