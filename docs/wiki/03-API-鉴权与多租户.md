# 03 · API、鉴权与多租户安全

## 1. 路由挂载结构

`api/router.py` 定义 `api_router = APIRouter(prefix="/v1")`，`main.py` 再套 `/api`，最终对外路径 `/api/v1/*`。

```python
api_router.include_router(health.router)
api_router.include_router(query.router)
api_router.include_router(chat.router)
api_router.include_router(sessions.router)
api_router.include_router(documents.router)
api_router.include_router(agent.router)
api_router.include_router(usage.router)
```

## 2. API 一览

| 方法 | 路径 | 用途 | 代码 |
| --- | --- | --- | --- |
| GET | `/health` | 存活与部分配置检查 | [health.py](file:///d:/PythonProjects/personal_rag/backend/app/api/v1/health.py) |
| POST | `/query` | 单次 RAG 问答 | [query.py](file:///d:/PythonProjects/personal_rag/backend/app/api/v1/query.py) |
| POST | `/chat` | 多轮问答（后端 session） | [chat.py](file:///d:/PythonProjects/personal_rag/backend/app/api/v1/chat.py) |
| POST | `/chat/stream` | 多轮问答（SSE） | [chat.py](file:///d:/PythonProjects/personal_rag/backend/app/api/v1/chat.py) |
| POST | `/agent/stream` | Agent 问答 + 执行事件（SSE） | [agent.py](file:///d:/PythonProjects/personal_rag/backend/app/api/v1/agent.py) |
| POST | `/agent/route` | 规则路由调试（纯函数） | [agent.py](file:///d:/PythonProjects/personal_rag/backend/app/api/v1/agent.py) |
| POST/GET/DELETE | `/sessions`、`/sessions/{id}` | 创建/删除会话 | [sessions.py](file:///d:/PythonProjects/personal_rag/backend/app/api/v1/sessions.py) |
| POST/GET | `/documents` | 上传/列出文档 | [documents.py](file:///d:/PythonProjects/personal_rag/backend/app/api/v1/documents.py) |
| GET | `/documents/{task_id}/status` | 入库任务状态 | [documents.py](file:///d:/PythonProjects/personal_rag/backend/app/api/v1/documents.py) |
| GET | `/documents/{id}/versions` | 版本历史 | [documents.py](file:///d:/PythonProjects/personal_rag/backend/app/api/v1/documents.py) |
| POST | `/documents/{id}/activate` | 激活指定就绪版本（回滚） | [documents.py](file:///d:/PythonProjects/personal_rag/backend/app/api/v1/documents.py) |
| DELETE | `/documents/{id}` | 删除文档/版本 | [documents.py](file:///d:/PythonProjects/personal_rag/backend/app/api/v1/documents.py) |
| GET | `/usage`、`/usage/sql` | 模型用量与 SQL 统计 | [usage.py](file:///d:/PythonProjects/personal_rag/backend/app/api/v1/usage.py) |
| GET | `/metrics` | Prometheus 指标（非 `/api/v1`，不鉴权） | [main.py](file:///d:/PythonProjects/personal_rag/backend/app/main.py) |

鉴权请求使用 `X-API-Key` 或 `Authorization: Bearer <key>`。

## 3. 依赖注入 — [deps.py](file:///d:/PythonProjects/personal_rag/backend/app/api/deps.py)

| 依赖 | 产出 |
| --- | --- |
| `get_principal` | 从请求头解析 `Principal` |
| `get_db` | 每请求 AsyncSession |
| `get_session_service` | 进程级 `SessionService` |
| `get_ingestion_service` | `IngestionService`（带 ARQ 池） |
| `get_pipeline` | 进程级 `RAGPipeline` 单例 |
| `resolve_top_k` | 回退配置默认 top_k |
| `enforced_filter` | 薄封装 `security.apply_tenant_acl`（不再重复实现） |

## 4. 身份解析与 RBAC — [security.py](file:///d:/PythonProjects/personal_rag/backend/app/core/security.py)

**Api Key 模型**：`RAG_TENANT_KEYS = "tenantA=keyA;tenantB=keyB"`，每租户一密钥。`resolve_principal`：

- 鉴权关闭（无租户 Key）→ 匿名主体 `tenant_id=None`，检索不过滤（软关闭，与改造前一致）。
- Key 缺失 → 401；Key 无效 → 403；租户是保留标识 → 403；角色非法 → 显式 403（防静默降级）。

**角色语义**：

| 角色 | 敏感列 | SQL 能力 |
| --- | --- | --- |
| `analyst`（默认） | 可见全部 | 可查询 |
| `employee` | 自动脱敏（`sql_redact_columns`） | 可查询（脱敏而非禁止） |

`Principal` 能力查询把“角色有什么权限”收敛到类内，避免字符串比较散落各处：`can_see_sensitive`、`can_run_sql`、`redact_columns()`（特权角色返回空集 → 零成本快路径）。

## 5. 检索侧租户隔离 — `apply_tenant_acl`

**单一入口**（由一次真实跨租户泄露驱动收敛）：

- 客户端/模型传的租户键**一律丢弃**；
- 非租户业务过滤（如 format）保留；
- `tenant_id` 为 None/空串时不注入。

经典 RAG（`enforced_filter`）与 Agent（`tools.kb_search`）都调用它，保证不会再次出现“某条路径漏传租户”的权限漂移。

## 6. SQL 侧租户隔离

RLS 策略（fail-closed）：

```sql
USING (
    is_shared IS TRUE
    OR tenant_id = current_setting('app.tenant_id', true)
)
```

- 未设置租户身份时不能读租户私有行，共享行仍可见。
- 执行器统一规范化身份，拒绝把保留哨兵值（`@anonymous@`/`@unowned@`/空串）当真实租户。

**配置前提**：development 允许关鉴权（此时检索不注入租户过滤）；staging/production `validate_security()` 强制开鉴权、默认 employee 角色、配置 SQL 账号口令。

## 7. 会话与文档归属

- 会话：`SessionService` 所有读写带 `tenant_id` ACL，非本租户 session 视为不存在（返回 `[]`/404）。
- 文档：上传/状态/列表/版本/激活/删除均带租户 ACL，跨租户返回 404（不与“不存在”区分，防枚举）。
- 文档与任务删除顺序：先清外部存储（向量/文件）成功后再删登记行，失败保留登记行可重试。

## 8. 前端 Key 管理

前端**不把共享 API Key 编译进静态产物**，仅在运行时通过 `localStorage` 写入 `X-API-Key`。Key 变更时用 `rag_session_tenant` 指纹丢弃旧会话，防跨租户串会话（见 [08-前端架构](08-前端架构.md)）。