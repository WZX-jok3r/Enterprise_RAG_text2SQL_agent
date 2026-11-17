"""Agent 检索路径的租户隔离测试。

## 本文件针对的真实跨租户泄露（双租户 E2E 实测发现）

经典 RAG 路径有隔离：`api/deps.enforced_filter()` 用 Principal 强制叠加租户键，
实测 `hidden_count=4`（租户 b 的向量被挡住）。

但 **Agent 路径绕过了那个依赖**：`AgentTools.dispatch` 调 `kb_search` 时
**没有传 tenant_id**，于是 `filter_dict=None` ⇒ Retriever 不做任何过滤
⇒ **Agent 能看到所有租户的私有向量**。

实测证据：租户 c 通过 `/agent/stream` 提问，拿到了租户 b 私有上传
的 xlsx 内容与金额（"B项目一 111 / B项目二 222"）。

根因是**两条路径的隔离机制不对称** —— 一个靠 FastAPI 依赖注入，
另一个自己拼 filter，于是后者漏了。这类不对称缺陷极难靠单路径测试发现，
所以下面既测"Agent 路径有隔离"，也测"不能再出现绕过"。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.agent.tools import AgentTools
from app.core.config import settings


class RecordingRetriever:
    """记录每次 search 收到的 filter_dict，供断言隔离是否注入。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def search(self, query: str, top_k: int = 5,
               filter_dict: Optional[Dict[str, Any]] = None):
        self.calls.append({"query": query, "top_k": top_k, "filter": filter_dict})
        return [], 0


@pytest.fixture
def tools():
    r = RecordingRetriever()
    t = AgentTools(retriever=r)
    t._recording = r          # 便于测试取用
    return t


TF = settings.tenant_field


class TestKbSearchEnforcesTenant:
    """kb_search 必须把租户条件注入到 Retriever 的 filter。"""

    def test_tenant_is_injected(self, tools):
        tools.kb_search("问题", tenant_id="tenant-b")
        f = tools._recording.calls[0]["filter"]
        assert f is not None, "kb_search 没有注入任何 filter —— 跨租户泄露"
        assert f.get(TF) == "tenant-b"

    def test_no_tenant_means_no_filter(self, tools):
        """无租户（鉴权关闭的本地/评测场景）时不注入 —— 与既有软关闭语义一致。"""
        tools.kb_search("问题", tenant_id=None)
        assert tools._recording.calls[0]["filter"] is None

    def test_caller_supplied_tenant_is_ignored(self, tools):
        """调用方传入的租户键必须被丢弃（防止模型/客户端伪造越权）。"""
        tools.kb_search("问题", tenant_id="tenant-b",
                        filter_dict={TF: "tenant-a", "format": "xlsx"})
        f = tools._recording.calls[0]["filter"]
        assert f[TF] == "tenant-b", "调用方传的租户键覆盖了 principal，存在越权"
        assert f["format"] == "xlsx", "普通业务过滤条件不应被丢弃"

    def test_empty_tenant_treated_as_no_tenant(self, tools):
        """空串按"无租户"处理（不注入过滤），而不是注入 `tenant_id=""`。

        ## 语义变更说明（安全考虑）

        本条原先断言"空串也注入 `{tenant_id: ''}`"，理由是"fail-closed 下
        空串匹配不到任何租户"。那个理由是**错的** —— 实测发现：

            只要某行的 tenant_id 也是空串（例如列默认值为 ''），
            `'' = ''` 就成立 ⇒ 那些行对被判为"空租户"的调用者**可见**。

        也就是说"用空串当身份"不是 fail-closed，而是**恰好打开了所有
        空归属数据**。因此现在统一把空串归到"无租户"（等价于鉴权软关闭），
        且未归属数据的列默认值也改成了不可匹配的哨兵
        （见 ddl.UNOWNED_TENANT）—— 两道一起保证空归属数据不会被误读。
        """
        tools.kb_search("问题", tenant_id="")
        assert tools._recording.calls[0]["filter"] is None

    def test_whitespace_tenant_treated_as_no_tenant(self, tools):
        """纯空白同样按无租户处理，避免 `" "` 这种意外身份。"""
        tools.kb_search("问题", tenant_id="   ")
        assert tools._recording.calls[0]["filter"] is None


class TestDispatchPropagatesTenant:
    """dispatch 必须把 tenant_id 传给 kb_search（漏传就是那个泄露根因）。"""

    def test_dispatch_passes_tenant(self, tools):
        tools.dispatch("kb_search", {"query": "问题"}, tenant_id="tenant-b")
        f = tools._recording.calls[0]["filter"]
        assert f is not None and f.get(TF) == "tenant-b", (
            "dispatch 没把 tenant_id 传给 kb_search —— 这正是跨租户泄露的根因"
        )

    def test_dispatch_argument_name_is_tenant_id(self):
        """签名断言：防止有人把参数改名/删掉导致静默失去隔离。"""
        import inspect

        sig = inspect.signature(AgentTools.dispatch)
        assert "tenant_id" in sig.parameters

    def test_kb_search_signature_accepts_tenant(self):
        import inspect

        sig = inspect.signature(AgentTools.kb_search)
        assert "tenant_id" in sig.parameters, (
            "kb_search 又不接受 tenant_id 了 —— 隔离会静默失效"
        )


