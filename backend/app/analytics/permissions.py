"""Database column privileges for the restricted SQL login.

No table-level/default SELECT grants: new tables remain inaccessible until
their column grants are provisioned. RLS applies independently to both logins.
"""
from sqlalchemy import text

from app.core.config import settings


def sensitive_sql_reason(sql: str, redact: frozenset[str]) -> str:
    import sqlglot
    from sqlglot import exp
    denied = {entry.rsplit('.', 1)[-1].lower() for entry in redact}
    try:
        trees = sqlglot.parse(sql, dialect="postgres")
        if len(trees) != 1 or trees[0] is None:
            return "权限校验失败：仅允许单条查询。"
        if any(col.name.lower() in denied for col in trees[0].find_all(exp.Column)):
            return "当前账号无权查询敏感字段及其派生结果，请只查询允许的字段。"
    except Exception:
        return "权限校验失败：无法解析查询。"
    return ""


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def column_grants(table: str, columns: list[str]) -> list[str]:
    role = quote_identifier(settings.analytics_employee_user)
    relation = 'public.' + quote_identifier(table)
    denied = {entry.rsplit('.', 1)[-1] for entry in settings.redact_column_set}
    allowed = [c for c in columns if c.lower() not in denied]
    statements = [f"REVOKE ALL PRIVILEGES ON TABLE {relation} FROM {role}"]
    if columns:
        all_cols = ', '.join(quote_identifier(c) for c in columns)
        # Table-level REVOKE does not remove pre-existing column privileges.
        statements.append(f"REVOKE SELECT ({all_cols}) ON {relation} FROM {role}")
        statements.append(f"REVOKE SELECT ON {relation} FROM PUBLIC")
        statements.append(f"REVOKE SELECT ({all_cols}) ON {relation} FROM PUBLIC")
    if allowed:
        names = ', '.join(quote_identifier(c) for c in allowed)
        statements.append(f"GRANT SELECT ({names}) ON {relation} TO {role}")
    return statements


def grant_table_columns(conn, table: str, columns: list[str]) -> None:
    for sql in column_grants(table, columns):
        conn.execute(text(sql))


def ensure_employee_role(conn) -> None:
    name = settings.analytics_employee_user
    if name in {settings.analytics_ro_user, settings.postgres_user}:
        raise ValueError("Restricted SQL requires a separate login")
    role = quote_identifier(name)
    exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:n"), {"n": name}).first()
    if not exists:
        conn.execute(text(f"CREATE ROLE {role} LOGIN"))
    conn.execute(text(
        f"ALTER ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOINHERIT NOBYPASSRLS NOREPLICATION"
    ))
    # Existing memberships could permit SET ROLE into a privileged role.
    memberships = conn.execute(text(
        "SELECT parent.rolname FROM pg_auth_members m "
        "JOIN pg_roles parent ON parent.oid=m.roleid "
        "JOIN pg_roles child ON child.oid=m.member WHERE child.rolname=:n"
    ), {"n": name}).scalars().all()
    for parent in memberships:
        conn.execute(text(f"REVOKE {quote_identifier(parent)} FROM {role}"))
    password = settings.effective_employee_password.replace("'", "''")
    conn.exec_driver_sql(f"ALTER ROLE {role} PASSWORD '{password}'")
    conn.execute(text(f"GRANT CONNECT ON DATABASE {quote_identifier(settings.analytics_db)} TO {role}"))
    conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    conn.execute(text(f"REVOKE CREATE ON SCHEMA public FROM {role}"))
    conn.execute(text(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {role}"))
    conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM {role}"))
    conn.execute(text(f"ALTER ROLE {role} SET default_transaction_read_only=on"))
    conn.execute(text(f"ALTER ROLE {role} SET statement_timeout='5s'"))
    conn.execute(text(f"ALTER ROLE {role} SET idle_in_transaction_session_timeout='10s'"))
    tables = conn.execute(text(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind IN ('r', 'p')"
    )).scalars().all()
    for table in tables:
        columns = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"
        ), {"t": table}).scalars().all()
        grant_table_columns(conn, table, list(columns))
