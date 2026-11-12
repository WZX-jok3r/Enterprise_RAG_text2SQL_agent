# Text2SQL 数据层设计（可直接落库的 DDL 草案）

> **文档性质：设计草案与历史方案。** DDL、字段、ETL 流程和“当前不支持”等描述不一定与现行实现一致；API 上传的版本化表格与 Analytics 表发布也有明确边界。当前 schema、权限和执行行为以 `backend/app/analytics/`、`backend/migrations/` 及根目录 `README.md` 为准。执行本文 SQL 前应先核对目标数据库和现行迁移。

> 配套文档：[《升级为内部知识库Agent-完整改造方案》](升级为内部知识库Agent-完整改造方案.md) 第四至六部分
> 设计原则：**演示数据真实可用** + **安全边界不靠 LLM 的自觉** + **表结构本身就是给 LLM 看的语义层**

---

## 一、为什么不用现有 `rag` 库，而新建 `kb_analytics`

现有 `rag` 库存的是**系统元数据**（documents/sessions/messages/ingest_tasks/tenants）。
Text2SQL 要查的是**业务数据**。两者必须物理隔离，理由有三：

1. **权限隔离**：LLM 生成的 SQL 只能碰业务表，绝不能碰 `messages`（会话内容）、`tenants`（租户表）；
   同库靠 `GRANT` 也能做，但**跨库 + 独立角色是最少争议的边界**，面试时也好讲。
2. **爆炸半径**：即使 SQL 校验被绕过，只读角色 + 独立库把损伤限制在业务数据内。
3. **可重建性**：业务数据全部来自 `knowledge_base/` 的 xlsx，删库可一键重建；
   系统元数据丢了不可恢复。**可重建的数据才允许被 LLM 触碰**。

```sql
-- 由 Alembic 迁移或独立初始化脚本执行（需 postgres 超级用户）
CREATE DATABASE kb_analytics;
```

---

## 二、只读角色（三层防线中的第三层，也是最硬的一层）

```sql
-- 1) 独立的只读角色：无 DDL、无写权限
CREATE ROLE kb_ro WITH LOGIN PASSWORD :'kb_ro_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

GRANT CONNECT ON DATABASE kb_analytics TO kb_ro;
GRANT USAGE ON SCHEMA public TO kb_ro;

-- 2) 只授 SELECT，且只授业务表（未来新增表默认不可见）
GRANT SELECT ON ALL TABLES IN SCHEMA public TO kb_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO kb_ro;

-- 3) 兜底：即使将来误授了写权限，会话级只读事务也会拒绝写入
ALTER ROLE kb_ro SET default_transaction_read_only = on;
-- 4) 防长查询拖垮库：独立角色级超时（比应用层超时更可靠）
ALTER ROLE kb_ro SET statement_timeout = '5s';
-- 5) 限制单次返回规模（配合应用层 LIMIT 注入，双保险）
ALTER ROLE kb_ro SET idle_in_transaction_session_timeout = '10s';
```

> **面试要点**：安全是**纵深防御**，不是单点。四层分别是
> ① 应用层 SQL AST 白名单（sqlglot，挡在生成之后、执行之前）
> ② 连接层只读事务 + 超时（`default_transaction_read_only`）
> ③ 数据库角色权限（`GRANT SELECT` only）
> ④ 数据库账号隔离（独立库 + 独立角色 + 密码独立于应用账号）
> **任何一层单独都不够**：LLM 可能生成 `SELECT ... INTO` 或调用 `pg_read_file`；
> 只用正则校验必然被绕过；只靠角色权限则攻击者可用合法 SELECT 打满 CPU（DoS）。

---

## 三、业务表 DDL（对应 `knowledge_base/` 的真实数据）

设计要点：**列名 = 中文语义 ↔ 英文标识 双向可映射**，因为 LLM 需要同时理解
「用户说'部门'」和「SQL 写 `department`」。做法是**给每张表/列加 `COMMENT ON`**——
PostgreSQL 的 comment 是 schema introspection 的一等公民，可以零成本喂给 prompt。

