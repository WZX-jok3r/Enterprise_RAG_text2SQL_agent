"""Opt-in request-local provider measurements; no prompt, answer or credentials."""
import asyncio
import json
import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

import requests
from app.core.config import settings
from app.core.trace import get_trace_id

logger = logging.getLogger(__name__)
_collector = ContextVar("performance_collector", default=None)
_file_lock = threading.Lock()


class Collector:
    def __init__(self):
        self.calls, self.stages = [], []
        self.lock = threading.Lock()

    def append(self, field, row):
        with self.lock:
            getattr(self, field).append(row)


@contextmanager
def capture_measurements():
    collector = Collector()
    token = _collector.set(collector)
    try:
        yield collector
    finally:
        _collector.reset(token)


def _tokens(usage, kind):
    def valid(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if not isinstance(usage, dict):
        return None, None
    inp = usage.get("prompt_tokens", usage.get("input_tokens"))
    out = usage.get("completion_tokens", usage.get("output_tokens"))
    if kind != "llm":
        inp = inp if valid(inp) else usage.get("total_tokens")
        out = 0 if valid(inp) else None
    return inp if valid(inp) else None, out if valid(out) else None


def measured_post(kind, model, url, **kwargs):
    collector = _collector.get()
    if collector is None:
        return requests.post(url, **kwargs)
    start = time.perf_counter()
    usage = None
    def finish(outcome, error_type=None):
        inp, out = _tokens(usage, kind)
        collector.append("calls", {"kind": kind, "model": model,
            "elapsed_ms": (time.perf_counter() - start) * 1000, "outcome": outcome,
            "input_tokens": inp, "output_tokens": out, "error_type": error_type})
    try:
        response = requests.post(url, **kwargs)
        if not kwargs.get("stream"):
            try:
                usage = response.json().get("usage")
            except (ValueError, AttributeError):
                pass
        response.raise_for_status()
    except Exception as exc:
        finish("error", type(exc).__name__)
        raise
    if not kwargs.get("stream"):
        finish("success")
        return response

    class StreamResponse:
        def __getattr__(self, name):
            return getattr(response, name)

        def iter_lines(self, *args, **kw):
            nonlocal usage
            outcome, error = "incomplete", None
            try:
                for raw in response.iter_lines(*args, **kw):
                    line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
                    if line.startswith("data:"):
                        body = line[5:].strip()
                        if body == "[DONE]":
                            outcome = "success"
                        else:
                            try:
                                data = json.loads(body)
                                if data.get("usage"):
                                    usage = data["usage"]
                            except (ValueError, AttributeError):
                                pass
                    yield raw
            except Exception as exc:
                outcome, error = "error", type(exc).__name__
                raise
            finally:
                finish(outcome, error)
                response.close()
    return StreamResponse()


def timed_stage(name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            collector = _collector.get()
            if collector is None:
                return fn(*args, **kwargs)
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                collector.append("stages", {"name": name, "elapsed_ms": (time.perf_counter() - start) * 1000})
        return wrapped
    return decorate


def append_record(path, record):
    line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock, dest.open("a", encoding="utf-8") as file:
        file.write(line)


class PerformanceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = settings.performance_log_file
        if scope["type"] != "http" or not path:
            return await self.app(scope, receive, send)
        start = time.perf_counter()
        status, completed = 500, False
        async def observed_send(message):
            nonlocal status, completed
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True
        with capture_measurements() as collector:
            try:
                await self.app(scope, receive, observed_send)
            finally:
                record = {"schema_version": 1, "request_id": get_trace_id(),
                          "status": status, "completed": completed,
                          "server_elapsed_ms": (time.perf_counter() - start) * 1000,
                          "calls": collector.calls, "stages": collector.stages}
                try:
                    await asyncio.to_thread(append_record, path, record)
                except Exception as exc:
                    logger.warning("Performance record unavailable: %s", type(exc).__name__)
