# RAG / AI Agent 面试题库（基于本项目，初/中/高各 20 题）

> 用法：每题解答都锚定本项目真实实现（文件、参数、决策），可直接作为面试口述底稿。
> 追问意识：每题末尾如与项目坑/亮点清单（见《项目全景深度解析》）联动，务必带出"亲历证据"。
> 状态核对（2026-10-20）：本文早期版本中的若干结论已过时，已按当前代码修订；精确参数与评测结果仍应以 `backend/app/`、配置和带日期的验证报告为准。不要把历史题库答案当成运行态验收。

---

## 初级（概念与项目认知，20 题）

**1. 什么是 RAG？为什么不直接微调大模型？**
RAG = 检索增强生成：先从知识库召回相关片段，拼进 Prompt 让 LLM 基于资料回答。知识更新通常不需要重训模型；API 上传路径使用文档版本链，新版本写入完成后再发布，旧版仍可在处理期间服务检索。系统提供来源信息，但来源存在不等于答案已被来源充分支持；生成仍可能出错。

**2. 用一分钟介绍你的项目架构。**
前后端分离：Vue 3 + Pinia 由 nginx(:8080) 提供，FastAPI(:8000) 处理 API；PostgreSQL 保存应用数据，Redis 承担缓存与 ARQ 队列，Qdrant 保存向量。API 上传由 ARQ worker 异步解析、分块和索引。模型名、向量集合名和服务参数以 `.env.example` 与实际部署配置为准；Compose 文件包含 7 个服务定义，其中 migrate 为一次性任务。

**3. Embedding 是什么？1024 维意味着什么？**
把文本映射为稠密向量，语义相近则 cosine 距离近。本项目用 BAAI/bge-m3，输出 1024 维（config `vector_dim=1024`，与 Qdrant dense 向量配置一致，改模型维度必须重建 collection）。维度本身决定表达容量与存储/计算成本；`EmbeddingClient.embed` 做了批量与维度校验，防止模型漂移悄悄污染索引。

**4. 分块（chunking）为什么必要？512/50 怎么理解？**
embedding 模型有输入长度上限，且"一段话一个向量"过粗会稀释语义；检索粒度应在"一个可独立回答的语义单元"。本项目 `chunk_size=512` 字符、`overlap=50` 滑窗重叠，防止句子被切断后两侧都检索不到。不是所有内容由同一策略处理：md 按标题层级、docx 段落/表格分路由、xlsx/PDF 表格走行级语义化（见中级题）。

**5. 向量数据库和普通数据库的区别？为什么两个都要？**
关系库精确匹配+事务+多条件查询；向量库做 ANN 近似最近邻（HNSW 索引），按语义相似度检索。本项目分工：Qdrant 存向量+payload（source/页码/tenant_id），负责"找内容"；PostgreSQL 存登记/状态/会话，负责"记账"——比如 `documents.status` 流转、消息历史的事实存储。

**6. top_k 是什么？你们设多少，为什么？**
返回相似度最高的 k 个片段。项目最终返回数默认 `top_k=5`，检索候选数与最终返回数分开配置。仓库冻结基线记录 107 道题中 101 道参与来源判定，100 道命中（Recall@5=99.01%，MRR=0.9703）；这是历史基线，不代表每次修改后的当前成绩，也不证明更大的 k 一定更好。

**7. 你们的 Prompt 是怎么设计的？**
Prompt 分布在 `backend/app/prompts/` 的 RAG、Agent 和 Text2SQL 模块；`context_builder` 将召回片段与来源组装进上下文。约束式提示、相关性过滤和拒答分支共同降低无依据回答风险，但不能保证消除幻觉。

**8. 单轮问答和多轮对话后端实现有何不同？**
单轮 `POST /query` 直接用当前问题检索生成。多轮 `POST /chat`：先解析/懒建 session（租户 ACL 校验），从会话存储取最近 20 条历史，检索仍只用当前问题（历史做指代歧义时会参与生成但不做检索改写——明确的取舍点，可作改进项），生成后 user+assistant 两条消息写回。流式版 `POST /chat/stream` 走 SSE。

**9. SSE 和 WebSocket 的区别？为什么选 SSE？**
SSE 是 HTTP 单向服务器推送：实现简单、走普通反代、自动重连、天然穿透 nginx（只需 `proxy_buffering off`）；WebSocket 双向但要多处理升级/心跳/半开连接。LLM 输出是单向流，SSE 够用且运维成本低。本项目事件契约 `meta(来源)→delta(逐字)→done/error`，前端 ReadableStream 手工解帧。

