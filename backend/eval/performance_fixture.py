"""Real localhost TCP + real RAG API routes, synthetic model/session dependencies."""
import asyncio
import socket
import time
import uuid

import uvicorn

from app.api.deps import get_pipeline, get_principal, get_session_service
from app.core.config import settings
from app.core.performance import _collector
from app.core.security import Principal
from app.main import create_app


class Pipeline:
    model = "fixture-model"

    def _record(self, started):
        collector = _collector.get()
        if collector:
            collector.append("calls", {"kind": "llm", "model": self.model,
                "elapsed_ms": (time.perf_counter() - started) * 1000, "outcome": "success",
                "input_tokens": 128, "output_tokens": 32, "error_type": None})

    def query(self, query, top_k=None, filter_dict=None):
        start = time.perf_counter()
        time.sleep(.015)
        self._record(start)
        return {"query": query, "answer": "压测夹具答案", "sources": [],
                "retrieved_count": 0, "hidden_count": 0}

    def query_with_history(self, query, history, top_k=None, filter_dict=None):
        return self.query(query, top_k, filter_dict)

    def stream_chat(self, query, history, top_k=None, filter_dict=None):
        start = time.perf_counter()
        yield {"type": "meta", "sources": [], "retrieved_count": 0, "hidden_count": 0}
        time.sleep(.01)
        yield {"type": "delta", "text": "压测夹具"}
        time.sleep(.005)
        yield {"type": "delta", "text": "答案"}
        self._record(start)
        yield {"type": "done", "answer": "压测夹具答案"}


class Sessions:
    def __init__(self):
        self.rows = {}
    async def create(self, tenant_id):
        sid = uuid.uuid4().hex
        self.rows[sid] = []
        return sid
    async def exists(self, sid, tenant_id):
        return sid in self.rows
    async def get_history(self, sid, tenant_id):
        return list(self.rows[sid])
    async def append(self, sid, tenant_id, message):
        self.rows[sid].append(message)


class FixtureServer:
    def __init__(self, ledger):
        self.ledger, self.server, self.task, self.socket = ledger, None, None, None
        self.previous_log = settings.performance_log_file

    async def start(self):
        settings.performance_log_file = str(self.ledger)
        app = create_app()
        pipeline, sessions = Pipeline(), Sessions()
        app.dependency_overrides[get_pipeline] = lambda: pipeline
        app.dependency_overrides[get_session_service] = lambda: sessions
        app.dependency_overrides[get_principal] = lambda: Principal("performance_fixture", True, "employee")
        # No Agent fixture: its real tool chain must not be invoked by synthetic tests.
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        port = self.socket.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning"))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        for _ in range(100):
            if self.server.started:
                return f"http://127.0.0.1:{port}"
            if self.task.done():
                await self.task
                raise RuntimeError("Fixture server failed to start")
            await asyncio.sleep(.01)
        await self.stop()
        raise TimeoutError("Fixture startup timed out")

    async def stop(self):
        try:
            if self.server:
                self.server.should_exit = True
            if self.task:
                await asyncio.wait_for(self.task, timeout=5)
        finally:
            if self.socket:
                self.socket.close()
            settings.performance_log_file = self.previous_log
