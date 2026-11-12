-- ============================================================================
-- 001_init.sql - Analytics 只读库的权限基座（幂等）
--
-- 执行者：可写账号（settings.postgres_user）连到 kb_analytics 库
-- 作用域：**不需要超级用户**。CREATE ROLE 需要 CREATEROLE，本项目 rag 角色已有。
--   （CREATE DATABASE 不能在目标库内执行，由 setup_db.py 单独处理）
--
-- 设计要点（见 docs/text2sql-数据层设计.md 第二节）：
--   安全是纵深防御，本文件负责「第三层：数据库角色权限」。
--   任何一层单独都不够：
--     ① 应用层 AST 白名单  -> 挡越权语句，但 sqlglot 官方自称不是 validator
--     ② 只读事务 + 超时     -> 挡漏网写操作与 DoS，但官方承认"不阻止所有落盘写"
--     ③ 角色权限（本文件）  -> 挡写企图，但挡不住"用合法 SELECT 读敏感数据"
--     ④ RLS 行级隔离        -> 挡跨租户越权读
-- ============================================================================

-- ---- 1) 只读角色：无 DDL、无写权限 ----
-- 用 DO 块做幂等（CREATE ROLE 没有 IF NOT EXISTS 语法）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_ro') THEN
        CREATE ROLE kb_ro WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
        RAISE NOTICE '[analytics] 已创建只读角色 kb_ro';
    ELSE
        RAISE NOTICE '[analytics] 只读角色 kb_ro 已存在，跳过创建';
    END IF;
END
$$;

-- ---- 2) 收敛角色属性（每次执行都强制对齐，防止被误改）----
ALTER ROLE kb_ro NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS NOREPLICATION;

-- ---- 3) 只连接与 schema 可见性 ----
GRANT CONNECT ON DATABASE kb_analytics TO kb_ro;
GRANT USAGE ON SCHEMA public TO kb_ro;

-- ---- 4) 只授 SELECT：现有表 + 未来新建表 ----
GRANT SELECT ON ALL TABLES IN SCHEMA public TO kb_ro;
-- ALTER DEFAULT PRIVILEGES 只影响「之后由当前角色创建的对象」。
-- 这是最容易漏的一步：不加它，新表默认对 kb_ro 不可见。
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO kb_ro;

-- 显式回收：确保 kb_ro 没有任何写权限（防御性，幂等）
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON ALL TABLES IN SCHEMA public FROM kb_ro;
REVOKE CREATE ON SCHEMA public FROM kb_ro;

-- ---- 5) 角色级兜底：会话默认只读 + 语句超时 ----
-- 角色级设置比应用层设置更硬：应用代码写错也不会覆盖它
ALTER ROLE kb_ro SET default_transaction_read_only = on;
ALTER ROLE kb_ro SET statement_timeout = '5s';
ALTER ROLE kb_ro SET idle_in_transaction_session_timeout = '10s';
-- 防止只读角色被用于放大查询：限制并行度（避免单条 SQL 打满 CPU）
ALTER ROLE kb_ro SET max_parallel_workers_per_gather = 0;
