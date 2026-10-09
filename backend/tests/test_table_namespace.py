"""表命名空间 / 覆盖归属保护的测试（真连 PostgreSQL）。

## 本文件针对的真实缺陷：跨租户**数据破坏**（比越权读取更严重）

目标表名由 `文件名 + sheet 名` 生成（`schema_infer.normalize_table_name`），
**不含租户**；而建表走 `DROP TABLE IF EXISTS ... CASCADE`。实测复现：

    租户 A 上传 shared_report.xlsx(sheet=Shared) -> 表 shared，A 的 2 行
    租户 B 上传**同名文件**                      -> 表名同样是 shared
    DROP 执行 -> A 的数据被**物理删除**，只剩 B 的 2 行

⚠️ RLS 挡不住这个：行级安全只管"读"，`DROP TABLE` 是 DDL。
一旦执行，A 的数据**不可逆地消失** —— 所以这比"读到别人的数据"更严重。

修复口径：覆盖前做**归属检查**（`ddl.assert_can_replace`），
发现表里有别人的行就抛 `TableOwnershipError` 中断入库。
刻意选择"拒绝"而非"自动改名"：自动改名会让同一份文件在不同租户下
得到不同表名，LLM 的表枚举与用户预期都变得不可预测；
"拒绝 + 明确报错"把决定权交回给人。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.analytics import ddl
from app.analytics.ddl import TableOwnershipError
from app.analytics.schema_infer import infer_schema, normalize_table_name
from app.core.config import settings

TENANT_A = "ns-test-a"
TENANT_B = "ns-test-b"
TABLE = "ns_probe_tbl"


def _available() -> bool:
    try:
        e = create_engine(settings.sync_analytics_url, connect_args={"connect_timeout": 5})
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        e.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _available(), reason="需要 kb_analytics")


@pytest.fixture
def engine():
    e = create_engine(settings.sync_analytics_url)
    yield e
    with e.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {ddl.q(TABLE)} CASCADE"))
    e.dispose()


def _schema():
    return infer_schema([["名称", "数值"], ["x", "1"]], table_name=TABLE,
                        display_name="探针", description="d")


def _seed(engine, tenant: str, is_shared: bool = False) -> None:
    """用生产路径建表并写入一行（模拟一次真实上传）。"""
    schema = _schema()
    ddl.create_or_replace_table(engine, schema, tenant_id=tenant, is_shared=is_shared)
    # 两个业务列都要给值：infer_schema 推断出的列是 NOT NULL
    with engine.begin() as c:
        c.execute(text(
            f"INSERT INTO {ddl.q(TABLE)} ({ddl.q('col_1')}, {ddl.q('col_2')}, "
            f"{ddl.q(ddl.TENANT_COLUMN)}, {ddl.q(ddl.SHARED_COLUMN)}) "
            f"VALUES ('x', 1, :t, :s)"),
            {"t": tenant or ddl.UNOWNED_TENANT, "s": is_shared})


# ==================== 表名确实不含租户（缺陷的前提）====================

class TestTableNameHasNoTenantNamespace:
    def test_name_is_tenant_independent(self):
        """同名文件在不同租户下算出**同一个表名** —— 这正是缺陷的根源。

        这条测试是"记录事实"，不是"断言期望"：
        修复方式是**覆盖保护**（下面的类），不是改命名规则
        （改命名会牵动元数据、LLM 表枚举与既有语料）。
        """
        n1 = normalize_table_name("shared_report.xlsx", "Shared")
        n2 = normalize_table_name("shared_report.xlsx", "Shared")
        assert n1 == n2 == "shared"


# ==================== 核心：覆盖保护 ====================

class TestCrossTenantOverwriteIsBlocked:
    def test_other_tenant_rows_block_overwrite(self, engine):
        """核心断言：表里有别的租户的行时，**拒绝覆盖**。"""
        _seed(engine, TENANT_A)
        with pytest.raises(TableOwnershipError) as ei:
            _seed(engine, TENANT_B)
        assert TENANT_A in str(ei.value), "错误信息应指出是谁的数据，便于排查"

    def test_first_tenant_data_survives(self, engine):
        """拒绝之后，原租户的数据必须**完好无损**。"""
        _seed(engine, TENANT_A)
        with pytest.raises(TableOwnershipError):
            _seed(engine, TENANT_B)

        with engine.connect() as c:
            rows = c.execute(text(
                f"SELECT {ddl.q(ddl.TENANT_COLUMN)}, count(*) FROM {ddl.q(TABLE)} "
                f"GROUP BY 1")).fetchall()
        assert rows == [(TENANT_A, 1)], f"A 的数据被破坏: {rows}"

    def test_same_tenant_can_reupload(self, engine):
        """同一租户重复上传（重灌）必须仍然允许 —— 这是正常业务流程。"""
        _seed(engine, TENANT_A)
        _seed(engine, TENANT_A)          # 不应抛异常
        with engine.connect() as c:
            n = c.execute(text(f"SELECT count(*) FROM {ddl.q(TABLE)}")).scalar_one()
        assert n == 1, "重灌应覆盖为自己的 1 行"

    def test_no_existing_table_is_allowed(self, engine):
        """表不存在时正常新建。"""
        _seed(engine, TENANT_B)
        assert ddl.table_exists(engine, TABLE)

    def test_third_tenant_also_blocked(self, engine):
        _seed(engine, TENANT_A)
        with pytest.raises(TableOwnershipError):
            _seed(engine, "ns-test-c")


class TestSharedCorpusInteractions:
    """共享语料与租户私有数据之间的覆盖也要保护（两个方向）。"""

    def test_private_load_cannot_clobber_shared(self, engine):
        """共享语料不能被某个租户的私有上传覆盖掉。"""
        _seed(engine, "", is_shared=True)
        with pytest.raises(TableOwnershipError) as ei:
            _seed(engine, TENANT_A, is_shared=False)
        assert "共享" in str(ei.value)

    def test_private_data_cannot_be_silently_promoted_to_shared(self, engine):
        """**反向保护**：把某租户的私有表改成共享 = 权限放开，必须被挡住。

        这是我特意加的方向：只防"私有覆盖共享"会漏掉
        "某人用 is_shared=True 把别人的私有数据一次性对所有租户公开"。

        拦截可能由两条分支之一完成（都算正确，因此断言只要求"被拒绝"）：
          - 归属不同（共享装载的归属是 UNOWNED，与 TENANT_A 不同）
          - 命中"私有 → 共享"的显式确认分支
        """
        _seed(engine, TENANT_A, is_shared=False)
        with pytest.raises(TableOwnershipError) as ei:
            _seed(engine, "", is_shared=True)
        msg = str(ei.value)
        assert TENANT_A in msg, "错误信息应指出是谁的数据"
        assert "拒绝" in msg, "应为拒绝覆盖，而不是静默放开权限"

    def test_shared_to_private_with_same_owner_is_also_guarded(self, engine):
        """共享语料不能被"同名归属"的私有装载悄悄改属性。

        归属同名（都是 UNOWNED）时会走到 is_shared 分支，也应拒绝。
        """
        _seed(engine, "", is_shared=True)
        with pytest.raises(TableOwnershipError) as ei:
            _seed(engine, "", is_shared=False)
        assert "共享" in str(ei.value)

    def test_shared_can_be_reloaded_as_shared(self, engine):
        """共享语料重复装载（CLI 反复导入）必须允许。"""
        _seed(engine, "", is_shared=True)
        _seed(engine, "", is_shared=True)     # 不应抛异常
        assert ddl.table_exists(engine, TABLE)


class TestOwnershipInspection:
    def test_reports_tenants_and_shared(self, engine):
        _seed(engine, TENANT_A)
        info = ddl.inspect_table_ownership(engine, TABLE)
        assert info["exists"] is True
        assert info["tenants"] == {TENANT_A: 1}
        assert info["shared_rows"] == 0

    def test_missing_table(self, engine):
        with engine.begin() as c:
            c.execute(text(f"DROP TABLE IF EXISTS {ddl.q(TABLE)} CASCADE"))
        info = ddl.inspect_table_ownership(engine, TABLE)
        assert info["exists"] is False

    def test_legacy_table_without_tenant_column_is_refused(self, engine):
        """老表没有租户列 ⇒ 归属未知 ⇒ 拒绝覆盖（宁可拒绝不可猜）。"""
        with engine.begin() as c:
            c.execute(text(f"DROP TABLE IF EXISTS {ddl.q(TABLE)} CASCADE"))
            c.execute(text(f"CREATE TABLE {ddl.q(TABLE)} (col_1 TEXT)"))
        with pytest.raises(TableOwnershipError) as ei:
            _seed(engine, TENANT_A)
        assert "租户列" in str(ei.value)

    def test_unowned_rows_refuse_foreign_overwrite(self, engine):
        """表里只有"无归属"行时，别的租户不能覆盖（无法确认是否自己所有）。"""
        _seed(engine, "")                       # 无归属装载
        with pytest.raises(TableOwnershipError):
            _seed(engine, TENANT_A)
