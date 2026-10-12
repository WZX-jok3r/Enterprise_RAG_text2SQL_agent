"""Closed-loop HTTP benchmark. Fixture results are not production capacity."""
import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import sys
import time
import uuid
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from eval.performance_metrics import PriceBook, ProviderCall, StrictModel, cost_summary, latency_summary

ENDPOINTS = {"query": "/api/v1/query", "chat": "/api/v1/chat",
             "chat-stream": "/api/v1/chat/stream", "agent-stream": "/api/v1/agent/chat"}
ERROR_PREFIXES = ("抱歉，调用模型时出错", "抱歉，模型响应格式异常", "调用模型时出错")


class Case(StrictModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    top_k: int = Field(default=3, ge=1, le=20)


def load_cases(path):
    if path is None:
        return [Case(id="fixture-a", question="并发测量测试A"), Case(id="fixture-b", question="并发测量测试B")]
    cases = [Case.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not cases or len({c.id for c in cases}) != len(cases):
        raise ValueError("Cases must be nonempty and have unique IDs")
    return cases


def validate_base_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Base URL must be HTTP(S), without credentials, query or fragment")
    return url.rstrip("/")


async def sample_request(client, endpoint, case, timeout):
    request_id = uuid.uuid4().hex
    row = {"request_id": request_id, "case_id": case.id, "success": False,
           "status": None, "error_type": None, "ttft_ms": None, "degraded": False}
    started = time.perf_counter()
    streamed = endpoint.endswith("stream")
    # agent-stream is a streaming endpoint despite its URL not ending in /stream.
    payload = {"query" if endpoint == "query" else "message": case.question, "top_k": case.top_k}
    try:
        async with asyncio.timeout(timeout):
            async with client.stream("POST", ENDPOINTS[endpoint], json=payload,
                                     headers={"X-Request-ID": request_id}) as response:
                row["status"] = response.status_code
                if response.headers.get("X-Request-ID") != request_id:
                    row["trace_correlated"] = False
                else:
                    row["trace_correlated"] = True
                response.raise_for_status()
                if not streamed:
                    data = json.loads(await response.aread())
                    answer = data.get("answer")
                    if not isinstance(answer, str) or not answer.strip() or answer.startswith(ERROR_PREFIXES):
                        raise ValueError("business_error")
                else:
                    if not response.headers.get("content-type", "").startswith("text/event-stream"):
                        raise ValueError("not_sse")
                    frame, done, text = [], False, []
                    size = 0
                    async for line in response.aiter_lines():
                        size += len(line)
                        if size > 8_000_000:
                            raise ValueError("stream_too_large")
                        if line:
                            if line.startswith("data:"):
                                frame.append(line[5:].strip())
                            continue
                        if not frame:
                            continue
                        event = json.loads("\n".join(frame))
                        frame = []
                        if event.get("type") == "error":
                            raise ValueError("sse_error")
                        if event.get("type") == "degraded" or (event.get("agent_stats") or {}).get("degraded"):
                            row["degraded"] = True
                        if event.get("type") == "delta" and event.get("text"):
                            if done:
                                raise ValueError("delta_after_done")
                            if row["ttft_ms"] is None:
                                row["ttft_ms"] = (time.perf_counter() - started) * 1000
                            text.append(event["text"])
                        if event.get("type") == "done":
                            done = True
                    if not done or not text or "".join(text).startswith(ERROR_PREFIXES):
                        raise ValueError("incomplete_or_failed_stream")
                row["success"] = True
    except ValueError:
        # Don't store response bodies or validation messages containing business data.
        row["error_type"] = "invalid_response"
    except Exception as exc:
        row["error_type"] = type(exc).__name__
    row["elapsed_ms"] = (time.perf_counter() - started) * 1000
    return row


async def run_scenario(client, cases, endpoint, *, requests, concurrency, warmup=0, timeout=30):
    if requests < 1 or concurrency < 1 or warmup < 0 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Invalid benchmark limits")
    warmup_rows = [await sample_request(client, endpoint, cases[i % len(cases)], timeout) for i in range(warmup)]
    rows, next_index, active, peak = [], 0, 0, 0
    async def worker():
        nonlocal next_index, active, peak
        while next_index < requests:
            index = next_index
            next_index += 1
            active += 1
            peak = max(peak, active)
            try:
                row = await sample_request(client, endpoint, cases[index % len(cases)], timeout)
                row["sequence"] = index
                rows.append(row)
            finally:
                active -= 1
    start = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(min(concurrency, requests))))
    wall = time.perf_counter() - start
    rows.sort(key=lambda r: r["sequence"])
    succeeded = [r for r in rows if r["success"]]
    return {"concurrency": concurrency, "observed_peak_inflight": peak,
            "requests": requests, "succeeded": len(succeeded), "failed": requests - len(succeeded),
            "error_rate": (requests - len(succeeded)) / requests, "wall_seconds": wall,
            "degraded_requests": sum(r["degraded"] for r in rows),
            "degraded_rate": sum(r["degraded"] for r in rows) / requests,
            "successful_rps": len(succeeded) / wall, "attempted_rps": requests / wall,
            "latency_all": latency_summary([r["elapsed_ms"] for r in rows]),
            "latency_success": latency_summary([r["elapsed_ms"] for r in succeeded]),
            "ttft_success": latency_summary([r["ttft_ms"] for r in succeeded if r["ttft_ms"] is not None]),
            "warmup": warmup_rows, "samples": rows}


