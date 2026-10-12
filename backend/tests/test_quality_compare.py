import copy

import pytest

from eval.quality_compare import compare_quality


def report():
    return {"schema_version": 1, "rubric_version": "v1", "dataset_sha256": "dataset",
            "policy": {"require_citations": True}, "judge": {"kind": "human_review"},
            "summary": {"total": 2}, "results": [
                {"id": id, "status": "judged", "passed": passed, "metrics": {
                    "correctness": score, "faithfulness": score, "citation_accuracy": score,
                    "citation_coverage": score, "appropriate_refusal": False,
                    "unnecessary_refusal": False}} for id, passed, score in [("a", True, 1), ("b", False, 0)]]}


def test_equal_averages_do_not_hide_per_case_regression():
    baseline = report()
    current = copy.deepcopy(baseline)
    current["results"][0]["metrics"], current["results"][1]["metrics"] = (
        current["results"][1]["metrics"], current["results"][0]["metrics"])
    current["results"][0]["passed"], current["results"][1]["passed"] = False, True
    assert any(r.startswith("a:") for r in compare_quality(baseline, current))
    assert compare_quality(baseline, copy.deepcopy(baseline)) == []


@pytest.mark.parametrize("change", [
    lambda r: r.update(dataset_sha256="changed"),
    lambda r: r["judge"].update(kind="calibration_fixture"),
    lambda r: r["results"][0].update(status="error"),
    lambda r: r["results"].pop(),
    lambda r: r["results"][1].update(id="a"),
    lambda r: r.update(policy={"require_citations": False}),
])
def test_invalid_or_incomparable_reports_fail_closed(change):
    a, b = report(), report()
    change(b)
    with pytest.raises(ValueError):
        compare_quality(a, b)