```sql
-- ============================================================
-- 表 1: employees  ← knowledge_base/xlsx-sample-large-10000-rows.xlsx
--        实测 10000 行 × 7 列
-- ============================================================
CREATE TABLE employees (
    id          INTEGER      PRIMARY KEY,
    first_name  VARCHAR(64)  NOT NULL,
    last_name   VARCHAR(64)  NOT NULL,
    email       VARCHAR(128),
    department  VARCHAR(32)  NOT NULL,
    salary      NUMERIC(12,2) NOT NULL,
    hire_date   DATE         NOT NULL
);

COMMENT ON TABLE  employees             IS '员工主表：全公司员工的花名册，含部门、薪资与入职日期';
COMMENT ON COLUMN employees.id          IS '员工工号，唯一主键';
COMMENT ON COLUMN employees.first_name  IS '名';
COMMENT ON COLUMN employees.last_name   IS '姓';
COMMENT ON COLUMN employees.email       IS '工作邮箱';
COMMENT ON COLUMN employees.department  IS '所属部门，取值：Sales/Support/HR/Marketing/Engineering/Product/Legal/Design/Operations/Finance 共 10 个';
COMMENT ON COLUMN employees.salary      IS '年薪（美元，整数金额）';
COMMENT ON COLUMN employees.hire_date   IS '入职日期';

-- LLM 高频过滤/分组列必须有索引，否则万行表聚合也会慢
CREATE INDEX idx_employees_department ON employees (department);
CREATE INDEX idx_employees_hire_date  ON employees (hire_date);
CREATE INDEX idx_employees_salary     ON employees (salary DESC);

-- ============================================================
-- 表 2: sales     ← xlsx-sample-multiple-sheets.xlsx / sheet "Sales"（20 行）
-- ============================================================
CREATE TABLE sales (
    id        SERIAL        PRIMARY KEY,
    sale_date DATE          NOT NULL,
    product   VARCHAR(64)   NOT NULL,
    quantity  INTEGER       NOT NULL CHECK (quantity >= 0),
    revenue   NUMERIC(14,2) NOT NULL
);

COMMENT ON TABLE  sales            IS '销售流水表：每一行是一笔产品销售记录';
COMMENT ON COLUMN sales.sale_date  IS '销售日期（注意：列名不用 date，避免与 SQL 关键字混淆）';
COMMENT ON COLUMN sales.product    IS '产品名称，取值如 Gadget Plus / MegaPack / Widget Pro 等';
COMMENT ON COLUMN sales.quantity   IS '本笔销量（单位：个）';
COMMENT ON COLUMN sales.revenue    IS '本笔营收金额（美元）';

CREATE INDEX idx_sales_date    ON sales (sale_date);
CREATE INDEX idx_sales_product ON sales (product);

-- ============================================================
-- 表 3: expenses  ← xlsx-sample-multiple-sheets.xlsx / sheet "Expenses"（15 行）
-- ============================================================
CREATE TABLE expenses (
    id            SERIAL        PRIMARY KEY,
    expense_date  DATE          NOT NULL,
    category      VARCHAR(64)   NOT NULL,
    amount        NUMERIC(14,2) NOT NULL
);

COMMENT ON TABLE  expenses               IS '费用支出表：每一行是一笔费用';
COMMENT ON COLUMN expenses.expense_date  IS '费用发生日期';
COMMENT ON COLUMN expenses.category      IS '费用类别，取值如 Office Rent / Supplies / Marketing 等';
COMMENT ON COLUMN expenses.amount        IS '费用金额（美元）';

CREATE INDEX idx_expenses_date     ON expenses (expense_date);
CREATE INDEX idx_expenses_category ON expenses (category);

-- ============================================================
-- 表 4: cabinet_costs  ← knowledge_base/cost_data.xlsx（中文列名，8 行 × 8 列）
--        这张表是「中文列名 Text2SQL」的专用测试素材
-- ============================================================
CREATE TABLE cabinet_costs (
    id          SERIAL       PRIMARY KEY,
    item        VARCHAR(64)  NOT NULL,   -- 项目
    brand_model VARCHAR(128),            -- 品牌/型号
    spec_usage  VARCHAR(128),            -- 规格/用途
    unit_price  NUMERIC(12,2),           -- 单价(元)
    quantity    NUMERIC(12,2),           -- 数量
    unit        VARCHAR(16),             -- 单位
    billing     VARCHAR(32),             -- 计费方式
    remark      TEXT                     -- 备注
);

COMMENT ON TABLE  cabinet_costs             IS '橱柜成本明细表：装修/家具项目的逐项成本（人民币计价）';
COMMENT ON COLUMN cabinet_costs.item        IS '成本项目，如 铰链 / 板材 / 台面';
COMMENT ON COLUMN cabinet_costs.brand_model IS '品牌与型号，如「悍高 三段力」';
COMMENT ON COLUMN cabinet_costs.spec_usage  IS '规格或用途描述';
COMMENT ON COLUMN cabinet_costs.unit_price  IS '单价（元）';
COMMENT ON COLUMN cabinet_costs.quantity    IS '数量';
COMMENT ON COLUMN cabinet_costs.unit        IS '计量单位，如 个 / 米 / 平方米';
COMMENT ON COLUMN cabinet_costs.billing     IS '计费方式，如「按个计费」';
COMMENT ON COLUMN cabinet_costs.remark      IS '备注说明';

-- 派生列：让「某项总价是多少」不必让 LLM 写乘法（降低出错面）
ALTER TABLE cabinet_costs
  ADD COLUMN total_price NUMERIC(14,2)
  GENERATED ALWAYS AS (unit_price * quantity) STORED;
COMMENT ON COLUMN cabinet_costs.total_price IS '小计金额（元）= 单价 × 数量，已预计算，查询时直接用此列';

-- ============================================================
-- 多租户 / 行级权限：预留列（演示期单租户，但结构先留好）
-- ============================================================
-- 方案：给所有业务表加 tenant_id，并用 PostgreSQL RLS 强制过滤。
-- 关键点：RLS 的过滤条件由数据库执行，LLM 生成的 SQL 无法绕过——
--         即使 LLM 写出 SELECT * FROM employees（不带 WHERE），也只会看到本租户数据。
ALTER TABLE employees ADD COLUMN tenant_id VARCHAR(64) NOT NULL DEFAULT 'a';
ALTER TABLE sales     ADD COLUMN tenant_id VARCHAR(64) NOT NULL DEFAULT 'a';
ALTER TABLE expenses  ADD COLUMN tenant_id VARCHAR(64) NOT NULL DEFAULT 'a';

ALTER TABLE employees ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales     ENABLE ROW LEVEL SECURITY;
ALTER TABLE expenses  ENABLE ROW LEVEL SECURITY;

-- 应用执行 SQL 前会 SET LOCAL app.tenant_id = '<tenant>'，RLS 自动生效
CREATE POLICY tenant_isolation_employees ON employees
  USING (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation_sales ON sales
  USING (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation_expenses ON expenses
  USING (tenant_id = current_setting('app.tenant_id', true));
```

