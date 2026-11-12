# backend 目录全量梳理（项目框架介绍）

> 逐文件说明 `backend/` 下每个文件/目录的用途，按模块组织。
> 整体分层：**api（路由）→ agent/rag/analytics（能力）→ services/repositories/models（业务与数据）
> → vector/llm/ingestion/worker（基础设施与入库）→ core（横切）→ eval/migrations/tests（配套）**。
>
> 核对说明（2026-11-11 复核）：后端唯一实现位于 `backend/app/`，仓库根目录不存在旧 `app/` 或后端 `src/`；`frontend/src/` 是当前前端源码。本文目录树已与当前工作区逐项对齐（含新增的 `app/prompts/` 提示词包与 49 个测试文件）；测试数量会随代码演进变化，执行前仍以工作区为准。

```text
backend/
├── pyproject.toml              # 包定义 + 依赖清单 + pytest 配置（pip install -e . 的来源）
├── uv.lock                     # uv 锁定的精确依赖版本
├── alembic.ini                 # Alembic 数据库迁移配置
├── conftest.py                 # pytest 引导：把 backend/ 加入 sys.path，使 `import app.*` 全局可用
├── Dockerfile                  # 后端运行镜像构建（API/worker 共用同一镜像）
├── .dockerignore               # 镜像构建时忽略的文件
├── .env.example                # 环境变量模板（复制为 .env 填值）
├── _diag_recall.py             # 诊断脚本：定位检索失败题卡在哪一关（非业务代码）
├── enterprise_rag_backend.egg-info/   # 可编辑安装生成的构建元数据（按约定保留，非垃圾）
│
├── app/                        # ★ 应用主体（FastAPI 后端）
│   ├── main.py                 # 应用工厂 + ASGI 入口：中间件栈、lifespan 里连 DB/Redis/ARQ
│   ├── database.py             # 异步数据库层（SQLAlchemy 2.0 async + asyncpg）
│   │
│   ├── api/                    # 【HTTP 路由层】只做路由与依赖注入，不含业务逻辑
│   │   ├── router.py           #   根路由聚合，挂载 /api/v1 下各子路由
│   │   ├── deps.py             #   FastAPI 公共依赖：解析 Principal、会话服务、强制租户过滤
│   │   └── v1/                 #   v1 版接口
│   │       ├── agent.py        #     Agent 对话（SSE 流式，含执行轨迹）—— 主链路入口
│   │       ├── chat.py         #     多轮对话 /chat 与 /chat/stream（前端/评测稳定契约）
│   │       ├── query.py        #     单轮问答 /query
│   │       ├── documents.py    #     文档入库路由（上传/状态/版本）
│   │       ├── sessions.py     #     会话新建 / 删除
│   │       ├── usage.py        #     用量与成本看板
│   │       └── health.py       #     健康检查（存活探针，回显模型/检索配置，不探外部依赖）
│   │
│   ├── core/                   # 【横切层】配置/安全/日志/指标等全局能力
│   │   ├── config.py           #   统一配置中心（pydantic-settings，字段对应 .env）
│   │   ├── security.py         #   API 鉴权 + 多租户隔离 + RBAC（apply_tenant_acl 唯一过滤入口）
│   │   ├── exceptions.py       #   统一异常类型与全局异常处理器
│   │   ├── logging.py          #   日志配置（结构化 + trace_id 贯穿）
│   │   ├── metrics.py          #   轻量指标注册表 + Prometheus 文本导出
│   │   ├── performance.py      #   请求级 provider 用量采集（opt-in，不记 prompt/答案/密钥）
│   │   ├── retry.py            #   外部 API 瞬时故障重试（指数退避 + 抖动）
│   │   └── trace.py            #   trace_id 贯穿：HTTP→Agent→SQL→worker
│   │
│   ├── agent/                  # 【Agent 编排·大脑】手写有界状态机
│   │   ├── loop.py             #   Agent Loop：五重防死循环 + 三条执行路径 + 收尾补 meta
│   │   ├── router.py           #   三层路由第一层：规则前置路由（纯函数，可穷举单测）
│   │   ├── tools.py            #   工具定义与执行：kb_search / sql_query / list_data_tables
│   │   └── events.py           #   SSE 事件契约（扩帧不改帧，向后兼容）
│   │
│   ├── rag/                    # 【RAG 检索问答领域】
│   │   ├── pipeline.py         #   完整链路编排：query / query_with_history / stream_chat
│   │   ├── retriever.py        #   检索读路径（VectorStore.search 的薄封装）
│   │   ├── context_builder.py  #   把检索结果拼装成 LLM 上下文文本
│   │   └── source_formatter.py #   把 chunk 组装成对外 sources 列表（来源/预览）
│   │
│   ├── prompts/                # 【提示词层】按通道拆分，供 pipeline / agent / text2sql 各自引用
│   │   ├── rag.py              #   RAG 系统提示词（含 {context} 注入位）
│   │   ├── agent.py            #   Agent 系统提示词、回答模板与三个工具的 TOOLS schema
│   │   ├── text2sql.py         #   Text2SQL 提示词 + few-shot 路径/表头 + 重试消息模板
│   │   └── data/fewshot_examples.jsonl # few-shot 示例库
│   │
│   ├── analytics/              # 【Text2SQL 数据层与分析通道】（只读、带权限护栏）
│   │   ├── text2sql.py         #   Text2SQL 引擎：NL→SQL→执行→NL 答案（9 步链路）
│   │   ├── schema.py           #   Schema 语义层：把元数据渲染成"给 LLM 看的数据字典"
│   │   ├── schema_infer.py     #   从表格原始行推断列类型/表头（纯函数，可单测）
│   │   ├── fewshot.py          #   question→SQL 示例库（Text2SQL 性价比最高的提准手段）
│   │   ├── guard.py            #   SQL 安全网关第①层：AST 白名单校验 + LIMIT 强制注入
│   │   ├── executor.py         #   只读执行器第②层：事务与资源限制（超时/行数上限）
│   │   ├── redact.py           #   结果集脱敏补充层（第③层）
│   │   ├── permissions.py      #   受限 SQL 登录的数据库列权限（第④层，DDL 授权）
│   │   ├── audit.py            #   SQL 审计写入（同步落 rag 库）
│   │   ├── ddl.py              #   业务表建表（在 kb_analytics 库，幂等）
│   │   └── seed.py             #   从知识库 xlsx 装载业务表（ETL 引擎 + worker 入口）
│   │
│   ├── vector/                 # 【向量层】Qdrant + Embedding/Rerank + 版本可见性
│   │   ├── qdrant.py           #   Qdrant 连接/写入/检索 + 混合检索(dense+BM25→RRF→rerank→低分过滤)
│   │   ├── embedding.py        #   模型网关：SiliconFlow Embedding + Rerank
│   │   └── version_visibility.py #  版本可见性：PG 为权威，暂存/历史版本向量不自动发布
│   │
│   ├── llm/                    # 【LLM 网关】屏蔽具体 provider
│   │   ├── provider.py         #   chat/completions 封装（SiliconFlow/OpenAI 兼容）
│   │   └── tool_client.py      #   带 tool calling 的客户端 + 流式 tool_calls 累积器
│   │
│   ├── ingestion/              # 【入库领域】解析 → 分块 → 索引
│   │   ├── loader.py           #   多格式文档加载解析核心（v4）
│   │   ├── indexer.py          #   入口脚本：扫描知识库、增量入库
│   │   └── chunking/           #   分块（按内容类型分层）
│   │       ├── base.py         #     分块通用基元（与内容类型无关）
│   │       ├── text.py         #     文本分块（txt / md / docx）
│   │       └── table.py        #     表格分块（Markdown 表格感知 + 行级语义化 + PDF 表格修复）
│   │
│   ├── models/                 # 【ORM 模型】元数据/业务数据的事实来源
│   │   ├── base.py             #   ORM 基类与通用 mixin
│   │   ├── tenant.py           #   租户模型
│   │   ├── document.py         #   文档登记表（替代旧的 processed_cache/cache.json）
│   │   ├── chat.py             #   会话与消息模型（多轮对话持久化）
│   │   ├── ingest_task.py      #   入库任务状态模型（worker 写）
│   │   ├── analytics.py        #   Analytics 元数据：哪些业务表可被 LLM 看到 + 列语义
│   │   ├── llm_usage.py        #   LLM 用量记账（token/耗时落库）
│   │   └── sql_audit.py        #   SQL 审计模型（每次生成的 SQL 执行）
│   │
│   ├── repositories/           # 【数据访问层】
│   │   ├── base.py             #   仓储基类：持有请求级 AsyncSession，只 flush 不擅自 commit
│   │   ├── document_repo.py    #   文档登记仓储
│   │   ├── document_version_repo.py # 文档版本仓储（事务级 PG 锁串行化版本分配/发布）
│   │   ├── ingest_task_repo.py #   入库任务仓储（建/查/更新）
│   │   └── session_repo.py     #   会话与消息仓储（含多租户 ACL 判定）
│   │
│   ├── schemas/                # 【Pydantic v2 请求/响应模型】对齐前端 types.ts
│   │   ├── agent.py            #   Agent 相关请求/响应
│   │   ├── chat.py             #   问答/会话相关
│   │   └── documents.py        #   文档入库相关响应
│   │
│   ├── services/               # 【业务服务层】
│   │   ├── ingestion_service.py#   入库编排：收文件→写 PG 任务/文档→入队 ARQ→立即返回 task_id
│   │   └── session_service.py  #   会话服务：内存 SessionStore 迁移为 PG(事实)+Redis(热缓存)
│   │
│   ├── cache/                  # 【缓存层】
│   │   └── redis.py            #   异步 Redis 客户端与会话热缓存
│   │
│   ├── observability/          # 【可观测层】
│   │   ├── langfuse.py         #   Langfuse 埋点（trace/span，软依赖，未配密钥即空转）
│   │   └── usage.py            #   LLM 用量记账：写入 + 聚合查询（供 /usage 成本看板）
│   │
│   ├── ops/                    # 【平台运维命令】人工/CI 调用，运行链路不 import
│   │   ├── setup_db.py         #   建库 + 建只读/受限角色 + 列权限自检（幂等）
│   │   ├── seed.py             #   装载 xlsx 业务表并跑真值断言（CLI 入口）
│   │   ├── reconcile.py        #   PG 登记表与 Qdrant 向量库的对账（P7）
│   │   ├── backup.py           #   备份：PG 两库 dump + Qdrant 快照 + 旧备份清理
│   │   ├── recovery.py         #   本地隔离恢复演练（绝不覆盖源库/集合）
│   │   └── sql/001_init.sql    #   分析库初始化 SQL（只读角色/授权）
│   │
│   └── worker/                 # 【ARQ 异步入库 worker】
│       ├── settings.py         #   worker 设置：Redis 连接、任务白名单、启停钩子
│       ├── pool.py             #   ARQ 连接池（API 侧入队用）
│       └── tasks/
│           ├── ingest.py       #     异步入库任务：解析→切分→向量化→索引，维护任务状态
│           └── versioned_ingest.py #   版本化入库：先暂存不可变修订，再在 PG 事务中发布
│
├── migrations/                 # 【Alembic 数据库迁移】
│   ├── env.py                  #   迁移运行环境
│   ├── script.py.mako          #   迁移脚本模板
│   └── versions/               #   按序演进的 schema（时间线）
│       ├── 35fd0e1e9233_init_persistence.py        #  初始化持久化表
│       ├── 7c1a4b2e9d05_analytics_metadata.py      #  analytics 元数据表
│       ├── 5b2c8d4e6f13_llm_usage.py               #  LLM 用量表
│       ├── 3e9d1f7a5b28_sql_audit.py               #  SQL 审计表
│       ├── 8d4f2a6c1b07_message_tool_context.py    #  消息增 tool_context 列(多轮)
│       ├── 9f31d2c8a604_document_versions.py       #  不可变文档修订 + 单一发布版本
│       └── a271c48319b6_version_identity_not_null.py # 拒绝托管标识里的 NULL 版本号
│
├── eval/                       # 【离线评测包】（迁自 src/eval_runner.py，三套：检索/质量/性能）
│   ├── runner.py               #   检索评测主脚本
│   ├── __main__.py             #   评测入口（python -m eval）
│   ├── compare.py              #   检索结果回归比对（每步改造的验证闸口）
│   ├── text2sql_runner.py      #   Text2SQL 评测（Execution Accuracy）
│   ├── text2sql_dataset.jsonl  #   Text2SQL 评测集
│   ├── quality.py              #   答案质量评测（独立于检索 runner）
│   ├── quality_models.py       #   质量评测的严格版本化契约
│   ├── quality_judge.py        #   fail-closed 语义评审（带人审支持）
│   ├── quality_metrics.py      #   证据/格式诊断（不把词面近似当语义准确率）
│   ├── quality_compare.py      #   逐题质量回归（平均值不能掩盖换答失败）
│   ├── quality_calibration.py  #   评测器自检（分数不代表真实模型质量）
│   ├── performance.py          #   闭环 HTTP 压测（fixture 结果≠生产产能）
│   ├── performance_fixture.py  #   真实 TCP + 真实路由，合成模型/会话依赖
│   ├── performance_metrics.py  #   确定性延迟/成本计算（缺账单不算免费调用）
│   ├── baseline/               #   检索基线（v1 快照 + 说明）
│   └── data/                   #   答案质量集 + 性能用例/价格样例（小体积夹具）
│
└── tests/                      # 【后端测试】hermetic 单测 + 真连 PG/LLM 的集成测试
    ├── test_agent_api.py           # Agent 路由（TestClient+依赖覆盖）
    ├── test_agent_loop.py          # Agent Loop（假 LLM/假工具）
    ├── test_agent_router.py        # 规则前置路由单测
    ├── test_agent_events.py        # SSE 事件契约（扩帧不改帧）
    ├── test_agent_multiturn.py     # 多轮工具上下文（确定性断言）
    ├── test_agent_tenant_isolation.py # Agent 检索租户隔离
    ├── test_api_v1.py              # API v1 门禁
    ├── test_prompt_citation_contract.py # 引用编号格式三方契约（context_builder / 提示词 / 解析器）
    ├── test_pipeline.py            # RAG 链路编排（FakeRetriever/FakeLLM）
    ├── test_retriever.py           # 检索读路径薄封装
    ├── test_chunking.py            # 分块（文本+表格）
    ├── test_vector_relevance.py    # 低相关过滤量纲 + BM25 分词
    ├── test_security.py            # 鉴权（resolve_principal 各分支）
    ├── test_rbac.py                # RBAC 与敏感列脱敏
    ├── test_retry.py               # 重试 + 异常链解包
    ├── test_metrics_trace.py       # 指标注册表 + trace_id
    ├── test_metrics_coverage.py    # 每个注册指标都必须真有埋点
    ├── test_sql_guard.py           # SQL 网关攻击性测试
    ├── test_sql_executor_integration.py # SQL 执行器（真连 PG）
    ├── test_column_permissions_integration.py # 别名/表达式绕不过列权限（真连 PG）
    ├── test_table_namespace.py     # 表命名空间/归属保护（真连 PG）
    ├── test_tenant_isolation.py    # 写入侧 ETL + 读取侧 RLS（真连 PG）
    ├── test_request_security_boundaries.py # 请求隔离/鉴权边界回归
    ├── test_text2sql_ambiguity.py  # 口径歧义检测（纯规则）
    ├── test_text2sql_e2e.py        # Text2SQL 端到端（真连 LLM+PG）
    ├── test_text2sql_metric.py     # Text2SQL 评测比较逻辑
    ├── test_document_versions.py   # 文档版本化（真连 PG）
    ├── test_version_visibility.py  # 版本可见性
    ├── test_versioned_indexing.py  # 版本化索引
    ├── test_ingest_worker.py       # 异步入库门禁
    ├── test_ingest_task_tenant.py  # 入库任务租户归属（真连 PG）
    ├── test_worker_analytics_sync.py # worker 与 SQL 通道接线
    ├── test_session_persistence.py # 会话持久化集成
    ├── test_session_concurrency.py # 会话并发竞态（真连 PG）
    ├── test_sql_audit_stats.py     # SQL 审计统计（真连 PG）
    ├── test_usage.py               # LLM 用量记账（真连 PG）
    ├── test_ops_not_imported_by_app.py # 架构守卫：运行态不得 import app.ops
    ├── test_reconcile.py           # 对账分类判定
    ├── test_recovery.py            # 恢复演练
    ├── test_schema_infer.py        # 表格列类型推断
    ├── test_tool_accumulator.py    # 流式 tool_calls 累积器穷举
    ├── test_eval_compare.py        # 回归闸口"真会拦"验证
    ├── test_quality_dataset.py     # 质量数据集
    ├── test_quality_judge.py       # 质量评审
    ├── test_quality_metrics.py     # 质量指标
    ├── test_quality_compare.py     # 质量回归比对
    ├── test_quality_runner.py      # 质量 runner
    ├── test_performance_capture.py # 性能采集
    ├── test_performance_metrics.py # 性能指标计算
    └── test_performance_runner.py  # 性能 runner
```

