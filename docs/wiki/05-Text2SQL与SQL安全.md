# 05 · Text2SQL 与 SQL 安全

## 1. Text2SQL 引擎链路 — [text2sql.py](file:///d:/PythonProjects/personal_rag/backend/app/analytics/text2sql.py)

`Text2SqlEngine.answer()` 完整链路：

```
问题
 ├─ 1. Schema Linking（schema.select_relevant_schema_text 裁剪相关表）
 ├─ 2. Few-shot 检索（fewshot.retrieve 召回相似 question-SQL）
 ├─ 3. 口径歧义检查（易歧义指标 + 未给口径 → 反问，不猜）
 ├─ 4. LLM 生成 SQL（function calling，含 sql_query 与 ask_clarification 两工具）
 ├─ 5. guard 校验（AST 白名单 + LIMIT 注入）
 ├─ 6. EXPLAIN 预检（不执行，提前发现列名/类型错误）
 ├─ 7. 执行（只读事务 + 超时 + 结果上限）
 ├─ 8. 失败则数据库报错原样回灌，最多 sql_max_retry 次重写
 └─ 9. 结果 → 自然语言答案（回传 SQL 与结果表，可解释）
```

关键设计：

- **口径澄清**（`detect_ambiguity`）：`AMBIGUOUS_METRICS` 收录“客单价/增长率/转化率/总额”等强歧义指标；“平均薪资”类仅在未限定范围时才追问（`SCOPE_QUALIFIERS` 出现“部门/团队/全公司”等即视为已限定）。`_looks_like_clarification` 识别模型不调工具直接用自由文本反问的形态。
- **错误回灌**：数据库报错（如 `column "dept" does not exist`）是最高质量修正信号，`_append_retry` 用 assistant/user 交替回灌。
- **结果→自然语言**（`summarize`）：小结果交给 LLM 组织，但 SQL 与结果表**原样回传前端**，不依赖 LLM 转述。

## 2. 四层 SQL 防御

> sqlglot 官方声明“parser 是 transpiler 不是 validator”，单独任何一层都能被绕过，因此四层叠加。