class TestNoPathBypassesTenantFilter:
    """结构性断言：不允许再出现"另一条检索路径没接租户"的情况。

    这条测试的价值在于**防止同类缺陷复发**：
    本次泄露的本质是"两条路径机制不对称"，而不是某一行写错。
    """

    def test_only_two_retrieval_entrypoints(self):
        """穷举代码库里所有 `*.search(` 调用点，必须都在受控路径内。"""
        import re
        from pathlib import Path

        app_dir = Path(__file__).resolve().parent.parent / "app"
        allowed = {
            # Agent 工具层：本文件已测其强制注入
            "agent/tools.py",
            # 唯一的检索出口（/query、Agent、评测后端共用）：由调用方先算好 filter
            "rag/executor.py",
        }
        offenders = []
        # 变量名两种都算：`retriever.search(...)` 与 `r.search(...)`
        pattern = re.compile(r"(?<![\w.])(?:retriever|r)\.search\s*\(")
        for path in app_dir.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if pattern.search(text):
                rel = path.relative_to(app_dir).as_posix()
                if rel not in allowed:
                    offenders.append(rel)
        assert not offenders, (
            f"发现未受控的检索调用点 {offenders} —— "
            f"新路径必须显式处理租户隔离，否则会重演跨租户泄露"
        )

    def test_query_route_receives_enforced_filter(self):
        """/query 路径必须经 enforced_filter 注入（回归保护）。"""
        from app.api.deps import enforced_filter
        from app.core.security import Principal

        p = Principal(tenant_id="tenant-c", authenticated=True)
        f = enforced_filter({TF: "tenant-a"}, p)
        assert f[TF] == "tenant-c", "enforced_filter 未以 principal 为准"


class TestSingleAclImplementation:
    """两条通道必须共用**同一个**租户过滤实现，不允许各写一份。

    这是"两通道不对称"的根治点：Agent 那条原先自己拼 filter 且漏了租户
    ⇒ 跨租户泄露。只修当前这一处没有意义，**必须消除"第二份实现"**，
    否则下次加通道还会漂移。
    """

    def test_both_channels_delegate_to_apply_tenant_acl(self):
        """/query 与 Agent 都必须走 security.apply_tenant_acl。"""
        import inspect

        from app.agent.tools import AgentTools
        from app.api import deps

        for fn in (deps.enforced_filter, AgentTools.kb_search):
            src = inspect.getsource(fn)
            assert "apply_tenant_acl" in src, (
                f"{fn.__qualname__} 没有走统一入口 apply_tenant_acl —— "
                f"出现了第二份租户过滤实现，会再次漂移"
            )

    def test_apply_tenant_acl_is_the_only_place_writing_tenant_key(self):
        """**检索路径**里，除 security.apply_tenant_acl 外不应有代码写入租户键。

        扫描范围只限"构造检索过滤条件"的模块：
          - `api/`（HTTP 层：经典 RAG 路由与依赖）
          - `agent/`（Agent 工具层）
          - `rag/`（检索管线）

        ⚠️ 局限（刻意写明，避免这个测试被误当成万能的）：
        这是**基于文本的**静态检查，只看"是否出现 `[tenant_field] = ...`"。
        其它模块里合法的同名写法会被误判 —— 实测就撞到一次：
        `worker/tasks/ingest.py` 里的 `analytics_sync["tenant_id"] = upload_tenant`
        是**给任务返回值记一个字段**，与检索过滤无关。
        所以这里限定范围，而不是做全仓扫描 + 白名单（那会越滚越长）。
        """
        import re
        from pathlib import Path

        app_dir = Path(__file__).resolve().parent.parent / "app"
        scan_roots = ["api", "agent", "rag"]
        allowed = {"core/security.py"}
        pattern = re.compile(
            r"\[(settings\.tenant_field|\"tenant_id\"|'tenant_id')\]\s*="
        )

        scanned, offenders = [], []
        for root in scan_roots:
            for path in (app_dir / root).rglob("*.py"):
                rel = path.relative_to(app_dir).as_posix()
                if rel in allowed:
                    continue
                scanned.append(rel)
                for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if pattern.search(stripped):
                        offenders.append(f"{rel}:{i}: {stripped}")

        assert scanned, "扫描范围为空 —— 测试本身失效了"
        assert not offenders, (
            "检索路径里出现了 security.apply_tenant_acl 之外的租户键写入"
            "（应改为调用统一入口，否则会再次出现两通道漂移）：\n  "
            + "\n  ".join(offenders)
        )

    def test_apply_tenant_acl_cannot_be_overridden(self):
        """客户端/模型传的租户键必须被丢弃（防伪造越权）。"""
        from app.core.security import apply_tenant_acl

        out = apply_tenant_acl({TF: "attacker"}, "real-tenant")
        assert out[TF] == "real-tenant"
        assert out.get("format") is None

    def test_apply_tenant_acl_keeps_non_tenant_filters(self):
        from app.core.security import apply_tenant_acl

        out = apply_tenant_acl({"format": "xlsx"}, "real-tenant")
        assert out == {TF: "real-tenant", "format": "xlsx"}

    def test_apply_tenant_acl_none_tenant_returns_business_filter(self):
        from app.core.security import apply_tenant_acl

        assert apply_tenant_acl({"format": "xlsx"}, None) == {"format": "xlsx"}
        assert apply_tenant_acl(None, None) is None