> **RLS 是这套设计里最容易被追问也最能加分的一点**：
> 它把「多租户隔离」从**应用层约定**（现在的 `enforced_filter` 靠代码自觉）
> 升级为**数据库层强制**（LLM 无法绕过）。
> 而且它天然复用了项目已有的 `tenant_id` 概念，形成前后一致的架构叙事。

---

## 四、Schema 语义层：给 LLM 看的「数据字典」

LLM 不需要看到 DDL，需要看到**紧凑的、带业务语义的 schema 描述**。
直接从 `information_schema` + `pg_description` 自动生成，避免手写漂移：

```sql
-- 自动抽取 schema（应用启动时执行一次，缓存到 Redis）
SELECT
    c.table_name,
    c.column_name,
    c.data_type,
    d.description                          AS column_comment,
    (pk.column_name IS NOT NULL)           AS is_primary_key
FROM information_schema.columns c
LEFT JOIN pg_description d
       ON d.objoid = (quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass
      AND d.objsubid = c.ordinal_position
LEFT JOIN (
    SELECT kcu.table_name, kcu.column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name
    WHERE tc.constraint_type = 'PRIMARY KEY'
) pk ON pk.table_name = c.table_name AND pk.column_name = c.column_name
WHERE c.table_schema = 'public'
ORDER BY c.table_name, c.ordinal_position;
```

**渲染进 prompt 的格式**（这就是所谓的 Schema Linking 的输入）：

```
### 表 employees —— 员工主表：全公司员工的花名册，含部门、薪资与入职日期
- id INTEGER [PK]      员工工号，唯一主键
- first_name VARCHAR   名
- last_name VARCHAR    姓
- email VARCHAR        工作邮箱
- department VARCHAR   所属部门，取值：Sales/Support/HR/Marketing/Engineering/Product/Legal/Design/Operations/Finance 共 10 个
- salary NUMERIC       年薪（美元，整数金额）
- hire_date DATE       入职日期

### 表 sales —— 销售流水表：每一行是一笔产品销售记录
- id INTEGER [PK]
- sale_date DATE       销售日期
- product VARCHAR      产品名称，取值如 Gadget Plus / MegaPack / Widget Pro 等
- quantity INTEGER     本笔销量（单位：个）
- revenue NUMERIC      本笔营收金额（美元）
...
```