**10. 你们怎么鉴权？为什么不用 JWT？**
支持 `X-API-Key` 或 Bearer Key 映射租户，并可通过配置映射角色；Principal 的角色参与 SQL 权限控制与用量可见范围。开发配置可以关闭鉴权，此时没有租户检索过滤，不能作为生产隔离模式。当前不是 JWT/OIDC 用户登录体系，也没有完整的同租户用户/部门文档 ACL；改造认证方式会影响多个依赖和授权入口，不能说成只替换一个函数。

**11. 支持哪些文档格式？各自解析要点？**
PDF/DOCX/XLSX/MD/TXT（`supported_extensions` 白名单）。要点：PDF 有表格三级提取+页码清洗+marker 兜底；DOCX 段落与表格分别抽取保结构；XLSX 多 sheet、行级语义化；MD 按标题层级切并识别内嵌表格；TXT 最简滑窗。统一输出 `{text, metadata}` 文档对象进同一条 chunking→索引管道。

**12. 项目怎么做质量评估？**
`backend/eval`：题集为 `test_dataset/qa_test_v2.jsonl`，retrieval-only 模式不调用 LLM，只测期望来源召回并输出逐题结果。冻结基线为 101 道参与判定题中 100 道命中，Recall@5=99.01%、MRR=0.9703。评测结果须注明题集和基线版本；测试规模与测试类型以当前 `backend/tests/` 和实际运行报告为准，不能沿用早期“45 例”的数字。

**13. 什么是"拒答"？为什么重要？**
拒答行为随链路而异：经典单轮与流式 RAG 在无有效来源时可直接拒答；历史对话和 Agent 收尾路径可能仍调用模型组织回答。应分别检查 `backend/app/rag/pipeline.py` 与 `backend/app/agent/loop.py`，不能把所有入口说成同一种固定拒答。

**14. 文档状态 pending→ready 是怎么流转的？**
API 版本化上传会登记文档版本和入库任务；worker 完成解析与索引后才将新版本标记为可发布，失败版本不会替代当前版本。任务状态记录排队、运行、成功或失败等过程。文档/版本状态与任务状态是两套状态，具体字段和转换请看模型与版本化入库说明。

**15. Redis 在你项目里承担什么角色？挂了会怎样？**
两件事：①会话热缓存；②ARQ 入库任务队列。PostgreSQL 是会话持久化事实来源，但这不等于 Redis 异常时所有会话接口都会自动回源：当前服务的读取回源主要发生在缓存未命中，Redis 连接异常可能使请求失败。队列不可用时新任务无法正常入队或消费。故障行为要按具体调用路径描述，不能概括成“Redis 挂了只损失性能”。

**16. FastAPI 相比 Flask 的优势？项目里怎么体现？**
原生 async + Pydantic 校验 + 自动 OpenAPI。体现：路由协程配合 `run_in_threadpool` 桥接同步 RAG 内核；依赖注入（`get_principal/get_pipeline`）让测试可整体替换依赖；response_model 契约化 API（前后端 types 对齐）；lifespan 集中管理 DB/Redis/ARQ 连接池的启停。

**17. session_id 是怎么生成和传递的？**
后端 `uuid4().hex` 生成 32 位 id，`POST /sessions` 返回后前端存 localStorage(`rag_session_id`)，每次请求带回。服务端以 PG `sessions` 表为权威（含 tenant_id），非本租户的 session 一律视为不存在（自动新建，防串会话）。前端另存一份 Key 的 djb2 指纹(`rag_session_tenant`)，切租户即弃旧会话。

**18. Docker Compose 起哪些服务？migrate 服务是干嘛的？**
qdrant/postgres/redis/migrate/backend/worker/frontend 七个容器（migrate 一次性）。migrate 跑 `alembic upgrade head` 建表后退出，backend/worker 用 `depends_on: {migrate: service_completed_successfully}` 等它退出码 0——保证应用启动时 schema 必然就绪，不用在应用代码里塞建表 hack。健康检查链：pg_isready/PING//api/v1/health。