| 层级 | 模块 | 约束 |
| --- | --- | --- |
| ① AST 白名单 | [guard.py](file:///d:/PythonProjects/personal_rag/backend/app/analytics/guard.py) | 拒绝非 SELECT/多语句/系统目录/危险函数，强制 LIMIT 注入 |
| ② 事务与资源 | [executor.py](file:///d:/PythonProjects/personal_rag/backend/app/analytics/executor.py) | 只读事务 + `statement_timeout` + 结果行上限 + EXPLAIN 预检 |
| ③ 数据库权限 | [permissions.py](file:///d:/PythonProjects/personal_rag/backend/app/analytics/permissions.py) | 独立只读登录；employee 另用受限登录 + 列级授权 |
| ④ RLS | [ddl.py](file:///d:/PythonProjects/personal_rag/backend/app/analytics/ddl.py) | `is_shared`/`tenant_id` fail-closed 行级隔离 |

### 2.1 guard — AST 白名单

`guard_sql(raw_sql, max_rows) → GuardResult`：

- 剥 markdown 代码围栏 → `sqlglot.parse`（postgres 方言）→ 仅单条且根节点为 `Select`。
- 子树禁止 `Insert/Update/Delete/Drop/Create/Alter/Grant/Copy/Set/Transaction/...`。
- 拒绝系统 schema（`pg_catalog/information_schema/...`）、危险函数（`pg_read_file/pg_sleep/dblink/lo_import/...`）、账号表（`pg_shadow/pg_authid/...`）。
- **强制 LIMIT 注入**：外层包裹 `SELECT * FROM (<original>) AS _q LIMIT max_rows+1`，`max_rows+1` 用于截断判定（与 executor `fetchmany(max_rows+1)` 严格同步，有单测锁死不变式）。

`guard_and_record` 是带指标记录的包装（拒绝分类计数 `rag_sql_guard_rejections_total`）；`guard_sql` 保持纯函数可穷举单测。

### 2.2 executor — 只读事务与资源

`SqlExecutor.execute()`：

- 用 `sync_analytics_ro_url`（psycopg 同步 + `kb_ro` 只读角色）；**不能**配 `analytics_url`（asyncpg 会出现 MissingGreenlet）。
- `_apply_session_guards()`：`set_config('transaction_read_only','on',true)` + `set_config('statement_timeout',...,true)` + 无条件 `set_config('app.tenant_id', resolve_tenant_identity(tenant_id), true)`（用 `set_config` 而非 `SET LOCAL`，因 `SET` 不接受绑定参数）。
- `explain_sql()`：只用 `EXPLAIN (FORMAT JSON)`，**绝不 `ANALYZE`**（否则真执行，安全网关形同虚设）。
- `fetchmany(max_rows+1)` 判断截断；`to_jsonable()` 序列化 Decimal/date。
- 大结果集保护：超过 `inline_max_rows` 只给列名+行数+前几行样例+截断标志。

### 2.3 permissions — 列级授权

`ensure_employee_role()` 为 `kb_employee` 建立受限登录：NOSUPERUSER/NOINHERIT/NOBYPASSRLS、撤销既有成员关系、置 `default_transaction_read_only=on`、`statement_timeout='5s'`、逐表 `REVOKE ALL` 后仅 `GRANT SELECT(允许列)`。**不提供表级默认 SELECT，新表不可见直到显式授权**（fail-closed）。

`column_grants()` 生成列级授权语句；`sensitive_sql_reason(sql, redact)` 在 EXPLAIN 前预检模型 SQL 是否显式引用了敏感列（含 WHERE/别名/聚合/子查询/CTE），命中即拒绝。

### 2.4 ddl — RLS

`build_rls_sql(table)`：

```sql
USING (is_shared IS TRUE OR tenant_id = current_setting('app.tenant_id', true))
```

- `is_shared` 列区分“共享语料”与“租户私有”；`tenant_id` 区分归属。
- 默认值 `UNOWNED_TENANT='@unowned@'`（刻意非空串，防 `''=''` 意外匹配）。
- `ensure_tenant_columns()` 为存量表补列并装策略，**默认 fail-closed、绝不自动标记共享**，补列后有不可见行会计数告警。

`create_or_replace_table()` 覆盖前做 `assert_can_replace()` 归属检查：表名为“文件名+sheet”（不含租户），跨租户同名会算出同一表名，无条件 DROP 会物理删除他人数据，因此发现他租户行时抛 `TableOwnershipError` 中断入库。

## 3. 脱敏 — [redact.py](file:///d:/PythonProjects/personal_rag/backend/app/analytics/redact.py)

`redact_rows()` 在**结果集**上按列名脱敏（`MASK="***"`）：

- 选结果集脱敏而非拒绝/改写 SQL：LLM 常写 `SELECT *`，按列名脱敏不依赖模型生成什么。
- 局限（诚实标注）：别名会让列名不匹配；聚合值列名未必叫 salary → **因此数据库列级权限才是关键边界**，结果脱敏只是兜底。
- `redact_observation()` 明确告知模型“这些列被脱敏，不要猜测编造”。

## 4. 审计 — [audit.py](file:///d:/PythonProjects/personal_rag/backend/app/analytics/audit.py)

`record_sql_audit()` 每次 SQL 执行落库（`SqlAudit`）：只记 SQL 与元数据（actor/tenant_id/ok/row_count/elapsed_ms/redacted_columns/error），**不记结果集**。审计失败绝不阻断主流程。

## 5. Schema 语义层 — [schema.py](file:///d:/PythonProjects/personal_rag/backend/app/analytics/schema.py)

- `load_table_schemas()`：读 `analytics_tables/analytics_columns` 元数据（`is_enabled` 白名单 + tenant 过滤）。
- `rank_tables()` / `select_relevant_schema_text()`：Schema Linking，按 display_name/列描述/枚举值打分裁剪前 `max_tables` 张。
- `render_schema()`：紧凑渲染成“数据字典”，枚举值内联。
- `filter_sensitive_columns()`：复制时剔除敏感列描述，受限请求看不到敏感列语义。

## 6. Few-shot — [fewshot.py](file:///d:/PythonProjects/personal_rag/backend/app/analytics/fewshot.py)

`retrieve()` 用轻量词面打分（中文单字+bigram、英文词）+ 聚合意图加权召回 top-k 相似 question-SQL；示例库来自 `app/prompts/data/*.jsonl`。刻意不存 Qdrant，避免污染已冻结检索基线。

## 7. ETL 装载 — [seed.py](file:///d:/PythonProjects/personal_rag/backend/app/analytics/seed.py)

- `sync_analytics_tables(path, tenant_id, is_shared)`：xlsx→`schema_infer` 推断表头/类型→`ddl.create_or_replace_table`→`_copy_rows` 用 COPY 批量装载→`_upsert_metadata` 写元数据。
- 归属（`tenant_id`）与可见性（`is_shared`）作为显式参数从 worker 一路传下，无硬编码兜底。
- `should_sync_to_analytics()` 只对 `.xlsx/.xlsm` 同步（PDF/DOCX 表格提取启发式、跨页截断，建表会引入静默错误）。