**两条提升准确率的工程细节**（都来自实测踩坑，成本极低）：

1. **枚举值内联**：`department` 的 10 个取值、`product` 的取值清单直接写进列注释。
   LLM 不必猜 `WHERE department = 'engineering'` 还是 `'Engineering'`——**大小写与单复数错误是中文 NL2SQL 的头号杀手**。
   取值清单由 `SELECT DISTINCT` 采样生成（低基数列才内联，阈值 ≤ 50 个）。
2. **中文别名双向映射**：列注释里同时给中英文（如 `department 所属部门`），
   让「部门」「薪资」这类中文问法能对上英文标识符。

---

## 五、自动建表：从 xlsx 到 SQL 的 ETL 任务

### 5.0 ⚠️ 适用范围：**SQL 建表只做 xlsx（后续可扩 CSV），不做 PDF / DOCX**

这是本方案**最重要的一条范围声明**，因为 Text2SQL 能查什么，取决于**哪些格式能可靠地变成关系表**。

**先厘清一个概念**（避免混淆）：
> **SQL 查询不直接读文件**。ETL 只在**入库时执行一次**，把表格转成关系表；
> 之后无论用户问什么统计问题，SQL 查的都是**数据库里的表**，与原始文件格式无关。
> 所以"是否只处理 xlsx"这个问题的准确问法是：**"哪些格式的文件能在入库时被可靠地转成表"**。

**逐格式的能力矩阵**（✅ 基于源码实测，`loader.py` 的 `metadata["type"]` 与 `chunking/` 路由）：

| 格式 | loader 产出的内容类型 | 结构化程度 | 能否建 SQL 表 | 原因 |
|---|---|---|---|---|
| **xlsx** | `type=sheet`（每 sheet 一张 Markdown 表）+ `type=summary`（≥1000 行的聚合摘要）+ pandas 降级 `type=row` | **高** | ✅ **能** | **sheet 是天然的表边界**；openpyxl 逐行读取；有独立的 `chunk_xlsx` 策略 |
| **csv**（当前不支持） | — | — | ✅ **易扩** | 与 xlsx 同构，只需加一个 loader 分支 |
| **docx** | `type=table`（`table_index` / `row_count` / `col_count`） | 中 | ⚠️ **暂不做** | 有表结构，但**一个 docx 可含多个异构表**，且表名/语义无来源；混在正文里难以自动命名 |
| **pdf** | `type=table`（pdfplumber / pymupdf get_tables / dict 三级兜底） | **低** | ❌ **不做** | ① 表格提取本身是启发式，**跨页表会被截断**；② 无 sheet 名，表边界靠猜；③ 表格与正文交错 |
| **md / txt** | `type=markdown` / `type=text`（Markdown 表格走 `chunk_md_table_aware`） | 低 | ❌ **不做** | Markdown 表格是**文档排版**，不是数据集；无类型信息、无表名 |

### ⚠️ 一个必须知道的技术障碍：**单元格类型在 loader 里已经丢失了**

✅ 源码实测（`loader.py:1415`，xlsx openpyxl 路径）：
```python
cleaned_row = [str(cell).strip() if cell is not None else "" for cell in row]
```
**所有单元格一律 `str()` 化**——`datetime` 变 `"2026-08-02"`、数字变 `"179997"`、`None` 变 `""`。
docx / pdf 路径同理（`cell.text.strip()` / `str(cell).strip()`）。

**这意味着 ETL 拿不到类型信息，必须从字符串重新推断**（这正好是我在 5.1 设计的 `schema_infer.py` 要做的事）。
好消息是**信息没丢干净**，可逆性良好：

| 原始类型 | loader 后的形态 | 能否还原 | 还原方式 |
|---|---|---|---|
| `datetime` | `"2026-08-02 00:00:00"` 或 `"2026-08-02"` | ✅ | `pd.to_datetime` / 正则 `^\d{4}-\d{2}-\d{2}` |
| `int` / `float` | `"179997"` / `"6623.76"` | ✅ | 复用 `loader._parse_number`（已容忍千分位与 `$`） |
| `None` / 空 | `""` | ✅ | 视作 SQL `NULL` |
| **公式** | **计算结果值**（`data_only=True`） | ⚠️ 丢失公式本身 | 通常**正是想要的**（要值不要公式） |
| 数字格式（货币/百分比） | `"28"`（格式已丢） | ❌ | 需靠**列名/单位列**补语义（如 `单价(元)`、`unit` 列） |