**19. 前端怎么做到"逐字输出"？遇到中文乱码吗？**
fetch 拿 `ReadableStream`，TextDecoder(`stream:true`) 增量解码，按 `\n\n` 切 SSE 帧、剥 `data:` 前缀再 JSON.parse。乱码坑真实踩过：按字节 chunk 直接 toString 会把多字节汉字劈开，必须用流式解码器；服务端侧 nginx 对该 location 关缓冲，否则逐帧变整块。

**20. 怎么本地把整个项目跑起来？**
按根目录 README 的“启动与演示”步骤准备 `.env` 后启动 Compose；前端不包含构建期演示 Key，需要在浏览器运行时设置 API Key。README 中的命令、初始化步骤和验证路径是本地启动依据；不要假设空容器已经装载知识文档或 Analytics 数据。

---

## 中级（实现细节与取舍，20 题）

**21. 详细讲你的混合检索实现。**
Qdrant 单 collection 双 named vectors：`dense`（bge-m3 1024d）+ `bm25` 稀疏向量（自管分词）。查询链路（`vector/qdrant.py search`）：①`tokenize_for_bm25` 对 query 做 jieba 分词+英数串保护（**与入库对称**，否则词表错位）；②prefetch_k=20 双路召回，RRF（reciprocal rank fusion，score=Σ1/(k+rank)）按排名融合——避开两路分数量纲不可比的问题；③融合候选送 bge-reranker-v2-m3 交叉编码精排；④rerank 绝对分做低相关过滤；⑤截 top_k=5 返回，stats 带 dropped 计数。

**22. RRF 为什么用排名而不是分数融合？**
dense 的 cosine∈[0,1] 与稀疏 BM25 分∈[0,+∞) 量纲/分布完全不同，线性加权需要逐 query 校准，很脆。RRF 只用名次：`1/(60+rank)` 求和，天然对分数分布免疫、对离群高分不敏感，一行参数都不用调。代价：丢失分差信息（第1名领先第2名很多和刚好领先同等看待），所以后面还要接 rerank 精排纠偏——这是"两级架构"的逻辑闭环。

**23. 低相关过滤的"量纲安全"具体指什么？**
普遍误区：对融合分/向量分套统一绝对阈值。本项目 `_filter_low_relevance` 按 score_kind 分支：**仅 rerank 绝对相关分**走组合策略——`SCORE_ABS_MIN=0.05` 下限、以最高分为锚的相对断层 `SCORE_REL_RATIO=0.15`、`SCORE_MIN_KEEP=1` 保底（防整页误杀）、`confident=0.5` 强锚点判定；RRF/原始向量分只按名次截断不设绝对限。效果：低质片段被滤且"已隐藏 N 条"可解释（dropped 计数沿 stats→retriever→pipeline.meta→前端徽标全链路透传）。

**24. rerank API 挂了怎么办？你们真踩过吗？**
`_rerank_points` 捕获异常降级为 RRF 原序返回，服务不中断——精排是增强项不是硬依赖。真踩的坑是第二层：**候选扩量到 20 后，降级分支忘了按 top_k 截断**，导致返回 20 条（评测发现"通过但超量"的怪数据）。修复：降级路径统一 `[:top_k]` 并加单测钉死。教训：任何 fallback 分支都要和主路径有相同的出口契约。

**25. 多租户隔离怎么实现的？为什么不用每租户一个 collection？**
单 collection + payload `tenant_id` keyword **多值**字段 + 服务端强制过滤：`enforced_filter` 注入当前租户且剥除客户端传入的租户键（防越权覆盖）；tenant_id 建 payload 索引（`_ensure_tenant_index`）。对比方案：①每租户一 collection——隔离最硬但 collection 数爆炸、HNSW 索引/内存碎片化、跨租户共享文档要复制；②每租户独立 Qdrant——运维不可承受。多值 keyword 还能原生表达"一份文档授权多租户"，未来文档级权限可直接复用该机制。跨租户数据在检索/列表/删除三条路径统一表现为 404/空。

**26. 上传大文件为什么会"秒回"？完整链路讲一遍。**
因为上传只做记账不做重活：`IngestionService.register_document` = 落盘（uuid8 前缀防同名覆盖）+ `Document(pending)` + `IngestTask(queued)` + `arq pool.enqueue_job` → 202+task_id。worker 消费：置 running(10) → `asyncio.to_thread(process_file)`（解析/切分/embed/upsert，同步重活进线程池避免卡事件循环）→ Document 回写(ready,chunk_count) + task done(100)。失败置 failed+error 并 re-raise 触发 ARQ 重试(max_tries=5)。前端轮询状态接口（带租户 ACL）。

