import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import requests
from fastapi import FastAPI

from app.core.config import settings
from app.core.performance import PerformanceMiddleware, capture_measurements, measured_post
from app.core.trace import TraceIdMiddleware


class Response:
    def __init__(self, usage=None, lines=(), error=None):
        self.usage, self.lines, self.error, self.closed = usage, lines, error, False
    def json(self):
        return {"usage": self.usage}
    def raise_for_status(self):
        if self.error:
            raise self.error
    def iter_lines(self):
        yield from self.lines
    def close(self):
        self.closed = True


def test_capture_usage_for_embedding_and_failed_attempt(monkeypatch):
    responses = iter([Response({"total_tokens": 12}), Response(error=requests.HTTPError("secret error body"))])
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: next(responses))
    with capture_measurements() as collector:
        measured_post("embedding", "fake-embed", "https://unused", json={"input": "secret prompt"})
        with pytest.raises(requests.HTTPError):
            measured_post("llm", "fake", "https://unused")
    assert len(collector.calls) == 2
    assert collector.calls[0]["input_tokens"] == 12 and collector.calls[0]["output_tokens"] == 0
    assert collector.calls[1]["outcome"] == "error" and collector.calls[1]["input_tokens"] is None
    assert "secret" not in json.dumps(collector.calls)


def test_stream_usage_uses_last_cumulative_frame(monkeypatch):
    response = Response(lines=[b'data: {"usage":{"prompt_tokens":10,"completion_tokens":1}}',
                              b'data: {"usage":{"prompt_tokens":10,"completion_tokens":5}}', b'data: [DONE]'])
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)
    with capture_measurements() as collector:
        assert list(measured_post("llm", "fake", "unused", stream=True).iter_lines()) == response.lines
    assert len(collector.calls) == 1
    assert collector.calls[0]["output_tokens"] == 5 and collector.calls[0]["input_tokens"] == 10
    assert collector.calls[0]["outcome"] == "success" and response.closed


def test_incomplete_stream_is_recorded_with_unknown_usage(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response(lines=[b'data: {}']))
    with capture_measurements() as collector:
        list(measured_post("llm", "fake", "unused", stream=True).iter_lines())
    assert collector.calls[0]["outcome"] == "incomplete"
    assert collector.calls[0]["input_tokens"] is None


async def test_request_context_survives_threadpool_without_cross_request_leak(monkeypatch, tmp_path):
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setattr(settings, "performance_log_file", str(ledger))
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response({"prompt_tokens": kwargs["json"]["n"], "completion_tokens": 1}))
    app = FastAPI()
    app.add_middleware(PerformanceMiddleware)
    app.add_middleware(TraceIdMiddleware)
    @app.get("/test/{number}")
    def route(number: int):
        measured_post("llm", "fake", "unused", json={"n": number})
        return {"ok": True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(*(client.get(f"/test/{i}", headers={"X-Request-ID": f"request-{i}"}) for i in range(1, 9)))
    assert all(r.status_code == 200 for r in responses)
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 8
    for row in rows:
        assert len(row["calls"]) == 1 and row["completed"]
        assert row["calls"][0]["input_tokens"] == int(row["request_id"].split("-")[-1])


def test_missing_usage_is_not_zero_tokens(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())
    with capture_measurements() as collector:
        measured_post("rerank", "fake-rerank", "unused")
    assert collector.calls[0]["input_tokens"] is None and collector.calls[0]["output_tokens"] is None


def test_actual_llm_stream_gateway_closes_and_records_before_return(monkeypatch):
    from app.llm.provider import LLMClient
    client = object.__new__(LLMClient)
    client.model, client.base_url, client.api_key = "fake", "http://unused", "fixture-key"
    client.temperature, client.max_tokens = 0, 100
    response = Response(lines=[b'data: {"choices":[{"delta":{"content":"text"}}],"usage":{"prompt_tokens":8,"completion_tokens":1}}',
                              b'data: [DONE]'])
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)
    with capture_measurements() as collector:
        assert list(client.stream([{"role": "user", "content": "private"}])) == ["text"]
        assert len(collector.calls) == 1 and collector.calls[0]["outcome"] == "success"
    assert response.closed
