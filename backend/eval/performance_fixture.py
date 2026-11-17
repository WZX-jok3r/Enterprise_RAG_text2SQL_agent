"""Real localhost TCP + real RAG API routes, synthetic model/session dependencies."""
import asyncio
import socket
import time
import uuid
from unittest.mock import patch

import uvicorn

from app.api.deps import get_oneshot_answerer, get_principal, get_session_service
from app.core.config import settings
from app.core.performance import _collector
from app.core.security import Principal
from app.main import create_app


def _record(started, *, model="fixture-model"):
    """写一条合成 LLM 调用（压测的成本/延迟归因需要它）。"""
    collector = _collector.get()
    if collector:
        collector.append("calls", {"kind": "llm", "model": model,
            "elapsed_ms": (time.perf_counter() - started) * 1000, "outcome": "success",
            "input_tokens": 128, "output_tokens": 32, "error_type": None})


class AnswerOnce:
    """替身：`POST /api/v1/query` 的单轮编排（一次"检索"+一次"生成"）。

    Step 3 起 /query 走 app.rag.oneshot.answer_once，替代了原先的 RAGPipeline。
    """

    def __call__(self, query, top_k=None, filter_dict=None):
        start = time.perf_counter()
        time.sleep(.015)
        _record(start)
        return {"query": query, "answer": "压测夹具答案", "sources": [],
                "retrieved_count": 0, "hidden_count": 0}


class AgentLoopStub:
    """替身：`/agent/stream` 的事件流（不触发真实检索/SQL/模型）。

    事件形状必须与 AgentLoop 一致（route -> delta* -> done），
    因为压测要量的正是 SSE 桥接与帧序列化这两层开销。
    """

    def run(self, question, **kwargs):
        start = time.perf_counter()
        yield {"type": "route", "target": "doc", "reason": "fixture"}
        time.sleep(.01)
        yield {"type": "delta", "text": "压测夹具"}
        time.sleep(.005)
        yield {"type": "delta", "text": "答案"}
        _record(start)
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
        self._agent_patch = None
        self.previous_log = settings.performance_log_file

    async def start(self):
        settings.performance_log_file = str(self.ledger)
        app = create_app()
        sessions = Sessions()
        app.dependency_overrides[get_oneshot_answerer] = lambda: AnswerOnce()
        # Agent 的真实工具链（Qdrant / SQL）绝不能被合成压测触发。
        # ⚠️ 这里不能用 dependency_overrides：agent.py 里是模块级的
        #    `loop = get_agent_loop()`（不是 Depends），依赖覆盖对它无效，
        #    只能替换模块全局。少了这一步，压测会打到真实 Qdrant/LLM。
        self._agent_patch = patch("app.api.v1.agent.get_agent_loop", lambda: AgentLoopStub())
        self._agent_patch.start()
        app.dependency_overrides[get_session_service] = lambda: sessions
        app.dependency_overrides[get_principal] = lambda: Principal("performance_fixture", True, "employee")
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
            if self._agent_patch:
                self._agent_patch.stop()
                self._agent_patch = None
            if self.socket:
                self.socket.close()
            settings.performance_log_file = self.previous_log