**27. 为什么 RAG 内核是同步的？全改 async 不好吗？**
qdrant-client/requests 等同步生态成熟、心智负担低，pipeline 已是"纯函数式"读路径。同步/异步混合的工程答案不是重写而是**桥接三件套**：HTTP 入口 `run_in_threadpool`（query）、流式 `iterate_in_threadpool`（SSE 逐帧透传不阻塞循环）、worker `asyncio.to_thread`（重活）。全 async 化收益主要是极限并发，但要把整条链（含 marker/pdfplumber 等无 async 版的库）包 to_thread，改造收益低回归风险高——面试时这是"克制"的加分叙事。

**28. SSE 的事件契约具体定义？为什么先 meta 后 delta？**
`{type:"meta", sources, retrieved_count, hidden_count}` → `{type:"delta", text}`* → `{type:"done", answer}` / `{type:"error", message}`。meta 先行让用户第一时间看到来源和过滤徽标（感知延迟<1s），done 定稿供落库/复制。前端对契约的鲁棒性：忽略未知事件类型；AbortController 支持中断。评测与 API 结果键（query/answer/sources/retrieved_count/hidden_count）是同一契约的两种载体，迁移时逐项对齐保前端无感。

**29. 表格类文档怎么检索？讲讲行级语义化。**
痛点："型号 X7 的质保几年"这种 query 对着几百行 markdown 表格，embedding 与 BM25 都失灵。方案（`chunking/table.py`）：`table_rows_to_sentences` 把每行改写成自然语言句——`_humanize_column` 美化列名、`_guess_entity_word` 从表头猜实体词（"产品/条款/项目"）、`_find_key_col` 选关键列做主语，产出"X7 路由器 的 质保期 为 3年"式句子块；大表按 `split_markdown_table` 切块且**每块补表头**（`prepend_header`），保住列名语义。xlsx 多 sheet 逐 sheet 走同一路径。

**30. PDF 解析你们做了什么深活？**
`loader.py`(1592 行) 三件事：①表格三级提取链：pdfplumber（线框准）→ PyMuPDF `get_tables` → 自研 `extract_tables_with_dict`+`detect_columns`（无线框/排版怪的最后兜底），提取结果转 markdown 再进表格切块；②噪声清洗：`preprocess_page` 去页眉页脚页码标记（切块后还会 `clean_chunks_page_markers` 二次清）；③极端版面用 marker 模型转 md（`timeout=900s`），**未安装/超时则优雅降级**纯文本继续——解析失败不允许整批入库崩掉。

**31. 小 chunk 合并为什么需要"受保护块"？踩过什么坑？**
`merge_small_chunks` 把 <100 字符碎片并入邻块提升语义密度，但曾把**表格块与周围普通文本粘合**——表格行级语义块被稀释，检索质量回归（评测分数掉下来的实锤）。修复：`_is_protected_chunk` 识别表格/代码块，受保护块**禁止跨类型合并**（同类也不硬并，宁留小体积）。原则：分块策略的每个"优化"都必须过评测回归验证，防止直觉性反作用。

**32. 重灌/删除时向量怎么不残留？ID 幂等怎么回事？**
约定"文件名 = Document.source = Qdrant payload source = 删除键"，重灌前 `delete_by_metadata({"source":...})` 精确清旧点（防新旧两份 chunk 混检）。点 ID = `uuid5(text+metadata)`——同内容同 ID，重复 upsert 覆盖不产生副本。曾踩坑：迁移早期删除键与写入 payload 字段不一致导致向量残留，后来用"单一约定+删除三段验证（vectors_cleared/file_removed/行消失）"钉死，并写进《全景解析》坑表。

**33. "PG 事实来源 + Redis 热缓存"的会话读写路径讲一下。**
读 `get_history`：exists(ACL)→Redis 命中则 touch 续期返回；miss（用 EXISTS 区分"空列表"与"未缓存"）→PG `get_recent_messages` 回源 + `reheat`（pipeline 覆盖重建 LTRIM+EXPIRE）。写 `append`：**先 PG（seq=next_seq、CASCADE 归属、touch 活跃时间）后 Redis**，顺序不可反——先写缓存会出现"缓存有库里无"的幻象数据。租户不符静默跳过（对齐原内存实现语义）。效果：重启/清缓存会话不丢，热路径又免去每次全表读。

