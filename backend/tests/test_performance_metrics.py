from decimal import Decimal

import pytest
from pydantic import ValidationError

from eval.performance_metrics import PriceBook, ProviderCall, cost_summary, latency_summary, percentile


def prices():
    return PriceBook(version="fixture-v1", as_of="2026-10-09", source="synthetic test prices",
        currency="CNY", rates=[{"kind": "llm", "model": "fake", "input_per_million": "1",
                                "output_per_million": "2"}])


def call(**kwargs):
    return ProviderCall(kind="llm", model="fake", elapsed_ms=10, outcome="success", **kwargs)


def test_percentiles_nearest_rank_and_empty():
    assert percentile(list(range(1, 101)), .95) == 95
    assert percentile([100, 1, 2], .99) == 100
    assert latency_summary([])["p95_ms"] is None
    with pytest.raises(ValueError):
        percentile([float("nan")], .95)


def test_decimal_cost_and_failed_retry_is_not_free():
    failed = call(input_tokens=1000, output_tokens=0).model_copy(update={"outcome": "error"})
    report = cost_summary([failed, call(input_tokens=1000, output_tokens=500)], prices())
    assert Decimal(report["total_cost"]) == Decimal("0.003")
    assert report["attempts"] == 2 and report["by_kind"]["llm"]["errors"] == 1


def test_missing_usage_or_prices_never_becomes_zero_total():
    assert cost_summary([call()], prices())["total_cost"] is None
    assert cost_summary([call()], prices())["known_cost"] is None
    assert cost_summary([call(input_tokens=10, output_tokens=20)])["known_cost"] is None
    assert not cost_summary([], prices(), trace_complete=False)["complete"]
    assert cost_summary([], prices(), trace_complete=True)["total_cost"] == "0"
    partial = call(input_tokens=10, output_tokens=3).model_copy(update={"outcome": "incomplete"})
    assert cost_summary([partial], prices())["total_cost"] is None


def test_price_validation_rejects_duplicates_negative_and_unknown_fields():
    book = prices()
    book.rates.append(book.rates[0])
    with pytest.raises(ValueError, match="Duplicate"):
        cost_summary([], book)
    with pytest.raises(ValidationError):
        PriceBook.model_validate({**prices().model_dump(), "secret_key": "not allowed"})
    with pytest.raises(ValidationError):
        call(input_tokens=-1)