## 按模块看职责（速查）

| 模块 | 一句话职责 |
|---|---|
| `api/` | HTTP 路由与依赖注入，无业务逻辑 |
| `core/` | 配置、鉴权/多租户、日志、指标、重试、trace 等横切能力 |
| `agent/` | 大脑：规则路由 + 有界 Agent Loop + 工具 + SSE 事件 |
| `rag/` | 检索问答链路编排（检索→上下文→生成→来源） |
| `analytics/` | Text2SQL 引擎与四层 SQL 安全防御、ETL（运行态） |
| `ops/` | 运维命令：建库建角色、灌数据、对账、备份、恢复演练 |
| `vector/` | Qdrant 混合读写 + Embedding/Rerank + 版本可见性 |
| `llm/` | LLM 网关与 tool calling 客户端 |
| `ingestion/` | 文档解析→分块→索引 |
| `models/` + `repositories/` | SQLAlchemy 模型 + 数据访问层 |
| `schemas/` | Pydantic 请求/响应契约（对齐前端） |
| `services/` | 会话与入库业务编排 |
| `cache/` | Redis 客户端与会话热缓存 |
| `observability/` | Langfuse 埋点、LLM 用量记账（`usage.py`） |
| `worker/` | ARQ 异步入库任务 |
| `migrations/` | Alembic schema 演进 |
| `eval/` | 检索/答案质量/性能三套离线评测 |
| `tests/` | 单测 + 集成测试（活文档） |

## 快速阅读建议
- **想懂一次问答**：`api/v1/agent.py → agent/loop.py → agent/tools.py → rag/*` 或 `analytics/text2sql.py`。
- **想懂数据权限**：`core/security.py → analytics/{guard,executor,redact,permissions}.py → models/{tenant,sql_audit}.py`。
- **想懂入库**：`api/v1/documents.py → services/ingestion_service.py → worker/tasks/versioned_ingest.py → ingestion/* → vector/qdrant.py`。
- **想懂"改了会不会退化"**：`tests/` 是活文档，尤其 `test_agent_tenant_isolation.py`、
  `test_column_permissions_integration.py`、`test_document_versions.py`。