**34. ingest_tasks 的 document_id 为什么用 ON DELETE SET NULL？**
删除文档时业务上要"文档没了但历史任务记录保留"（审计/排障）。两种坏选择：CASCADE 会把任务证据一起删；RESTRICT 会报错阻断删除。SET NULL 让任务行留存且 document_id 置空。同理 messages 用 CASCADE——会话删除时消息没有独立留存价值。每个外键动作都是一次数据生命周期决策，面试值得展开。

**35. 删除文档为什么"先清向量、最后删 DB 行"？**
一致性次序设计（`delete_document`）：外部资源清理（Qdrant→磁盘文件）先做且**任何一步失败就中止保留登记行**——行在，用户可重试，重试幂等（向量已清则 delete_by_metadata 返回 0 不报错）；若反过来先删行，半途失败就产生"向量/文件孤儿且无账可查"。跨租户在入口 `get_by_id(id, tenant)` 统一 404。两个私有函数 `_clear_source_vectors/_remove_physical_file` 就是为单测 monkeypatch 设计的——编排逻辑纯本地可测。

**36. Alembic 在项目里怎么用的？为什么 compose 里做一次性 migrate 服务？**
schema 版本控制：`migrations/versions/35fd0e1e9233` 初始建 5 表+索引+外键；改 models 后 `alembic revision --autogenerate`。迁移用同步 psycopg URL（`sync_postgres_url`），运行时用 asyncpg——一次性脚本任务不需要异步。compose 把 `alembic upgrade head` 做成一次性 migrate 容器，backend/worker `service_completed_successfully` 等待：应用代码永远假设表存在，避免"启动时建表"的竞态与权限混乱。另有个环境坑：alembic.ini 在 Windows 按 GBK 读，中文注释直接崩，ini 注释保持 ASCII。

**37. ARQ 为什么不用 Celery？worker 为什么单并发？**
选型：任务形态是"IO+CPU 混合型单步任务"，ARQ 原生 asyncio、只依赖 Redis（本来就有）、配置即 WorkerSettings 类，比 Celery（要 broker+routing+result backend 全家桶）轻一个量级；P0 决策记在项目里。坑：`RedisSettings.from_dsn`（不是 from_url）。`max_jobs=1` 是刻意的：保证同一文件不会并行重灌（delete+upsert 竞态），job_timeout=600 给大 PDF 留量。吞吐不足时升级路径是"按文件粒度分队列+多副本+幂等已具备"，不是盲目调大并发。

**38. 前端"切租户不串会话"是怎么做的？**
会话绑定 Key 指纹：`createNewSession` 成功时除存 `rag_session_id` 再存 `rag_session_tenant`（API Key 的 djb2 hex 指纹）；`ensureSession` 只在指纹与当前 Key 一致时复用，否则清掉两个键重建；新建失败（401）同样清残留。为什么存指纹不存 Key 本身：不落敏感原文、比对 O(1)。配合后端"非本租户 session 视为不存在自动新建"的 ACL，双端闭环。Key 本身 localStorage 优先于构建期 VITE 变量，随时可切。

**39. Langfuse 埋点怎么做到"不配置就零开销"？**
`observability/langfuse.py` 封装 `trace()/span()` 上下文管理器：`settings.langfuse_enabled`（public+secret 都非空才真）为假时返回 no-op 上下文，pipeline 代码里埋点语句照常写但不产生任何 I/O。真开启时记录 rag_query/rag_chat/rag_chat_stream trace、retrieve 子 span（output 带 retrieved/hidden 计数）、LLM generation 用量。埋点策略是"选择性打点"：只打编排节点，不在 chunk 循环里打，防 trace 噪音与费用。

**40. hidden_count（已隐藏 N 条）这个数据怎么从引擎走到 UI 徽标？**
完整透传链路五环：Qdrant `search` 内 `_filter_low_relevance` 返回 dropped → stats 字典 → `Retriever.search` 解包成 `(chunks, dropped)` → pipeline 三种出口（query 结果键 / stream meta 事件 / chat response）→ 前端 stores 存 currentMeta → SourcesList 组件渲染"已隐藏 N 条"徽标。设计意图：低分过滤是把双刃剑（可能用户觉得漏答），把"被藏了多少"暴露出来让行为可解释、可反馈。这条链曾断过——迁移时 pipeline 返回键少了 dropped，UI 永远显示 0，靠端到端冒烟抓回。

