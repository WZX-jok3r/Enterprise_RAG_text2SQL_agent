"""Agent API 路由测试（TestClient + 依赖覆盖，hermetic）。

重点验证：
  1. 既有 4 类事件的字段契约未被破坏（扩帧不改帧）
  2. 新增 6 类 Agent 事件按预期出现
  3. 旧路由 /chat/stream 完全没被动到（回归保护）
  4. /agent/route 纯函数不触发外部依赖
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app.agent.loop import AgentLoop
from app.agent.tools import ToolOutcome
from app.api.deps import get_principal, get_session_service
from app.core.security import Principal
from app.llm.tool_client import ToolChatResult
from app.main import create_app


# ==================== 测试替身 ====================

class FakeSessionService:
    """内存会话服务，避免依赖 PG/Redis。"""

    def __init__(self) -> None:
        self.sessions: Dict[str, List[Dict[str, str]]] = {}

    async def create(self, tenant_id=None) -> str:
        sid = f"sess{len(self.sessions) + 1}"
        self.sessions[sid] = []
        return sid

    async def exists(self, session_id: str, tenant_id=None) -> bool:
        return session_id in self.sessions

    async def get_history(self, session_id: str, tenant_id=None) -> List[Dict[str, str]]:
        return list(self.sessions.get(session_id, []))

    async def append(self, session_id: str, tenant_id, msg: Dict[str, str]) -> None:
        self.sessions.setdefault(session_id, []).append(msg)


class FakeLLM:
    def __init__(self, content: str = "答案是 1042 人") -> None:
        self.content = content
        self.calls = 0

    def chat_with_tools(self, messages, tools=None, temperature=None, **kw) -> ToolChatResult:
        self.calls += 1
        return ToolChatResult(content=self.content, tool_calls=[], finish_reason="stop")


class FakeSqlResult:
    """假 Text2SQL 结果（结构对齐 Text2SqlResult 的字段）。"""

    def __init__(self) -> None:
        self.ok = True
        self.question = "q"
        self.sql = "SELECT count(*) FROM employees"
        self.columns = ["count"]
        self.rows = [[1042]]
        self.row_count = 1
        self.elapsed_ms = 4
        self.truncated = False
        self.answer = "共 1042 人"
        self.needs_clarification = False
        self.clarification = ""
        self.attempts = 1
        self.errors: List[str] = []
        self.redacted_columns: List[str] = []


class FakeTools:
    """假工具集。签名与 AgentTools 保持一致（含 redact / actor）。"""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self.last_redact = None
        self.last_actor = ""

    def dispatch(self, name, args, tenant_id=None, redact=None, actor="") -> ToolOutcome:
        self.calls.append(name)
        self.last_redact = redact
        self.last_actor = actor
        if name == "kb_search":
            return ToolOutcome(
                ok=True, name=name, observation="路由器质保 24 个月",
                summary="检索到 1 段相关内容",
                payload={"chunks": [], "dropped": 3, "retrieved_count": 1,
                         "sources": [{"source": "warranty_policy.md", "format": "md",
                                      "score": 0.91}]},
            )
        return ToolOutcome(ok=True, name=name, observation="ok", summary="ok",
                           payload={"sql": "SELECT 1", "row_count": 2, "elapsed_ms": 3,
                                    "truncated": False})

    def sql_answer(self, question, tenant_id=None, redact=None, actor=""):
        """模拟 Text2SQL 引擎（不碰数据库）。"""
        self.calls.append("sql_answer")
        self.last_redact = redact
        self.last_actor = actor
        return FakeSqlResult()


def parse_sse(text: str) -> List[Dict[str, Any]]:
    events = []
    for frame in text.split("\n\n"):
        line = next((l for l in frame.split("\n") if l.startswith("data:")), None)
        if not line:
            continue
        body = line[len("data:"):].strip()
        if body:
            events.append(json.loads(body))
    return events


@pytest.fixture
def client(monkeypatch) -> TestClient:
    app = create_app()
    svc = FakeSessionService()
    app.dependency_overrides[get_principal] = lambda: Principal(tenant_id="a", authenticated=True)
    app.dependency_overrides[get_session_service] = lambda: svc
    # 用假 Agent Loop 替换单例，避免真连 LLM/DB/Qdrant
    fake_loop = AgentLoop(tools=FakeTools(), llm=FakeLLM())
    monkeypatch.setattr("app.api.v1.agent.get_agent_loop", lambda: fake_loop)
    return TestClient(app)


# ==================== /agent/route（纯函数）====================

class TestAgentRouteEndpoint:
    def test_route_endpoint_is_pure(self, client):
        """/agent/route 不触发 LLM/DB，应秒回。"""
        r = client.post("/api/v1/agent/route", json={"message": "员工表有多少人？"})
        assert r.status_code == 200
        body = r.json()
        assert body["route"] == "sql"
        assert body["matched_aggregation"] and body["matched_column"]

    def test_route_endpoint_rag(self, client):
        r = client.post("/api/v1/agent/route", json={"message": "路由器的质保期是多久？"})
        assert r.json()["route"] == "rag"

    def test_route_endpoint_llm_decide(self, client):
        r = client.post("/api/v1/agent/route", json={"message": "你好"})
        assert r.json()["route"] == "llm_decide"

    def test_rejects_empty_message(self, client):
        assert client.post("/api/v1/agent/route", json={"message": ""}).status_code == 422

    def test_rejects_invalid_force_route(self, client):
        r = client.post("/api/v1/agent/stream", json={"message": "x", "force_route": "evil"})
        assert r.status_code == 422


# ==================== /agent/stream ====================

class TestAgentStreamEvents:
    def test_rag_question_event_sequence(self, client):
        r = client.post("/api/v1/agent/stream", json={"message": "路由器的质保期是多久？"})
        assert r.status_code == 200
        events = parse_sse(r.text)
        types = [e["type"] for e in events]

        assert types[0] == "route"
        assert "tool_call" in types and "tool_result" in types
        assert "meta" in types, f"必须补发 meta 帧，实际: {types}"
        assert types[-1] == "done"

    def test_frozen_meta_fields_intact(self, client):
        """扩帧不得改帧：meta 的字段集合必须与改造前完全一致。"""
        r = client.post("/api/v1/agent/stream", json={"message": "路由器的质保期是多久？"})
        meta = next(e for e in parse_sse(r.text) if e["type"] == "meta")
        assert set(meta.keys()) == {"type", "sources", "retrieved_count", "hidden_count"}
        assert meta["retrieved_count"] == 1
        assert meta["hidden_count"] == 3

    def test_hidden_count_is_preserved(self, client):
        """被低相关过滤的条数必须透传（前端「已隐藏 N 条」徽标依赖它）。"""
        r = client.post("/api/v1/agent/stream", json={"message": "路由器的质保期是多久？"})
        meta = next(e for e in parse_sse(r.text) if e["type"] == "meta")
        assert meta["hidden_count"] == 3

    def test_done_carries_session_and_answer(self, client):
        r = client.post("/api/v1/agent/stream", json={"message": "路由器的质保期是多久？"})
        done = next(e for e in parse_sse(r.text) if e["type"] == "done")
        assert done["answer"]
        assert done["session_id"]
        assert "history_length" in done

    def test_force_route_sql(self, client):
        r = client.post("/api/v1/agent/stream",
                        json={"message": "随便问问", "force_route": "sql"})
        types = [e["type"] for e in parse_sse(r.text)]
        assert types[0] == "route"
        assert "sql" in types, f"强制 sql 路由应产出 sql 事件，实际: {types}"

    def test_force_route_rag(self, client):
        r = client.post("/api/v1/agent/stream",
                        json={"message": "随便问问", "force_route": "rag"})
        types = [e["type"] for e in parse_sse(r.text)]
        assert "meta" in types

    def test_all_events_json_serializable(self, client):
        r = client.post("/api/v1/agent/stream", json={"message": "路由器的质保期是多久？"})
        for e in parse_sse(r.text):
            json.dumps(e, ensure_ascii=False)

    def test_sse_headers_prevent_buffering(self, client):
        """nginx 必须被告知不要缓冲，否则流式变整块。"""
        r = client.post("/api/v1/agent/stream", json={"message": "路由器的质保期是多久？"})
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers.get("x-accel-buffering") == "no"

    def test_multi_turn_reuses_session(self, client):
        r1 = client.post("/api/v1/agent/stream", json={"message": "路由器的质保期是多久？"})
        sid = next(e for e in parse_sse(r1.text) if e["type"] == "done")["session_id"]
        r2 = client.post("/api/v1/agent/stream",
                         json={"message": "那安装呢？", "session_id": sid})
        assert r2.status_code == 200
        done = next(e for e in parse_sse(r2.text) if e["type"] == "done")
        assert done["session_id"] == sid
        assert done["history_length"] >= 4      # 两轮 user+assistant


# ==================== 旧路由回归保护 ====================

class TestLegacyRoutesUnchanged:
    """新增 Agent 路由**不得**影响既有路由的契约。"""

    def test_openapi_contains_both(self, client):
        spec = client.get("/openapi.json").json()
        paths = spec["paths"]
        assert "/api/v1/chat/stream" in paths, "既有 chat/stream 路由必须仍在"
        assert "/api/v1/agent/stream" in paths
        assert "/api/v1/query" in paths
        assert "/api/v1/documents" in paths

    def test_agent_route_does_not_shadow_chat(self, client):
        """路径不冲突：/agent/* 与 /chat/* 各自独立。"""
        spec = client.get("/openapi.json").json()
        assert "/api/v1/agent/route" in spec["paths"]
        assert "/api/v1/chat" in spec["paths"]
