"""Deterministic latency/cost calculations. Missing bills are never free calls."""
import math
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderCall(StrictModel):
    kind: str = Field(pattern="^(llm|embedding|rerank)$")
    model: str = Field(min_length=1)
    elapsed_ms: float = Field(ge=0, allow_inf_nan=False)
    outcome: str = Field(pattern="^(success|error|incomplete)$")
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    error_type: str | None = None


class Rate(StrictModel):
    kind: str = Field(pattern="^(llm|embedding|rerank)$")
    model: str = Field(min_length=1)
    input_per_million: Decimal = Field(ge=0, allow_inf_nan=False)
    output_per_million: Decimal = Field(ge=0, allow_inf_nan=False)


class PriceBook(StrictModel):
    version: str = Field(min_length=1)
    as_of: str = Field(min_length=1)
    source: str = Field(min_length=1)
    currency: str = Field(pattern="^[A-Z]{3}$")
    rates: list[Rate]


def percentile(values, quantile):
    """Nearest rank: small samples cannot manufacture interpolated tail latency."""
    if not 0 < quantile <= 1:
        raise ValueError("quantile must be in (0, 1]")
    if not values:
        return None
    if any(not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("latency must be finite and nonnegative")
    return sorted(values)[math.ceil(quantile * len(values)) - 1]


def latency_summary(values):
    return {"samples": len(values), "mean_ms": sum(values) / len(values) if values else None,
            "p50_ms": percentile(values, .5), "p95_ms": percentile(values, .95),
            "p99_ms": percentile(values, .99), "max_ms": max(values) if values else None}


def cost_summary(calls, pricebook=None, *, trace_complete=True):
    rates = {} if pricebook is None else {(r.kind, r.model): r for r in pricebook.rates}
    if pricebook and len(rates) != len(pricebook.rates):
        raise ValueError("Duplicate model price rates")
    known = Decimal(0)
    priced = 0
    missing = []
    by_kind = {}
    for call in calls:
        group = by_kind.setdefault(call.kind, {"attempts": 0, "errors": 0, "usage_known": 0,
            "input_tokens": 0, "output_tokens": 0})
        group["attempts"] += 1
        group["errors"] += call.outcome != "success"
        usage_known = call.input_tokens is not None and call.output_tokens is not None
        if usage_known:
            group["usage_known"] += 1
            group["input_tokens"] += call.input_tokens
            group["output_tokens"] += call.output_tokens
        rate = rates.get((call.kind, call.model))
        if not usage_known or rate is None:
            missing.append({"kind": call.kind, "model": call.model,
                            "reason": "usage_missing" if not usage_known else "rate_missing"})
            continue
        known += (call.input_tokens * rate.input_per_million +
                  call.output_tokens * rate.output_per_million) / Decimal(1_000_000)
        priced += 1
    complete = bool(pricebook) and trace_complete and priced == len(calls) and all(c.outcome != "incomplete" for c in calls)
    return {"currency": pricebook.currency if pricebook else None,
            "price_version": pricebook.version if pricebook else None,
            "known_cost": str(known) if pricebook and (priced > 0 or (not calls and trace_complete)) else None,
            "total_cost": str(known) if complete else None,
            "complete": complete, "trace_complete": trace_complete,
            "attempts": len(calls), "priced_attempts": priced, "unknown_attempts": missing,
            "by_kind": by_kind}
