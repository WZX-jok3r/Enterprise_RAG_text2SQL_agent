"""Real PostgreSQL proof: aliases/expressions cannot bypass column privileges.

Creates only uniquely named probe objects; skips when local PostgreSQL is absent.
"""
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from app.analytics.ddl import build_rls_sql
from app.analytics.executor import SqlExecutor
from app.analytics.permissions import grant_table_columns, quote_identifier
from app.core.config import settings


@pytest.fixture(autouse=True)
def no_external_audit(monkeypatch):
    monkeypatch.setattr(settings, "sql_audit_enabled", False)


@pytest.fixture(scope="module")
def restricted_db():
    admin = create_engine(settings.sync_analytics_url, connect_args={"connect_timeout": 2})
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        admin.dispose()
        pytest.skip("Local analytics PostgreSQL is unavailable")
    suffix = uuid.uuid4().hex[:16]
    role_name = "probe_employee_" + suffix
    table = "probe_columns_" + suffix
    role = quote_identifier(role_name)
    relation = quote_identifier(table)
    password = uuid.uuid4().hex
    employee = None
    provisioned = False
    old_user = settings.analytics_employee_user
    old_redact = settings.sql_redact_columns
    try:
        settings.analytics_employee_user = role_name
        settings.sql_redact_columns = f"{table}.salary"
        with admin.begin() as conn:
            conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS NOINHERIT"))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            conn.execute(text(f"CREATE TABLE {relation} (id int, salary int, tenant_id text, is_shared bool)"))
            conn.execute(text(f"INSERT INTO {relation} VALUES (1,900,'a',false),(2,800,'b',false)"))
            for sql in build_rls_sql(table):
                conn.execute(text(sql))
            grant_table_columns(conn, table, ["id", "salary", "tenant_id", "is_shared"])
        provisioned = True
        url = URL.create("postgresql+psycopg", username=role_name, password=password,
                         host=settings.postgres_host, port=settings.postgres_port,
                         database=settings.analytics_db)
        employee = create_engine(url)
        yield admin, employee, table
    finally:
        settings.analytics_employee_user = old_user
        settings.sql_redact_columns = old_redact
        if employee is not None:
            employee.dispose()
        if provisioned:
            with admin.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS {relation}"))
                conn.execute(text(f"DROP OWNED BY {role}"))
                conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        admin.dispose()


@pytest.mark.parametrize("projection", [
    "salary AS s", "salary::text", "AVG(salary)", "*", "to_json(e)",
])
def test_database_itself_blocks_sensitive_projections(restricted_db, projection):
    _, employee, table = restricted_db
    # Direct connection deliberately bypasses the application's AST check.
    with employee.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id','a',true)"))
        with pytest.raises(Exception, match="permission denied"):
            conn.execute(text(f'SELECT {projection} FROM "{table}" e')).fetchall()


def test_restricted_executor_keeps_rls_and_no_privilege_fallback(restricted_db):
    admin, employee, table = restricted_db
    executor = SqlExecutor(engine=admin, employee_engine=employee)
    redact = frozenset({f"{table}.salary"})
    for tenant, expected in [("a", [[1]]), ("b", [[2]]), ("c", []), (None, [])]:
        result = executor.execute(f'SELECT id FROM "{table}"', tenant_id=tenant, redact=redact)
        assert result.ok, result.error
        assert result.rows == expected
    # COUNT(*) remains usable, SELECT * is rejected even without EXPLAIN.
    count = executor.execute(f'SELECT count(*) FROM "{table}"', tenant_id="a", redact=redact)
    assert count.ok and count.rows == [[1]]
    result = executor.execute(f'SELECT * FROM "{table}"', tenant_id="a",
                              redact=redact, skip_explain=True)
    assert not result.ok and not result.rows
