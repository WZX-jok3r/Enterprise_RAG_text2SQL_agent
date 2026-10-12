import asyncio
import json

import httpx
import pytest

from eval.performance import Case, apply_gate, attach_cost, main, read_ledger, run_scenario, sample_request, validate_base_url


class Stream(httpx.AsyncByteStream):
    def __init__(self, events, delay=.001):
        self.events, self.delay = events, delay
    async def __aiter__(self):
        for event in self.events:
            await asyncio.sleep(self.delay)
            yield ("data: " + json.dumps(event) + "\n\n").encode()


def response(request, events):
    return httpx.Response(200, headers={"content-type": "text/event-stream", "X-Request-ID": request.headers["X-Request-ID"]}, stream=Stream(events))


async def test_concurrency_bound_and_meaningful_ttft():
    def handler(request):
        return response(request, [{"type": "meta"}, {"type": "delta", "text": "text"}, {"type": "done"}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        scenario = await run_scenario(client, [Case(id="a", question="q")], "chat-stream", requests=9, concurrency=3, warmup=1)
    assert scenario["succeeded"] == 9 and scenario["observed_peak_inflight"] == 3
    assert len(scenario["warmup"]) == 1 and len(scenario["samples"]) == 9
    assert all(0 < r["ttft_ms"] < r["elapsed_ms"] for r in scenario["samples"])
    attach_cost(scenario, {}, None)
    assert scenario["cost_measured"]["trace_coverage"] == 0
    assert scenario["cost_measured"]["total_cost"] is None


@pytest.mark.parametrize("events", [
    [{"type": "meta"}, {"type": "error", "message": "private data"}],
    [{"type": "delta", "text": "partial"}],
    [{"type": "meta"}, {"type": "done"}],
    [{"type": "delta", "text": "抱歉，调用模型时出错: secret"}, {"type": "done"}],
])
async def test_http_200_does_not_hide_sse_failures(events):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response(req, events)), base_url="http://test") as client:
        row = await sample_request(client, "chat-stream", Case(id="a", question="q"), 1)
    assert not row["success"] and row["status"] == 200
    assert "private" not in json.dumps(row) and "secret" not in json.dumps(row)


async def test_timeout_and_http_failure_keep_denominator():
    async def handler(req):
        await asyncio.sleep(.02)
        return httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        result = await run_scenario(client, [Case(id="a", question="q")], "query", requests=3, concurrency=2, timeout=.001)
    assert result["failed"] == 3 and result["requests"] == 3
    assert result["latency_success"]["p95_ms"] is None
    assert all(r["error_type"] == "TimeoutError" for r in result["samples"])


async def test_failed_http_request_keeps_cost_correlation():
    def handler(req):
        return httpx.Response(503, headers={"X-Request-ID": req.headers["X-Request-ID"]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        row = await sample_request(client, "query", Case(id="a", question="q"), 1)
    assert row["trace_correlated"] and not row["success"] and row["status"] == 503


def test_invalid_url_and_duplicate_ledger_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        validate_base_url("https://secret-key@example.com")
    path = tmp_path / "ledger.jsonl"
    row = {"schema_version": 1, "request_id": "a", "calls": []}
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        read_ledger(path)


def test_real_tcp_fixture_cli_and_missing_cost_gate(tmp_path):
    output = tmp_path / "report.json"
    assert main(["--requests", "4", "--concurrency", "1,2", "--warmup", "1", "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["mode"] == "fixture" and report["complete"]
    assert all(s["cost_measured"]["trace_coverage"] == 1 for s in report["scenarios"])
    assert all(s["cost_measured"]["attempts"] == 4 for s in report["scenarios"])
    assert all(s["ttft_success"]["samples"] == 4 for s in report["scenarios"])
    assert main(["--requests", "2", "--concurrency", "1,2", "--warmup", "0", "--require-cost", "--output", str(output)]) == 2
    assert len(json.loads(output.read_text(encoding="utf-8"))["scenarios"]) == 1


def test_cli_invalid_config_does_not_start_load(tmp_path):
    assert main(["--concurrency", "1,1", "--output", str(tmp_path / "bad.json")]) == 2
    assert main(["--mode", "http", "--output", str(tmp_path / "bad.json")]) == 2
    assert main(["--timeout", "nan", "--output", str(tmp_path / "bad.json")]) == 2
    assert main(["--max-p95-ms", "nan", "--output", str(tmp_path / "bad.json")]) == 2


def test_priced_fixture_includes_warmup_and_per_request_cost(tmp_path):
    from decimal import Decimal
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"version": "fixture", "source": "synthetic prices",
        "as_of": "2026-10-09", "currency": "CNY", "rates": [{"kind": "llm", "model": "fixture-model",
        "input_per_million": "1", "output_per_million": "2"}]}), encoding="utf-8")
    output = tmp_path / "report.json"
    assert main(["--requests", "2", "--concurrency", "1", "--warmup", "1", "--prices", str(prices),
                 "--require-cost", "--output", str(output)]) == 0
    scenario = json.loads(output.read_text(encoding="utf-8"))["scenarios"][0]
    assert Decimal(scenario["cost_including_warmup"]["total_cost"]) == Decimal("0.000576")
    assert Decimal(scenario["cost_measured"]["cost_per_successful_request"]) == Decimal("0.000192")
