"""setup_db.py - 建库与建只读角色（幂等，可反复执行）。

为什么单独一个模块而不是塞进 Alembic：
- `CREATE DATABASE` **不能在目标库内执行**，必须在维护库（rag）里做，
  而 Alembic 的迁移是针对单一库的 → 语义不匹配。
- 业务库与系统元数据库是**两套独立的演进节奏**：业务表随数据源变化，
  元数据表走 Alembic。耦合会造成"改业务表要动系统迁移"的混乱。

用法：
    python -m app.analytics.setup_db            # 建库 + 建只读角色 + 赋权 + 自检
    python -m app.analytics.setup_db --check    # 只检查现状，不做任何修改

⚠️ 关于 docker 端口映射与 pg_hba 的一个重要事实（实测确认，见 docs/经验教训.md L-004）：
    本项目 postgres 的 pg_hba.conf 有一条 `host all all 127.0.0.1/32 trust`，
    但**从宿主机经 :5432 连入时它不会生效**。原因是 Docker 端口发布走 NAT，
    宿主机看到的 127.0.0.1 在容器侧被改写为**网桥网关地址**（172.x.x.x），
    于是最终匹配到末行的 `host all all all scram-sha-256`，**必须提供口令**。
    因此本模块的连接一律显式带口令（即使本地开发）。
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import settings

logger = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parent / "sql"

_POSTGRES_CONTAINER = "docker_setting-postgres-1"


# ==================== 连接工具 ====================

def _maintenance_engine() -> Engine:
    """连到维护库（默认 rag），用于 CREATE DATABASE。必须 AUTOCOMMIT。"""
    return create_engine(settings.sync_maintenance_url, isolation_level="AUTOCOMMIT")


def _analytics_engine() -> Engine:
    """连到 kb_analytics 库（可写账号），用于建角色与赋权。"""
    return create_engine(settings.sync_analytics_url)


def effective_ro_password() -> str:
    """只读角色的实际口令：配置优先，否则用本地开发默认值（单一事实来源在 config）。"""
    return settings.effective_ro_password


def database_exists(engine: Engine, db_name: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name}
        ).first() is not None


def role_exists(engine: Engine, role_name: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": role_name}
        ).first() is not None


# ==================== 建库 ====================

def ensure_database() -> bool:
    """确保 kb_analytics 存在。返回 True 表示本次新建。"""
    db_name = settings.analytics_db
    engine = _maintenance_engine()
    try:
        if database_exists(engine, db_name):
            logger.info("[analytics] database already exists: %s", db_name)
            return False
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        logger.info("[analytics] database created: %s", db_name)
        return True
    finally:
        engine.dispose()


# ==================== 建角色与赋权 ====================

def ensure_ro_role() -> None:
    """执行 001_init.sql（幂等）：建只读角色、收敛权限、设置口令。"""
    sql_text = (SQL_DIR / "001_init.sql").read_text(encoding="utf-8")
    engine = _analytics_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text(sql_text))
            from app.analytics.permissions import ensure_employee_role
            ensure_employee_role(conn)
            pwd = effective_ro_password()
            escaped = pwd.replace("'", "''")
            conn.exec_driver_sql(f"ALTER ROLE kb_ro WITH PASSWORD '{escaped}'")
            logger.info("[analytics] kb_ro password set (source=%s)",
                        "env" if settings.analytics_ro_password else "dev-default")
    finally:
        engine.dispose()


# ==================== 权限自检 ====================

def _ro_engine() -> Engine:
    """用只读角色连 kb_analytics（显式带口令，原因见模块 docstring）。"""
    url = (
        f"postgresql+psycopg://{settings.analytics_ro_user}:{effective_ro_password()}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.analytics_db}"
    )
    return create_engine(url, isolation_level="AUTOCOMMIT")


def verify_readonly() -> tuple[bool, list[str]]:
    """以 kb_ro 实测：应能连、能 SELECT，且**不能**建表/建 schema。

    这是"闸口思维"——不假设权限配对了，而是真的拿只读账号去试写。
    """
    problems: list[str] = []
    engine = _ro_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1")).first()

            for ddl, label in (
                ("CREATE TABLE _ro_probe (x int)", "CREATE TABLE"),
                ("CREATE SCHEMA _ro_probe_schema", "CREATE SCHEMA"),
            ):
                try:
                    conn.execute(text(ddl))
                    problems.append(f"kb_ro 竟能 {label} —— 权限过高")
                    # 清理（若真的建成功了）
                    if label == "CREATE TABLE":
                        conn.execute(text("DROP TABLE IF EXISTS _ro_probe"))
                    else:
                        conn.execute(text("DROP SCHEMA IF EXISTS _ro_probe_schema"))
                except Exception:
                    pass  # 预期：权限不足
    except Exception as e:  # noqa: BLE001
        problems.append(f"无法以 kb_ro 连接 {settings.analytics_db}: {str(e)[:200]}")
    finally:
        engine.dispose()
    return (len(problems) == 0), problems


def verify_readonly_in_container() -> tuple[bool, list[str]]:
    """在容器内以 kb_ro 实测只读性（走 local trust，不依赖宿主机认证）。

    为什么需要这条独立路径：宿主机的 pg_hba 匹配的是 scram（见模块 docstring），
    口令一旦配错就测不出真实权限。容器内 local trust 绕开口令变量，
    专门验证「权限本身」是否正确 —— 两个维度分开验证，失败时能立刻定位。
    """
    problems: list[str] = []
    probes = [
        ("SELECT count(*) FROM information_schema.tables", "读 information_schema"),
        ("CREATE TABLE _ro_probe (x int)", "CREATE TABLE"),
        ("CREATE SCHEMA _ro_probe_s", "CREATE SCHEMA"),
    ]
    for sql, label in probes:
        try:
            r = subprocess.run(
                ["docker", "exec", _POSTGRES_CONTAINER, "psql", "-U", "kb_ro",
                 "-d", settings.analytics_db, "-tAc", sql],
                capture_output=True, text=True, timeout=30,
            )
            succeeded = r.returncode == 0
        except Exception as e:  # noqa: BLE001
            problems.append(f"容器内探针执行失败({label}): {str(e)[:120]}")
            continue

        expect_success = label.startswith("读")
        if expect_success and not succeeded:
            problems.append(f"kb_ro 无法{label} —— 权限过低")
        if not expect_success and succeeded:
            problems.append(f"kb_ro 竟能 {label} —— 权限过高")
            # 清理
            cleanup = ("DROP TABLE IF EXISTS _ro_probe" if "TABLE" in label
                       else "DROP SCHEMA IF EXISTS _ro_probe_s")
            subprocess.run(
                ["docker", "exec", _POSTGRES_CONTAINER, "psql", "-U", "kb_ro",
                 "-d", settings.analytics_db, "-c", cleanup],
                capture_output=True, text=True, timeout=30,
            )
    return (len(problems) == 0), problems


# ==================== CLI ====================

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Analytics 库与只读角色初始化")
    parser.add_argument("--check", action="store_true", help="只检查现状，不做修改")
    args = parser.parse_args()

    if args.check:
        m = _maintenance_engine()
        try:
            print(f"数据库 {settings.analytics_db!r} 存在: {database_exists(m, settings.analytics_db)}")
            print(f"角色 {settings.analytics_ro_user!r} 存在: {role_exists(m, settings.analytics_ro_user)}")
        finally:
            m.dispose()
        ok1, p1 = verify_readonly()
        ok2, p2 = verify_readonly_in_container()
        print(f"A. 宿主机以 kb_ro 连接自检: {'通过' if ok1 else '失败'}")
        for p in p1:
            print(f"   - {p}")
        print(f"B. 容器内只读权限自检    : {'通过' if ok2 else '失败'}")
        for p in p2:
            print(f"   - {p}")
        return 0 if (ok1 and ok2) else 1

    created = ensure_database()
    print(f"[1/3] 建库          : {'新建' if created else '已存在'} ({settings.analytics_db})")
    ensure_ro_role()
    print(f"[2/3] 只读角色与权限: 已收敛 ({settings.analytics_ro_user})")

    ok_host, prob_host = verify_readonly()
    ok_ctr, prob_ctr = verify_readonly_in_container()
    print(f"[3/3] 只读自检")
    print(f"      A. 宿主机连接 (走 scram): {'通过 ✅' if ok_host else '失败 ❌'}")
    for p in prob_host:
        print(f"         - {p}")
    print(f"      B. 容器内权限 (走 trust) : {'通过 ✅' if ok_ctr else '失败 ❌'}")
    for p in prob_ctr:
        print(f"         - {p}")
    return 0 if (ok_host and ok_ctr) else 1


if __name__ == "__main__":
    sys.exit(main())
