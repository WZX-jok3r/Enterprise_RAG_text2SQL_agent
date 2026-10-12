"""Evidence/format diagnostics; no lexical proxy is called semantic accuracy."""
import hashlib
import json
import re
import unicodedata

from eval.quality_models import QualityCase, ResponseRecord


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).lower()


def citation_ids(answer: str) -> set[int]:
    # Mirrors build_context's numbered references; accepts common model variants.
    return {int(m.group(1) or m.group(2)) for m in re.finditer(
        r"[【\[]\s*参考\s*(\d+)\s*[】\]]|\[(\d+)\]", answer
    )}


def response_hash(record: ResponseRecord) -> str:
    payload = json.dumps(record.model_dump(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def diagnostics(case: QualityCase, record: ResponseRecord) -> dict:
    if case.id != record.id:
        raise ValueError("Case/response IDs differ")
    ids = citation_ids(record.answer)
    available = {c.id for c in record.contexts}
    hits = [any(normalize(e.source) == normalize(c.source)
                and normalize(e.quote) in normalize(c.text) for c in record.contexts)
            for e in case.gold_evidence]
    facts = [normalize(f) in normalize(record.answer) for f in case.required_facts]
    return {
        "evidence_recall": sum(hits) / len(hits) if hits else None,
        "fact_coverage_diagnostic": sum(facts) / len(facts) if facts else None,
        "citation_ids": sorted(ids),
        "invalid_citation_ids": sorted(ids - available),
        "citation_id_validity": len(ids & available) / len(ids) if ids else None,
    }
