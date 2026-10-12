"""
security.py - API 鉴权、多租户隔离与角色（RBAC）

设计（"API Key 每租户一密钥 + 单 collection + tenant_id 字段"）:
- 请求头带 X-API-Key（或 Authorization: Bearer <key>）来标识调用方。
- 服务端把 Key 映射到一个租户（tenant_id），后续检索按该租户强制过滤。
- 软关闭：未配置任何租户 Key（settings.tenant_keys 为空）时，鉴权不启用，
  返回 tenant_id=None 的匿名主体，检索不做租户过滤——行为与加鉴权前完全一致，
  不打断已有数据与本地调试。

RBAC（P7 新增，**向后兼容**）:
- 接入 Text2SQL 之前，"能拿到什么信息"取决于文档里写了什么；
  接入之后，"能查到什么"变成**可枚举的数据权限问题**（例如"全公司薪资最高的人"）。
  这把租户隔离从技术问题升级为**合规问题**，因此显式建模角色。
- 角色语义：
    analyst  —— 数据分析角色，可见全部列（**默认角色，等价于改造前行为**）
    employee —— 普通员工，敏感列（settings.sql_redact_columns）自动脱敏
- 兼容性保证：未配置 RAG_TENANT_ROLES 时所有人都拿 default_role(=analyst)，
  且敏感列脱敏只对非 analyst 生效 ⇒ **不配置就等于没有这个功能**，
  既有部署与测试零影响。
- 本文件是唯一的身份解析入口：升级 JWT/OIDC 只需替换 resolve_principal。
"""

from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional, Tuple

from app.core.config import settings
from app.core.exceptions import ForbiddenError, UnauthorizedError

# 角色常量
ROLE_ANALYST = "analyst"
ROLE_EMPLOYEE = "employee"

# 允许的角色集合（配置写错时应报错而不是静默降级）
KNOWN_ROLES = frozenset({ROLE_ANALYST, ROLE_EMPLOYEE})

# 不做敏感列脱敏的角色
PRIVILEGED_ROLES = frozenset({ROLE_ANALYST})


def apply_tenant_acl(
    client_filter: Optional[dict],
    tenant_id: Optional[str],
) -> Optional[dict]:
    """**唯一的租户过滤器构造入口**：把主体租户强制叠加到检索过滤条件上。

    ## 为什么要抽成一个函数（由真实跨租户泄露驱动）

    这个逻辑原先存在于**两个地方**，各写各的：
      1. `api/deps.enforced_filter()` —— 经典 RAG 路径用
      2. `agent/tools.kb_search()` 内联构造 —— Agent 路径用

    结果 Agent 那条**漏了租户**（`dispatch` 没把 tenant_id 传下去），
    于是 Agent 检索完全不做过滤，**能看到所有租户的私有向量**。
    实测：租户 c 通过 `/agent/stream` 拿到了租户 b 私有上传的数据，
    而同样问题走 `/chat/stream` 却被正确挡住（hidden_count=4）。

    这类缺陷的根因不是"某一行写错"，而是**同一策略有两份实现** ——
    只要有两份，就一定会漂移。所以收敛到这一个函数，
    并用 `tests/test_agent_tenant_isolation.py` 的结构性断言
    保证不再出现"自己拼 filter"的第三条路径。

    ## 语义

    - 客户端/模型传的租户键**一律丢弃**（不可覆盖，防伪造越权）
    - 非租户的业务过滤条件（如 format）保留
    - `tenant_id` 为 None（鉴权软关闭）时不注入 —— 与既有行为一致
    - 空串视为"无租户"，同样不注入（避免 `'' = ''` 式意外匹配）

    Args:
        client_filter: 调用方给的过滤条件（可为 None）
        tenant_id: 已认证主体的租户；None/空串表示不做租户过滤

    Returns:
        合并后的过滤条件；无任何条件时返回 None（Retriever 视为不过滤）
    """
    merged: dict = dict(client_filter or {})
    # 先无条件丢弃客户端传的租户键 —— 顺序很重要，必须在写入之前
    merged.pop(settings.tenant_field, None)
    tid = (tenant_id or "").strip()
    if tid:
        merged[settings.tenant_field] = tid
    return merged or None


@dataclass
class Principal:
    """已解析的调用方身份。

    字段扩展刻意设默认值，保证既有构造方式（Principal(tenant_id, authenticated)）
    与既有测试**完全不受影响**。
    """

    tenant_id: Optional[str]               # 租户标识；None 表示匿名（鉴权关闭时）
    authenticated: bool                    # 是否已通过 API Key 认证
    role: str = ROLE_ANALYST               # RBAC 角色；默认 analyst（等价改造前行为）
    scopes: Tuple[str, ...] = field(default_factory=tuple)

    # ---- 能力查询（把"角色有什么权限"收在这里，避免散落各处的字符串比较）----

    @property
    def can_see_sensitive(self) -> bool:
        """是否可见敏感列（薪资/邮箱等）。

        analyst 可见；其他角色一律脱敏。
        注意：匿名主体（鉴权关闭）视为 analyst —— 与"软关闭不打断本地调试"一致。
        """
        return self.role in PRIVILEGED_ROLES

    @property
    def can_run_sql(self) -> bool:
        """是否允许执行 SQL 查询。

        所有已知角色都可以（employee 只是脱敏，不是禁止）；
        未识别的角色保守拒绝。
        """
        return self.role in KNOWN_ROLES

    def redact_columns(self) -> FrozenSet[str]:
        """该身份需要脱敏的列集合（"表.列" 小写）。

        特权角色返回空集 —— 上层可据此走零成本快路径。
        """
        if self.can_see_sensitive:
            return frozenset()
        return frozenset(settings.redact_column_set)


def resolve_principal(x_api_key: Optional[str], authorization: Optional[str]) -> Principal:
    """
    解析并校验调用方身份（与框架无关，便于单测直接调用）。
    - 鉴权关闭：直接放行，返回匿名主体（tenant_id=None，role=default_role）。
    - 鉴权开启：缺失 Key -> 401；Key 无效 -> 403；有效 -> 返回对应租户主体 + 角色。

    角色来源：RAG_TENANT_ROLES 里该租户的配置；未配置则用 settings.default_role。
    """
    settings.validate_security()
    if not settings.auth_enabled:
        return Principal(
            tenant_id=None, authenticated=False, role=settings.default_role,
        )

    # 优先取 X-API-Key，其次 Bearer token
    key = x_api_key
    if not key and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            key = token.strip()

    if not key:
        raise UnauthorizedError("缺少 API Key（X-API-Key 或 Authorization: Bearer）")

    tenant_id = settings.tenant_keys.get(key)
    if tenant_id is None:
        raise ForbiddenError("API Key 无效或无权限")
    if tenant_id in {"@anonymous@", "@unowned@"}:
        raise ForbiddenError("保留租户标识不可用作身份")

    role = settings.tenant_roles.get(tenant_id, settings.default_role)
    if role not in KNOWN_ROLES:
        # 配置写错时**显式失败**，不要静默降级为特权角色 ——
        # 静默降级会让一个笔误变成权限漏洞。
        raise ForbiddenError(
            f"租户 {tenant_id!r} 配置了未知角色 {role!r}（可选：{sorted(KNOWN_ROLES)}）"
        )

    return Principal(tenant_id=tenant_id, authenticated=True, role=role)