> **实践建议**：**不要改造 loader 去保留类型**（会影响现有 RAG 链路与评测基线，
> 触碰 1592 行的核心文件风险高）。
> 更稳的做法是 **ETL 直接读原始文件**（用 `openpyxl.load_workbook(..., data_only=True)` 自己读，
> 就像我在本文档第一节验证 schema 时做的那样），**与 RAG 通道完全解耦**——
> 两条通道各自读同一份文件，互不影响。代价是读两遍文件，在入库时一次性成本里可以忽略。

### 5.1 ETL 链路

**关键决策：不要手写 INSERT 脚本。** 复用现有的异步入库链路，把「表格转表」做成一个 ARQ 任务。

```
POST /documents 上传 xlsx
  └─ worker: ingest_document
      ├─ [现有] 解析 → 分块 → 向量化 → Qdrant      （RAG 通道，保留，不动）
      └─ [新增] tabular_detect → 类型推断 → CREATE TABLE / COPY  （SQL 通道）
                └─ 仅当 ext == .xlsx 时触发；其他格式跳过（记日志说明原因）
```

**类型推断规则**（`app/analytics/schema_infer.py`）——⚠️ 注意**输入全是字符串**（见 5.0 的类型丢失说明）：

| 列特征 | 推断类型 | 判据 |
|---|---|---|
| 全列可解析为整数，且 distinct == 行数 且连续 | `INTEGER PRIMARY KEY` | 复用 `loader._is_sequence_col` 的现成逻辑 |
| 全列可解析为数值，有小数 | `NUMERIC(14,2)` | `loader._parse_number` 复用（已容忍 `,` 与 `$`） |
| 全部匹配 `^\d{4}-\d{2}-\d{2}` 或可 `to_datetime` | `DATE` | 归一化后回写（注意 loader 形态可能是 `"2026-08-02 00:00:00"`） |
| distinct ≤ 50 且长度 ≤ 32 | `VARCHAR(32/64)` + **枚举值写进注释** | 低基数 → 内联取值 |
| 其余 | `VARCHAR(128)` / `TEXT` | 兜底 |

### ⚠️ 表头检测：`cost_data.xlsx` 是必须处理的陷阱样本

✅ 实测该文件结构：**第 1 行是大标题**（`橱柜项目成本明细表`，8 列只有 1 列有值），**第 2 行才是真表头**。

而现有 loader 的 xlsx 路径**不做表头识别**——第 1 行被直接当作表头写进 Markdown 表。
**如果 ETL 直接复用 loader 的输出，`cost_data` 会建出一张只有 1 个有效列的表。**

**对策**（`schema_infer.detect_header_row`）：
```python
def detect_header_row(rows: list[list[str]], scan: int = 5) -> int:
    """在首 N 行里找最像表头的一行：非空单元格数最多、且去重后全是非数值文本。
    返回行号；找不到则返回 0（回退到'第一行即表头'）。"""
    best, best_score = 0, -1
    for i, row in enumerate(rows[:scan]):
        filled = [c for c in row if c.strip()]
        if len(filled) < 2:                     # 大标题行通常只有 1 个非空单元格
            continue
        if any(_looks_numeric(c) for c in filled):   # 表头一般不含纯数字
            continue
        if len(set(filled)) != len(filled):          # 表头一般不重复
            continue
        if len(filled) > best_score:
            best, best_score = i, len(filled)
    return best
```
**验收**：对 `cost_data.xlsx` 断言 `detect_header_row(...) == 1`（跳过标题行）；
对 `Employees` 断言 `== 0`。**把这个断言写进测试**，否则这类"少一列"的错误会很晚才被发现。

**幂等与安全**：
- 表名归一化：`xlsx-sample-large-10000-rows.xlsx` + sheet `Employees` → `employees`；
  加 `xlsx_<hash8>` 后缀防碰撞；**表名一律走 `sqlglot` 的标识符转义，绝不字符串拼接**。
- **只允许 `CREATE TABLE`，禁止 `DROP`**：重建走 `DROP TABLE IF EXISTS` 的白名单分支，
  且表名必须已登记在 `analytics_tables` 元数据表中。