---

## 高级（架构决策、规模化与演进，20 题）

**41. 多租户方案的完整权衡：什么时候你会换掉单 collection 字段隔离？**
三层方案谱系：字段 ACL（当前）→ collection 分区 → 独立实例。换 collection 的触发条件：①租户数使单 collection payload 过滤选择率恶化（大量租户×稀疏数据）；②租户间数据量悬殊引发 HNSW 局部退化与大租户抢占；③合规要求物理隔离（数据驻留/加密边界）。迁移成本低的原因：tenant_id 已贯穿 API/入库/删除全链，抽象点只在 `enforced_filter` 和 collection 名解析两处。面试答法：先讲当前方案为何占优（共享文档多值授权、运维面小），再讲边界与可退路径——体现"决策有条件"而非"选型有信仰"。

**42. 从你的系统角度，幻觉抑制的完整链路怎么设计？**
四层：①检索层——混合召回保"有据可查"，低分过滤保"无据不发"（宁缺毋滥）；②Prompt 层——系统模板约束"仅根据资料回答"，context 带来源编号供引用；③兜底层——无召回固定拒答/多轮"无相关参考资料"降级，`done` 事件保持契约一致；④验证层——sources 全量透传到 UI（文件名+页码+预览），人肉核对答案-依据对应关系，评测侧 expected-source 命中判分。再往企业级走：答案-引用一致性检查（每句话是否挂得上 source，LLM-as-judge 自动核）、事实冲突时的版本优先级策略。

**43. 检索指标不错但答案质量差，你怎么定位和改进？**
先归因分层：用 retrieval-only 评测检查期望来源是否召回，再结合答案质量评测框架、标注题和人工复核检查忠实度、相关性与拒答正确性。当前已有质量指标、judge 与比较工具，但小规模起始数据和未完成的真实模型评审不足以形成可信的生产答案质量分数。定位生成问题时，再检查上下文组织、Prompt、截断、历史污染和引用支持情况。

**44. 要支撑 1000 租户，现在架构哪些点会先崩？怎么改？**
按爆炸半径排序：①Qdrant 单 collection 过滤选择率与内存（1000 租户稀疏数据）——租户分片 collection 或 Qdrant 集群分片+tenant hash；②`RAG_TENANT_KEYS` 静态配置与重启轮换不可行——Key 入 PG 哈希存储+管理面 API；③worker max_jobs=1 吞吐——队列分级（小文件快车道）+多副本，幂等 ID 已保证安全；④rerank 每 query 外呼——结果缓存+批量归并，否则 P95 与费用双爆；⑤PG 连接池 20+10 单实例——读写分离/pgbouncer；⑥评测集按租户维度扩容否则质量不可知。答法要点：给出"先崩顺序"比罗列方案更能体现系统直觉。

**45. 异步入库的可靠性设计：重试、幂等、死信、对账你们各做到什么程度？**
已有 ARQ 重试、任务状态与错误记录、版本化发布保护，以及独立的备份/对账工具。对账模块和定时运维工作流已经存在，但仓库工作流的定时触发不代表已在生产主机成功执行并告警；失败任务也不应未经核对就等同于具备 DLQ。应分别说明任务重试、运维对账脚本和实际调度状态。

**46. Agent 方向：当前工具编排实现了什么，后续如何演进？**
当前系统已包含 Agent 工具循环，可调用知识检索、Text2SQL 和数据表发现工具，并有规则前置路由、步数/调用预算、重复调用保护及降级事件。它不是多 Agent 工作流，也没有完整的人工审批或长期任务编排。经典 RAG 仍保留独立入口；两条链路的路由和收尾行为应以 `backend/app/agent/` 为准。

**47. 成本工程：LLM 系统的钱都花在哪，你怎么控？**
成本来自检索 Embedding/Rerank 调用、回答生成、Agent 工具决策与 Text2SQL 重写等环节。仓库已有模型 token 用量记录及按场景/模型/租户/角色聚合的接口，但供应商价格、未回报用量和后台入库成本未必完整；不能据此声称已有硬性预算或超额自动降级。先结合实际调用日志和价格配置测量，再决定优化顺序。

