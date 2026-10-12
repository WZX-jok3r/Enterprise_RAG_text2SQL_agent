"""Deterministic regressions for request isolation and authorization boundaries."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.loop import AgentLoop
from app.agent.router import Route
from app.agent.tools import ToolOutcome
from app.analytics.executor import SqlExecutor
from app.analytics.permissions import column_grants, sensitive_sql_reason
from app.core.config import Settings, settings
from app.core.security import resolve_principal


@pytest.fixture(autouse=True)
def no_external_audit(monkeypatch):
    monkeypatch.setattr(settings, "sql_audit_enabled", False)


class TenantTools:
    def dispatch(self, name, args, tenant_id=None, redact=None, actor=""):
        return ToolOutcome(
            ok=True, name=name, observation=f"private-{tenant_id}", summary="ok",
            payload={"sources": [{"source": f"{tenant_id}.md"}], "retrieved_count": 1},
        )

    def sql_answer(self, question, tenant_id=None, redact=None, actor=""):
        return SimpleNamespace(
            ok=True, sql=f"SELECT '{tenant_id}'", columns=["owner"], rows=[[tenant_id]],
            row_count=1, elapsed_ms=1, truncated=False, needs_clarification=False,
            clarification="", attempts=1,
        )


class EchoLLM:
    def chat_with_tools(self, messages, tools=None, temperature=None):
        return SimpleNamespace(content=messages[-1]["content"], tool_calls=[])


def test_interleaved_requests_keep_answer_sources_and_trace_separate():
    loop = AgentLoop(tools=TenantTools(), llm=EchoLLM())
    a = loop.run("request-a", tenant_id="a", forced_route=Route.RAG)
    a_events = []
    # Suspend A after its meta frame, before composing its answer. B completes
    # on the same instance, which previously overwrote A's observation.
    for event in a:
        a_events.append(event)
        if event["type"] == "meta":
            break
    b_events = list(loop.run("request-b", tenant_id="b", forced_route=Route.SQL))
    a_events.extend(a)
    a_done = a_events[-1]
    b_done = b_events[-1]
    assert "private-a" in a_done["answer"]
    assert "private-b" not in a_done["answer"]
    assert a_done["_tool_context"] == ""
    assert "'b'" in b_done["_tool_context"]
    assert next(e for e in a_events if e["type"] == "meta")["sources"] == [{"source": "a.md"}]
    assert not any(e["type"] == "meta" for e in b_events)
    assert a_done["agent_stats"]["tool_calls"] == 1
    assert b_done["agent_stats"]["tool_calls"] >= 2


def test_cancelled_stream_does_not_leave_meta_for_next_request():
    loop = AgentLoop(tools=TenantTools(), llm=EchoLLM())
    abandoned = loop.run("a", tenant_id="a", forced_route=Route.RAG)
    next(abandoned)  # route
    next(abandoned)  # tool_call
    next(abandoned)  # tool_result; meta is pending
    abandoned.close()
    events = list(loop.run("b", tenant_id="b", forced_route=Route.LLM_DECIDE))
    assert not any(e["type"] == "meta" for e in events)
    assert events[-1]["_tool_context"] == ""


@pytest.mark.parametrize("sql", [
    "SELECT salary AS s FROM employees",
    "SELECT AVG(salary) FROM employees",
    "SELECT id FROM employees WHERE salary > 1",
    "SELECT id FROM employees ORDER BY salary",
    "WITH t AS (SELECT salary AS x FROM employees) SELECT x FROM t",
    'SELECT "SALARY"::text FROM employees',
    "SELECT concat('', salary) FROM employees",
])
def test_sensitive_derived_queries_rejected_before_database(sql):
    analyst = MagicMock()
    employee = MagicMock()
    ex = SqlExecutor(engine=analyst, employee_engine=employee)
    result = ex.execute(sql, tenant_id="a", redact=frozenset({"employees.salary"}),
                        skip_explain=True)
    assert not result.ok
    analyst.connect.assert_not_called()
    employee.connect.assert_not_called()


def test_restricted_database_error_never_falls_back_to_analyst():
    analyst = MagicMock()
    employee = MagicMock()
    employee.connect.side_effect = RuntimeError("restricted login unavailable")
    ex = SqlExecutor(engine=analyst, employee_engine=employee)
    result = ex.execute("SELECT id FROM employees", tenant_id="a",
                        redact=frozenset({"employees.salary"}), skip_explain=True)
    assert not result.ok
    employee.connect.assert_called_once()
    analyst.connect.assert_not_called()


def test_safe_aggregation_and_column_grants(monkeypatch):
    monkeypatch.setattr(settings, "sql_redact_columns", "employees.salary,employees.email")
    assert not sensitive_sql_reason("SELECT count(*) FROM employees", frozenset({"employees.salary"}))
    grants = column_grants("employees", ["id", "department", "salary", "email"])
    grant = next(s for s in grants if s.startswith("GRANT"))
    assert '"id"' in grant and '"department"' in grant
    assert '"salary"' not in grant and '"email"' not in grant
    assert any(s.startswith("REVOKE SELECT (") for s in grants)


def production_settings(**overrides):
    config = dict(_env_file=None, environment="production", rag_tenant_keys="a=key-a",
                  default_role="employee", analytics_ro_password="analyst-test-pw",
                  analytics_employee_password="employee-test-pw")
    config.update(overrides)
    return Settings(**config)


@pytest.mark.parametrize("overrides", [
    {"rag_tenant_keys": ""}, {"default_role": "analyst"},
    {"analytics_employee_password": ""}, {"analytics_ro_password": ""},
    {"analytics_employee_user": "kb_ro"}, {"rag_tenant_keys": "@unowned@=key"},
    {"rag_tenant_roles": "a=unknown"},
    {"sql_redact_columns": ""},
])
def test_production_configuration_fails_closed(overrides):
    with pytest.raises(ValueError):
        production_settings(**overrides).validate_security()


def test_explicit_analyst_role_is_allowed_in_production():
    production_settings(rag_tenant_roles="a=analyst").validate_security()


def test_reserved_tenant_key_rejected_even_in_development(monkeypatch):
    from app.core.exceptions import ForbiddenError
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "rag_tenant_keys", "@unowned@=bad-key")
    with pytest.raises(ForbiddenError):
        resolve_principal("bad-key", None)


def test_vector_cleanup_contains_server_tenant(monkeypatch):
    from app.services.ingestion_service import _clear_source_vectors
    store = MagicMock()
    store.delete_by_metadata.return_value = 1
    monkeypatch.setattr("app.vector.qdrant.get_vector_store", lambda: store)
    assert _clear_source_vectors("same.md", "tenant-a")
    store.delete_by_metadata.assert_called_once_with(
        {"source": "same.md", settings.tenant_field: "tenant-a"}
    )


async def test_legacy_shared_filename_is_not_unlinked(monkeypatch):
    from app.services import ingestion_service as module
    doc = SimpleNamespace(id=1, source="same.md", tenant_id="a", document_key=None)
    repo = SimpleNamespace(get_by_id=AsyncMock(return_value=doc), delete=AsyncMock())
    session = MagicMock()
    session.execute = AsyncMock()
    session.execute.return_value = MagicMock()
    session.execute.return_value.first.return_value = (2,)  # Another document uses this file.
    session.commit = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(module, "DocumentRepository", lambda s: repo)
    cleanup = MagicMock(return_value=True)
    unlink = MagicMock()
    monkeypatch.setattr(module, "_clear_source_vectors", cleanup)
    monkeypatch.setattr(module, "_remove_physical_file", unlink)
    result = await module.IngestionService(lambda: context, None).delete_document(1, "a")
    assert not result["file_removed"]
    cleanup.assert_called_once_with("same.md", "a")
    unlink.assert_not_called()
    repo.delete.assert_awaited_once_with(doc)


def test_restricted_schema_does_not_mutate_analyst_schema():
    from app.analytics.schema import TableSchema, filter_sensitive_columns
    schema = TableSchema("employees", "employees", "", 2,
                         [{"name": "id", "type": "int"}, {"name": "salary", "type": "int"}])
    restricted = filter_sensitive_columns([schema], frozenset({"employees.salary"}))
    assert [c["name"] for c in restricted[0].columns] == ["id"]
    assert [c["name"] for c in schema.columns] == ["id", "salary"]