def read_ledger(path):
    if path is None or not path.exists():
        return {}
    rows = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise ValueError(f"Incomplete measurement JSONL at line {number}") from None
        if row.get("schema_version") != 1 or not isinstance(row.get("request_id"), str):
            raise ValueError("Invalid measurement ledger")
        if row["request_id"] in rows:
            raise ValueError("Duplicate request ID in measurement ledger")
        row["calls"] = [ProviderCall.model_validate(c) for c in row["calls"]]
        rows[row["request_id"]] = row
    return rows


def attach_cost(scenario, ledger, pricebook):
    def summarize(samples):
        records = [ledger[r["request_id"]] for r in samples
                   if r.get("trace_correlated") and r["request_id"] in ledger]
        complete = len(records) == len(samples) and all(r.get("completed") for r in records)
        cost = cost_summary([c for r in records for c in r["calls"]], pricebook, trace_complete=complete)
        cost["trace_coverage"] = len(records) / len(samples) if samples else 1
        return cost, records
    measured, records = summarize(scenario["samples"])
    measured["cost_per_attempted_request"] = (str(Decimal(measured["total_cost"]) / scenario["requests"])
        if measured["total_cost"] is not None else None)
    measured["cost_per_successful_request"] = (str(Decimal(measured["total_cost"]) / scenario["succeeded"])
        if measured["total_cost"] is not None and scenario["succeeded"] else None)
    warmup, _ = summarize(scenario["warmup"])
    whole_run, _ = summarize(scenario["samples"] + scenario["warmup"])
    scenario.update(cost_measured=measured, cost_warmup=warmup, cost_including_warmup=whole_run)
    scenario["server_latency"] = latency_summary([r["server_elapsed_ms"] for r in records])
    scenario["provider_latency"] = {kind: latency_summary([c.elapsed_ms for r in records for c in r["calls"] if c.kind == kind])
                                    for kind in ("llm", "embedding", "rerank")}
    scenario["observed_models"] = sorted({(c.kind, c.model) for r in records for c in r["calls"]})
    names = {s["name"] for r in records for s in r.get("stages", [])}
    scenario["stage_latency"] = {name: latency_summary([s["elapsed_ms"] for r in records for s in r.get("stages", []) if s["name"] == name])
                                for name in sorted(names)}


def apply_gate(scenario, *, max_error_rate=0, max_p95_ms=None, max_ttft_ms=None, require_cost=False):
    failures = []
    if scenario["error_rate"] > max_error_rate:
        failures.append("error_rate")
    if any(not r["success"] for r in scenario["warmup"]):
        failures.append("warmup_failed")
    if max_p95_ms is not None and (scenario["latency_success"]["p95_ms"] is None or scenario["latency_success"]["p95_ms"] > max_p95_ms):
        failures.append("p95_latency")
    if max_ttft_ms is not None and (scenario["ttft_success"]["samples"] != scenario["succeeded"] or
        scenario["ttft_success"]["p95_ms"] is None or scenario["ttft_success"]["p95_ms"] > max_ttft_ms):
        failures.append("ttft")
    if require_cost and not scenario["cost_including_warmup"]["complete"]:
        failures.append("cost_incomplete")
    scenario["gate"] = {"passed": not failures, "failures": failures}


