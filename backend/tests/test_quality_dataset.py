from pathlib import Path

from eval.quality import load_jsonl
from eval.quality_calibration import run_self_check
from eval.quality_metrics import normalize
from eval.quality_models import QualityCase

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "backend/eval/data/answer_quality_v1.jsonl"


def test_annotations_are_backed_by_current_repository_documents():
    cases = load_jsonl(DATASET, QualityCase)
    assert len(cases) == 12
    assert sum(not c.answerable for c in cases) == 3
    for case in cases:
        for evidence in case.gold_evidence:
            text = (ROOT / "knowledge_base" / evidence.source).read_text(encoding="utf-8")
            assert normalize(evidence.quote) in normalize(text), (case.id, evidence.source)


def test_calibration_accepts_good_answers_and_rejects_every_negative_probe():
    report = run_self_check(DATASET)
    assert report["self_check"]["passed"], report["self_check"]
    assert report["summary"]["errors"] == 0
    assert report["summary"]["passed"] == 2
    assert report["summary"]["failed"] == 7
    assert report["judge"]["kind"] == "calibration_fixture"
