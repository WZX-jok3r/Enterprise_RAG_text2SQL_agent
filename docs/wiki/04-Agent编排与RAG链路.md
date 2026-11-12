# 04 · Agent 编排与 RAG 链路

## 1. 三层混合路由

三层不是并列，而是**串联关卡**：

| 关卡 | 模块 | 成本 | 说明 |
| --- | --- | --- | --- |
| L1 规则前置路由 | [router.py](file:///d:/PythonProjects/personal_rag/backend/app/agent/router.py) | 0 | 命中即强路由，跳过 LLM |
| L2 LLM 工具选择 | [loop.py](file:///d:/PythonProjects/personal_rag/backend/app/agent/loop.py) | 有界 | function calling 自主选工具 |
| L3 兜底降级 | loop.py | 0 | 异常/超步数/超时强制退回纯 RAG |

### 规则路由判定（纯函数，可穷举单测）

`route_by_rules(question)` 四步：

1. 文档意图词命中 **且** 聚合词未同时命中 → `RAG`
2. 聚合词 **且** 列名词同时命中 → `SQL`（强路由）
3. 仅命中列名词 → `LLM_DECIDE`（可能是查明细）
4. 无信号 → `LLM_DECIDE`

关键原则：**必须同时命中「聚合词」与「列名词」才强路由 SQL**。反例“布洛芬最多多久吃一次”命中“最多”但非统计题——`DOC_WORDS` 的“多久”优先，避免误路由。

## 2. Agent 状态机 — [loop.py](file:///d:/PythonProjects/personal_rag/backend/app/agent/loop.py)

`AgentLoop.run(question, tenant_id, history, forced_route, redact, actor)` 是主入口，返回事件迭代器（可直接转 SSE）。

### 五重防死循环

| 机制 | 默认 | 目的 |
| --- | --- | --- |
| 步数硬上界 | `MAX_STEPS=5` | 状态机而非 while True |
| 墙钟超时 | `WALL_CLOCK_SECONDS=60` | 防单步慢 |
| LLM 调用预算 | `MAX_LLM_CALLS=8` | 成本上界 |
| 重复调用检测 | `seen_calls`（工具+参数去重） | 识别循环 |
| 任一步失败 | 降级到纯 RAG | 可用性不倒退 |

每个请求独立 `AgentState`，避免共享状态串扰。

### 三条执行路径

- `_run_forced_sql`：直接 `list_data_tables` + 走 Text2SQL 引擎，省一次 LLM 决策。
- `_run_forced_rag`：直接 `kb_search`。
- `_run_llm_loop`：LLM 自主循环；`kb_search` 执行后必补发 meta 帧，保证前端来源列表/徽标正常。

### 多轮追问上下文

`build_tool_context()` 把本轮「已执行 SQL + 结果行（≤5 行）」渲染成紧凑文本，随 assistant 消息的 `tool_context` 字段落库，下一轮附在历史里供模型延续。只记 SQL 与结果，不记 prompt/工具原始报文（体积与敏感）。

## 3. 工具 — [tools.py](file:///d:/PythonProjects/personal_rag/backend/app/agent/tools.py)

三个工具（不贪多），`description` 里刻意写清“何时不该用”：

| 工具 | 能力 | 复用 |
| --- | --- | --- |
| `kb_search` | 知识库检索 | `Retriever.search`，返回 `(chunks, dropped)` |
| `sql_query` | 只读 SQL | guard + executor |
| `list_data_tables` | 列出可查数据表 | schema 元数据 |

`sql_answer` 是内部方法（非面向模型工具），走 Text2SQL 引擎处理自然语言统计问题，session 生命周期收在工具层，让 Loop 可被注入假引擎的 hermetic 测试覆盖。

## 4. SSE 事件契约 — [events.py](file:///d:/PythonProjects/personal_rag/backend/app/agent/events.py)

**扩帧不改帧**。既有 4 类字段冻结：

- `meta`（sources/retrieved_count/hidden_count）
- `delta`（text）
- `done`（answer，可带 agent_stats/_tool_context）
- `error`（message）

新增 6 类（前端未知 type 走 default 忽略，旧前端不崩）：

- `route` / `tool_call` / `tool_result` / `sql`（含 truncated）/ `clarify` / `degraded`

## 5. RAG 管道 — [pipeline.py](file:///d:/PythonProjects/personal_rag/backend/app/rag/pipeline.py)

`RAGPipeline` 三条对外链路：

| 方法 | 行为 |
| --- | --- |
| `query(generate=True/False)` | 检索→(可选)生成→sources；`generate=False` 仅检索（评测快速模式） |
| `query_with_history()` | 多轮，无召回时用“无相关参考资料”兜底 context |
| `stream_chat()` | 流式 yield `meta→delta*→done/error` |

结果字典键（query/answer/sources/retrieved_count/hidden_count）与流式事件类型保持稳定，前端与评测无感。

## 6. 混合检索 — [qdrant.py](file:///d:/PythonProjects/personal_rag/backend/app/vector/qdrant.py)

`VectorStore.search()` 流程：

1. 构造版本过滤（`document_version_id` 为当前发布 id 或未版本化）。
2. `retrieval_mode=hybrid`：dense + BM25 各 `prefetch_k` 候选，RRF 融合；否则纯 dense。
3. `rerank_client` 存在且候选 >1：交叉编码器重排取 top_k，失败降级原始排序。
4. 低相关过滤仅对 `rerank` 绝对分生效（`_filter_low_relevance`），非 rerank 量纲原样返回。

**量纲安全**：RRF 融合分量级仅 ~0.016，若与 rerank 分数混用会误删全部结果；所以只在 `score_kind=="rerank"` 时施加绝对阈值。

**中文 BM25 预分词**：`tokenize_for_bm25()` 先归一破折号、再保护英数串（型号/SKU）整体 token，其余用 jieba 切分；入库与查询对称调用。

**集合结构**：named vectors（`dense` + `bm25` 稀疏）；为 `tenant_id`、`document_version_id` 建 keyword payload 索引。

### 版本可见性 — [version_visibility.py](file:///d:/PythonProjects/personal_rag/backend/app/vector/version_visibility.py)

`current_revision_ids()` 从 PG `documents` 表读 `is_current=True` 且 `status='ready'` 且 `document_key` 非空的版本 id；登记服务不可用时抛异常 **fail-closed**（停止检索，避免放出不确定版本）。

## 7. 上下文与来源

- [context_builder.py](file:///d:/PythonProjects/personal_rag/backend/app/rag/context_builder.py)：`build_context()` 拼 `【参考N】来源: 文件名(格式)[第X页/行/Sheet]`。
- [source_formatter.py](file:///d:/PythonProjects/personal_rag/backend/app/rag/source_formatter.py)：`format_sources()` 组装带版本号的来源列表。