async def execute(args):
    cases = load_cases(args.dataset)
    levels = [int(c.strip()) for c in args.concurrency.split(",")]
    if not levels or len(levels) != len(set(levels)) or any(c < 1 or c > 256 for c in levels):
        raise ValueError("Concurrency levels must be unique values between 1 and 256")
    if not 1 <= args.requests <= 100000 or args.warmup < 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("Invalid requests/warmup/timeout")
    if not 0 <= args.max_error_rate <= 1 or any(v is not None and (not math.isfinite(v) or v <= 0) for v in [args.max_p95_ms, args.max_ttft_ms]):
        raise ValueError("Invalid gate thresholds")
    prices = PriceBook.model_validate_json(args.prices.read_text(encoding="utf-8")) if args.prices else None
    if prices:
        cost_summary([], prices)  # validate duplicate rates before sending requests
    fixture = None
    ledger_path = args.usage_log
    if args.mode == "fixture":
        if args.endpoint == "agent-stream":
            raise ValueError("Agent fixture is unavailable; use an authorized HTTP target")
        from eval.performance_fixture import FixtureServer
        ledger_path = args.output.with_suffix(".measurements.jsonl")
        fixture = FixtureServer(ledger_path)
        base_url = await fixture.start()
    else:
        if args.dataset is None:
            raise ValueError("HTTP mode requires an explicit dataset")
        base_url = validate_base_url(args.base_url)
    headers = {}
    if args.mode == "http" and os.getenv("RAG_BENCH_API_KEY"):
        headers["X-API-Key"] = os.environ["RAG_BENCH_API_KEY"]
    scenarios = []
    try:
        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=args.timeout,
                                    limits=httpx.Limits(max_connections=max(levels), max_keepalive_connections=max(levels))) as client:
            for level in levels:
                scenario = await run_scenario(client, cases, args.endpoint, requests=args.requests,
                                              concurrency=level, warmup=args.warmup, timeout=args.timeout)
                scenarios.append(scenario)
                print(json.dumps({"concurrency": level, "requests": scenario["requests"],
                    "succeeded": scenario["succeeded"], "p95_ms": scenario["latency_success"]["p95_ms"]}))
                # Stop stepping up load if the current level already fails reliability/latency.
                # The response may finish just before the server flushes its numerical ledger.
                expected = {r["request_id"] for r in scenario["samples"] + scenario["warmup"]}
                ledger = read_ledger(ledger_path)
                for _ in range(20):
                    if ledger_path is None or expected.issubset(ledger):
                        break
                    await asyncio.sleep(.1)
                    ledger = read_ledger(ledger_path)
                attach_cost(scenario, ledger, prices)
                apply_gate(scenario, max_error_rate=args.max_error_rate, max_p95_ms=args.max_p95_ms,
                           max_ttft_ms=args.max_ttft_ms, require_cost=args.require_cost)
                if not scenario["gate"]["passed"]:
                    break
    finally:
        if fixture:
            await fixture.stop()
    ledger = read_ledger(ledger_path)
    for scenario in scenarios:
        attach_cost(scenario, ledger, prices)
        apply_gate(scenario, max_error_rate=args.max_error_rate, max_p95_ms=args.max_p95_ms,
                   max_ttft_ms=args.max_ttft_ms, require_cost=args.require_cost)
    report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "mode": args.mode, "endpoint": args.endpoint, "base_url": base_url,
              "load_model": "closed_loop_fixed_workers", "percentile_method": "nearest_rank",
              "python": platform.python_version(), "platform": platform.platform(),
              "case_sha256": hashlib.sha256(json.dumps([c.model_dump() for c in cases], ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
              "prices": prices.model_dump(mode="json") if prices else None,
              "requested_levels": levels, "gates": {"max_error_rate": args.max_error_rate,
                  "max_p95_ms": args.max_p95_ms, "max_ttft_ms": args.max_ttft_ms, "require_cost": args.require_cost},
              "scenarios": scenarios,
              "complete": len(scenarios) == len(levels) and all(s["gate"]["passed"] for s in scenarios)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.require_cost and any(not s["cost_including_warmup"]["complete"] for s in scenarios):
        return 2
    return 0 if report["complete"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["fixture", "http"], default="fixture")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--endpoint", choices=list(ENDPOINTS), default="chat-stream")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", default="1,2,4")
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--usage-log", type=Path)
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--max-error-rate", type=float, default=0)
    parser.add_argument("--max-p95-ms", type=float)
    parser.add_argument("--max-ttft-ms", type=float)
    parser.add_argument("--require-cost", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(__file__).parents[2] / "output/performance_fixture.json")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(execute(args))
    except Exception as exc:
        print("Benchmark error: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