**48. 灰度发布与回滚：schema、索引、模型三个层面各怎么做？**
schema：expand-contract 迁移（先加列/新表→双写过渡→切读→后删旧），alembic down 脚本必须实测；索引：collection 版本化——项目真实案例 `personal_rag_v2` 就是换维度/字段时的新 collection 并行重灌+别名切换（旧 collection 保留随时回切），配置 `QDRANT_COLLECTION_NAME` 一行完成切换；模型：embedding 模型与维度绑定（1024 写进 collection 配置），换模型=新版本 collection，不存在"热换"——这是 RAG 特有的发布约束，面试讲出来很识别内行。服务层：compose 镜像 tag+`up -d` 滚动，回滚即换 tag，healthcheck 兜底自动摘流。

**49. "忠实迁移"重构策略你是怎么执行的？为什么不全重写？**
这是项目的历史迁移背景：代码曾由早期单体结构整理到 `backend/` 分层，目前仓库根目录没有旧后端 `app/` 或 `src/`。讲重构方法时应以可核验的提交、验收记录和当前测试为证据；早期题库的测试数量及“迁移前后逐项相等”不能直接当作当前持续保证。

**50. 你的技术债清单排过优先级吗？第一个动的是哪个，为什么？**
按"爆炸半径×修复成本"排：先动安全与成本的三件——①Key 体系（静态明文→入库哈希+轮换+吊销）：它是所有企业客户接入的硬门槛，其他一切改进都会被它挡在门外；②限流/配额：LLM API 直通意味着一个脚本小子的成本事故，止损面最大；③rerank 缓存：P95 延迟与费用的单点，收益天天可见。而后端吞吐类（worker 扩容）反而靠后——当前单并发+重试已满足上传规模，"没有疼的债不急着还"。明确不做的：全链路 async 化（收益/风险比不划算，见 27 题）。这题答好的关键：让面试官看到你区分"技术优雅"与"业务疼点"的判断力。

**51. 文档级权限（同租户内不同人可见范围不同）怎么在现有架构落地？**
当前主要提供租户级检索隔离和 SQL 角色/列权限，没有完整的同租户用户、部门或文档 ACL。扩展时需要把授权标签同时纳入文档登记、上传/版本操作、向量过滤、列表和删除等入口，并补权限变更与历史会话策略；仅增加 Qdrant payload 字段不足以形成完整权限边界。

**52. 系统怎么可观测：metrics/logs/traces 三条腿现状与目标？**
当前已有结构化日志与 trace ID 中间件、Langfuse 可选追踪、轻量 Prometheus 文本指标、性能记录以及用量/SQL 统计 API；这些不等于已部署监控采集、长期保留、告警和看板。回答时应区分仓库内采集能力与目标环境的监控运营状态，并以 `.github/workflows/` 和部署配置核实实际接线。

**53. nginx/前端这一层有什么值得讲的工程决策？**
三个点：①同源反代降低跨域配置；②nginx 为 SSE 关闭响应缓冲；③API Key 当前由用户在浏览器运行时配置并保存在 localStorage，不编译进前端产物，但同源脚本仍可读取，XSS 防护和正式用户登录仍需治理。另可从当前组件中挑选实际存在的交互案例，讲清问题和修复证据，避免沿用已改版 UI 的历史截图或描述。

**54. 评测体系怎么从"能跑"升级到"可信的门禁"？**
仓库已有检索基线、答案质量评测与逐题比较工具，也有服务运行态验收脚本；这些工具尚未组成完整的 CI 质量门禁，也没有自动线上反馈闭环。演进可先扩充带标签的代表性题集和复核标准，再将稳定、低噪声的离线检查接入 CI，最后评估线上反馈采集与治理。

**55. 如果重做一次这个项目，你会改哪三个决策？**
准备这题是资深面试的信号题——考反思而非悔棋。候选：①API 契约先行：前端是后完成后接的，字段靠注释约定，中间吃过"结果键漂移"（hidden_count 断链）的亏——应一开始 OpenAPI schema + 前端 codegen（types.ts 自动生成）；②payload schema 与删除键约定应更早写成 ADR：`source=文件名=删除键` 这个隐性契约散落三个模块，差点因不一致造成向量残留；③测试基建立得更早：hermetic 单测是在 P4 worker 阶段才补的，如果 P1（vector 迁移）就建好，量纲过滤这类坑不会等到评测阶段才暴露。每一条都带"付出了什么代价"的具体事实，比空谈"会更模块化"有力得多。

