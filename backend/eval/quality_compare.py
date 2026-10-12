"""Per-case quality regression: aggregate averages cannot hide swapped failures."""
import argparse
import json
from pathlib import Path


def compare_quality(baseline: dict, current: dict) -> list[str]:
    for report in (baseline, current):
        ids = [r["id"] for r in report["results"]]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("Invalid report IDs")
        if report["summary"]["total"] != len(ids):
            raise ValueError("Report denominator differs from results")
        if any(r["status"] != "judged" for r in report["results"]):
            raise ValueError("Cannot compare incomplete semantic evaluations")
        if report["judge"]["kind"] not in {"llm", "human_review"}:
            raise ValueError("Calibration/diagnostic reports cannot be production baselines")
    for key in ("schema_version", "rubric_version", "dataset_sha256", "policy", "judge"):
        if baseline.get(key) != current.get(key):
            raise ValueError(f"Incompatible evaluation configuration: {key}")
    old = {r["id"]: r for r in baseline["results"]}
    new = {r["id"]: r for r in current["results"]}
    if old.keys() != new.keys():
        raise ValueError("Case IDs differ")
    regressions = []
    for id, before in old.items():
        after = new[id]
        if before["passed"] and not after["passed"]:
            regressions.append(f"{id}: passed -> failed")
        for metric in ("correctness", "faithfulness", "citation_accuracy", "citation_coverage"):
            a, b = before["metrics"][metric], after["metrics"][metric]
            if a is not None and (b is None or b < a - 1e-9):
                regressions.append(f"{id}: {metric} {a} -> {b}")
        for metric in ("appropriate_refusal", "unnecessary_refusal"):
            a, b = before["metrics"][metric], after["metrics"][metric]
            if metric == "appropriate_refusal" and a and not b or metric == "unnecessary_refusal" and not a and b:
                regressions.append(f"{id}: {metric} {a} -> {b}")
    return regressions


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare compatible semantic quality reports")
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--current", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        reports = [json.loads(p.read_text(encoding="utf-8")) for p in (args.baseline, args.current)]
        regressions = compare_quality(*reports)
        print(json.dumps({"regressions": regressions}, ensure_ascii=False))
        return 1 if regressions else 0
    except (ValueError, KeyError, OSError) as exc:
        print(f"Invalid comparison: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