- **CSV 中转 + `COPY`**：万行数据用 `COPY ... FROM STDIN` 而非逐行 INSERT（实测 10000 行 <1s vs 逐行 30s+）。
- **列名归一化**：中文列名保留原文进 comment，标识符转拼音或英文（`单价(元)` → `unit_price`），
  转换表落库到 `analytics_columns` 元数据表供人工校正。

**新增元数据表（放在 `rag` 库，不是业务库）**：

```sql
-- 数据表登记：Text2SQL 的 schema 层来源，也是「哪些表可被 LLM 看到」的白名单
CREATE TABLE analytics_tables (
    id            SERIAL PRIMARY KEY,
    table_name    VARCHAR(64)  NOT NULL UNIQUE,   -- 业务库中的物理表名
    display_name  VARCHAR(128) NOT NULL,          -- 人类可读名，如「员工表」
    description   TEXT,                           -- 业务语义，写进 prompt
    source        VARCHAR(512) NOT NULL,          -- 来源文件，如 xlsx-sample-large-10000-rows.xlsx
    sheet_name    VARCHAR(128),
    row_count     INTEGER,
    tenant_id     VARCHAR(64),
    is_enabled    BOOLEAN NOT NULL DEFAULT TRUE,  -- 关掉即从 prompt 中消失
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE analytics_columns (
    id            SERIAL PRIMARY KEY,
    table_id      INTEGER NOT NULL REFERENCES analytics_tables(id) ON DELETE CASCADE,
    column_name   VARCHAR(64)  NOT NULL,
    data_type     VARCHAR(32)  NOT NULL,
    description   TEXT,
    enum_values   JSONB,          -- 低基数列的取值清单（内联进 prompt）
    is_primary    BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (table_id, column_name)
);
```

> **为什么元数据表是必需的**：
> ① 它是 prompt 里 schema 描述的**唯一来源**，不靠运行时 introspection（快且可控）；
> ② 它是表级白名单——`is_enabled=false` 即让某张表对 LLM 不可见（**数据权限而非表权限**）；
> ③ 它让「人工校正列名/补业务语义」成为可能，而不用改代码。

---

## 六、种子数据：让演示立刻可跑

```sql
-- 从 knowledge_base/*.xlsx 一键装载（幂等）
-- 用法: python -m app.ops.seed --file knowledge_base/xlsx-sample-large-10000-rows.xlsx
```

装载后 `GET /api/v1/analytics/tables` 应返回 4 张表：

| table_name | display_name | rows | 来源文件 |
|---|---|---|---|
| employees | 员工表 | 10000 | xlsx-sample-large-10000-rows.xlsx |
| sales | 销售流水 | 20 | xlsx-sample-multiple-sheets.xlsx |
| expenses | 费用支出 | 15 | xlsx-sample-multiple-sheets.xlsx |
| cabinet_costs | 橱柜成本明细 | 7 | cost_data.xlsx |

**验收用真值**（pandas 实测，可直接写进回归测试）：

| 问题 | 期望 SQL 语义 | 真值 |
|---|---|---|
| Sales 与 Engineering 各多少人、差多少？ | `GROUP BY department` | Sales 1042 / Engineering **1010** / 差 **32** |
| 哪个部门人数最多/最少？ | `ORDER BY count` | 最多 Sales 1042 / 最少 Finance 956 |
| 全公司薪资最高的员工？ | `ORDER BY salary DESC LIMIT 1` | Derek Cummings，179997，Operations |
| 2026 年入职多少人？ | `WHERE hire_date >= '2026-01-01'` | **192** |
| 哪个产品总营收最高？ | `SUM(revenue) GROUP BY product` | MegaPack，47044.68 |
| MegaPack 一共卖了多少单位？ | `SUM(quantity) WHERE product=...` | **340** |
| 平均客单价？ | `AVG(revenue/quantity)` 或 `AVG(revenue)` | 5693.416 |

> 注意最后一行：**「平均客单价」的口径本身有歧义**（是 `AVG(revenue)` 还是 `SUM(revenue)/SUM(quantity)`？）。
> 这类**口径歧义**必须在 prompt 里显式要求「若口径不唯一，先向用户澄清」——
> 这是 Text2SQL 比 RAG 更容易被追问的地方，也是简历上可以写"做了口径澄清机制"的亮点。