**56. 知识图谱/GraphRAG 要不要上？你的判断框架是什么？**
判断框架：失败案例归因决定引入成本。当前评测失败集中在"表格行级/跨块聚合"类——GraphRAG 对"实体-关系多跳"类问题（A 公司的 B 项目的负责人参与过哪些合同）才有显著收益，对表格逐行事实无优势。引入成本：抽取质量（LLM 批量抽取实体的噪声）、增量更新（文档删除时图谱一致性比向量删除难得多）、运维面。结论：不上，但把"多跳类失败占比"设进评测监控，超阈值再启动——届时方案是混合路由（检索型走现有管线，关系型 query 路由到图）。核心答法：新技术的引入由评测分布驱动，不由 demo 驱动。

**57. 讲讲你在这个项目里做过的最有技术含量的一次 debug。**
推荐讲 uv 互搏事件（真实、完整、跨工具链）：现象——PyCharm 每次启动 backend 卸 11 装 11 还重新下载；假设链——先怀疑"关程序释放"（否，venv 是磁盘目录）→ `uv run` 隐式 `sync --exact`（方向对）→ 真凶定位用探针：跑两次 `uv sync`，第一次卸了 6 个 pytest 系包、第二次零动作——证明"环境里存在 lock 外的包被 uv 清除，而 PyCharm 测试集成又会装回"的循环；根因是声明位置错误：pytest 在 `[project.optional-dependencies]`（extras，uv 默认不同步）而 PyCharm 需要它。修复：迁移到 PEP 735 `[dependency-groups].dev`，lock+sync 二连静默，45 测试在新 venv 全绿。展示点：可复现探针设计（"pass2 必须零动作"作为判据）、对工具语义的精确理解（extras vs groups）、修复不伤 Docker 构建（pip 忽略 dependency-groups）。

**58. 这个系统离"生产可用"还差什么？给一个上线 checklist。**
上线前应核实部署实际使用的认证与密钥管理、限流和传输安全；检查备份策略、恢复报告中未解决的 Qdrant 物理快照问题、失败任务告警和对账调度；确认指标接入、跨 worker trace、容量与并发目标；再完成针对实际环境的权限和恢复演练。仓库有工具或工作流不代表生产环境已启用。当前尚无完整的 CI 测试/评测门禁；应以现行配置和真实演练结果判定缺口，不沿用本文早期的概括性清单。

**59. 如何向非技术决策者介绍这个项目的价值与投入？**
练的是表达降维能力。价值可以概括为：让企业文档可检索并附带来源、用 SQL 处理全量统计、通过租户边界控制数据访问。项目具备本地容器化演示能力和历史检索基线；它们不能单独证明生产可用、答案正确率或完整的部门级文档权限。谈投入时应根据实际部署和容量验收结果估算，不沿用未经验证的工期承诺。答案质量仍受文档质量和评测覆盖影响，面试时要讲清已实现范围与待验收事项。

**60. 项目里你最满意的一个设计决策？为什么？**
开放收尾题，推荐答"量纲安全的低分过滤"：①问题识别力——它不是教科书问题，是"把 RRF 分当相关性分套阈值"这一流行错误的自发纠偏（能讲清为什么多数人会做错）；②方案分寸——没有引入校准模型等重武器，而是"仅对 rerank 绝对分建阈值体系+相对断层+保底 N 条"的组合拳，简单但边界严密；③工程闭环——把"被过滤多少"变成产品可见性（前端徽标透传链路），可解释性做进 UI；④防回归——策略被单测钉死、评测验证净收益。一道小题同时体现"洞察、克制、闭环"三种素质，这正是要传递给面试官的信号。

---

## 附：答题节奏建议
- 初级题：概念一句话 + 项目实例一句 + 一个参数/文件位置，证明"亲手做过"。
- 中级题：先讲机制再讲"为什么这么选"，主动带一个相关踩坑（坑从《全景解析》18 条里取）。
- 高级题：结构 = 当前决策与理由 → 边界/失效条件 → 演进路径 → 成本意识。区分“代码具备工具”“环境已部署运行”和“有验收证据”；缺口要根据最新代码和专项报告描述，不复述早期题库里的过时清单。
