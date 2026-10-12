"""
统一配置中心（pydantic-settings）

单一事实来源：合并原 src/config.py 的全部配置项，并新增企业级所需的
PostgreSQL / Redis / CORS 等。保持与现有根 .env 的扁平变量名 100% 兼容
（大小写不敏感），无需改写 .env 即可运行。

env_file 查找顺序（后者覆盖前者，均为绝对路径，避免受启动目录影响）：
    项目根 /.env  ->  backend/.env
容器内两者都不存在时，配置全部来自环境变量（12-factor）。
"""

from functools import lru_cache
from pathlib import Path
from typing import Dict, List

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> parents[2] = backend/，其 parent = 项目根
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = _BACKEND_DIR.parent

# Analytics 只读角色的本地开发默认口令（单一事实来源）。
# 仅用于本机演示；该角色只有 SELECT 权限，爆炸半径限于业务数据。
# 生产环境必须用 ANALYTICS_RO_PASSWORD 环境变量覆盖。
# 为什么本机也必须带口令（实测结论，见 docs/经验教训.md L-004）：
#   Docker 端口发布走 NAT，宿主机的 127.0.0.1 在容器侧被改写为网桥网关地址，
#   因此 pg_hba.conf 里 `host all all 127.0.0.1/32 trust` 对宿主机连接永不生效，
#   实际匹配末行 scram-sha-256 —— 必须提供口令。
DEV_ANALYTICS_RO_PASSWORD = "kb_ro_dev_pw"


class Settings(BaseSettings):
    """全局配置。字段名与 .env 变量大小写不敏感匹配。"""

    model_config = SettingsConfigDict(
        env_file=(str(_PROJECT_ROOT / ".env"), str(_BACKEND_DIR / ".env")),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ==================== 应用 ====================
    app_name: str = "Enterprise RAG"
    environment: str = "development"          # development / staging / production
    debug: bool = False
    performance_log_file: str = ""  # Opt-in numeric JSONL ledger, preferably under output/.
    log_level: str = "INFO"
    # 日志格式：text（默认，人读友好）| json（结构化单行，便于日志采集器解析）
    log_format: str = "text"
    host: str = "0.0.0.0"
    port: int = 8000

    # ==================== 可观测（P7）====================
    # Prometheus 指标端点开关（GET /metrics，Prometheus 文本格式）
    metrics_enabled: bool = True

    # ==================== Qdrant 向量库 ====================
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_collection_name: str = "personal_rag_v2"

    # ==================== SiliconFlow（LLM + Embedding）====================
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    vector_dim: int = 1024
    llm_model: str = "deepseek-ai/DeepSeek-V3.2"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 4096

    # ==================== Chunk ====================
    default_chunk_size: int = 512
    default_chunk_overlap: int = 50

    # ==================== 文档解析 ====================
    pdf_subdir: str = "pdf_examples"          # 知识库内 PDF 示例子目录名
    use_marker_for_pdf: bool = True           # 无线表格 PDF 三级提取失败时用 marker 转 md（未装则优雅降级）

    # ==================== 检索（dense + BM25 混合 + RRF）====================
    top_k: int = 5
    retrieval_mode: str = "hybrid"            # vector | hybrid
    prefetch_k: int = 20
    hybrid_tokenize: bool = True              # jieba 预分词，未装自动降级
    index_filename_prefix: bool = True        # 入库时块文本头部注入文档名（BM25/rerank 对称可见，问题点名文件名时可精确命中；改动需全量重灌生效）

    # ==================== Rerank 精排 ====================
    rerank_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_candidates: int = 20

    # ==================== 低相关过滤（仅对 rerank 绝对分生效）====================
    score_rel_ratio: float = 0.15
    score_abs_min: float = 0.05
    score_confident: float = 0.5
    score_min_keep: int = 1
    score_drop_all_below: bool = False

    # ==================== 鉴权与多租户 ====================
    tenant_field: str = "tenant_id"
    rag_tenant_keys: str = ""                 # 格式: tenant_id=api_key;tenant2=key2

    # ---- RBAC（P7）----
    # 为什么 Text2SQL 上线后必须补 RBAC：
    #   纯 RAG 时代，能检索到什么取决于"文档里写了什么"；
    #   接入 SQL 之后，"能查到什么"变成可枚举的数据权限问题 ——
    #   例如"全公司薪资最高的员工是谁"这类查询，普通员工不该能问。
    #   这把租户隔离问题升级成了**合规问题**，必须显式建模。
    #
    # 角色定义：
    #   analyst   —— 数据分析角色：可看全部列（默认角色，与改造前行为一致）
    #   employee  —— 普通员工：**敏感列自动脱敏**
    # 向后兼容：默认角色即 analyst + 敏感列清单为空 ⇒ 与改造前行为完全一致。
    default_role: str = "analyst"
    # 按租户指定角色：格式 tenant_id=role;tenant2=role2
    # （同一租户内可再细分时，可扩展为 api_key 维度）
    rag_tenant_roles: str = ""
    # 敏感列清单：逗号分隔的 "表.列"；非 analyst 角色的查询结果会被脱敏。
    # 例：employees.salary,employees.email
    sql_redact_columns: str = "employees.salary,employees.email"

    # ---- SQL 审计（P7）----
    # 是否把每次 SQL 执行落库审计。为什么值得做：
    #   ① 合规要求"谁在什么时候查了敏感数据"；② 出问题时能复盘；
    #   ③ Agent 场景下模型可能生成意外查询，审计是唯一的追溯手段。
    sql_audit_enabled: bool = True

    # ==================== Langfuse 可观测（软依赖，默认关闭）====================
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # ==================== 会话 ====================
    session_ttl_seconds: int = 3600
    max_history_messages: int = 20

    # ==================== PostgreSQL（P3 起启用）====================
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "rag"
    postgres_password: str = "rag"
    postgres_db: str = "rag"
    postgres_pool_size: int = 20
    postgres_max_overflow: int = 10

    # ==================== Analytics 只读库（Text2SQL，P1 起启用）====================
    # 设计：业务数据放独立库 kb_analytics，由独立只读角色 kb_ro 访问，
    # 与系统元数据库 rag 物理隔离 —— LLM 生成的 SQL 只能碰业务表，
    # 永远碰不到 messages（会话内容）/ tenants（租户表）。
    # 见 docs/text2sql-数据层设计.md 第一、二节。
    analytics_db: str = "kb_analytics"
    analytics_ro_user: str = "kb_ro"
    # 只读角色口令。留空 = 不设口令（本地 docker 映射到 127.0.0.1，pg_hba 为 trust，
    # 可直接连接）；生产环境务必通过环境变量注入强口令。
    analytics_ro_password: str = ""
    # Restricted SQL uses a separate login, never SET ROLE from the analyst login.
    analytics_employee_user: str = "kb_employee"
    analytics_employee_password: str = ""

    # ---- Text2SQL 执行护栏（安全网的第二层，见设计文档第五节）----
    # 单次查询返回行数硬上限：超限截断，并在结果里标注 truncated=true
    sql_max_rows: int = 200
    # 单条 SQL 的语句级超时（毫秒）：设在连接上，保证 PG 侧真的停下
    sql_timeout_ms: int = 5000
    # 结果集超过该行数时，不再把明细塞进 prompt，改为只给"统计概要"
    # 理由：LLM 无法预知结果集大小，上千行明细会直接炸掉上下文窗口
    sql_inline_max_rows: int = 50
    # LLM 生成 SQL 失败后，允许带错误信息重写的最多次数
    sql_max_retry: int = 2
    # 入库时是否把表格类文件（xlsx）同步成 SQL 表，实现 RAG + Text2SQL 双通道。
    # 设计文档要求「复用异步入库链路把表格转表做成 ARQ 任务」；
    # 关闭它则只走 RAG（xlsx 不会出现在 SQL 侧），便于对照与排障。
    analytics_sync_on_ingest: bool = True

    # ==================== Redis（缓存 / 会话 / ARQ 队列，P3+P4 起启用）====================
    redis_url: str = "redis://localhost:6379/0"
    arq_queue_name: str = "rag_ingest"

    # ==================== CORS ====================
    # 逗号分隔，便于 .env 直接书写；用 allowed_origins 属性取列表
    cors_origins: str = "http://localhost:5173,http://localhost:3000,http://localhost:8080"

    # ==================== 派生属性 ====================
    @property
    def base_dir(self) -> Path:
        return _PROJECT_ROOT

    @property
    def knowledge_base_dir(self) -> Path:
        return _PROJECT_ROOT / "knowledge_base"

    @property
    def processed_cache_file(self) -> Path:
        return _PROJECT_ROOT / "processed_cache" / "cache.json"

    @property
    def test_dataset_file(self) -> Path:
        return _PROJECT_ROOT / "test_dataset" / "qa_test_v2.jsonl"

    @property
    def allowed_origins(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def tenant_keys(self) -> Dict[str, str]:
        """解析 RAG_TENANT_KEYS -> {api_key: tenant_id}。留空则鉴权关闭。"""
        mapping: Dict[str, str] = {}
        for part in self.rag_tenant_keys.replace("\n", "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            tenant_id, api_key = part.split("=", 1)
            tenant_id, api_key = tenant_id.strip(), api_key.strip()
            if tenant_id and api_key:
                mapping[api_key] = tenant_id
        return mapping

    @property
    def auth_enabled(self) -> bool:
        return bool(self.tenant_keys)

    @property
    def tenant_roles(self) -> Dict[str, str]:
        """解析 RAG_TENANT_ROLES -> {tenant_id: role}。留空则所有租户用 default_role。"""
        mapping: Dict[str, str] = {}
        for part in self.rag_tenant_roles.replace("\n", "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            tenant_id, role = part.split("=", 1)
            tenant_id, role = tenant_id.strip(), role.strip()
            if tenant_id and role:
                mapping[tenant_id] = role
        return mapping

    @property
    def redact_column_set(self) -> set:
        """解析 SQL_REDACT_COLUMNS -> {"employees.salary", ...}（统一小写比较）。"""
        out = set()
        for part in self.sql_redact_columns.replace("\n", "").split(","):
            p = part.strip().lower()
            if p and "." in p:
                out.add(p)
        return out

    @property
    def role_for(self) -> Dict[str, str]:
        """向后兼容别名（便于阅读）：租户 -> 角色 的映射。"""
        return self.tenant_roles

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def postgres_url(self) -> str:
        """async SQLAlchemy 连接串（asyncpg 驱动）。"""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def sync_postgres_url(self) -> str:
        """Alembic 迁移用的同步连接串（psycopg 驱动）。"""
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def analytics_url(self) -> str:
        """Analytics 只读库连接串（**asyncpg** 驱动，用只读角色）。

        用途：需要异步访问业务库时使用（配合 create_async_engine）。
        ⚠️ 不能配 create_engine()（同步）—— 会报 MissingGreenlet。
           同步场景请用 `sync_analytics_ro_url`。
        """
        auth = self.analytics_ro_user
        if self.analytics_ro_password:
            auth = f"{auth}:{self.analytics_ro_password}"
        return (
            f"postgresql+asyncpg://{auth}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.analytics_db}"
        )

    @property
    def effective_ro_password(self) -> str:
        """只读角色的实际口令：配置优先，否则用本地开发默认值。"""
        return self.analytics_ro_password or DEV_ANALYTICS_RO_PASSWORD

    @property
    def effective_employee_password(self) -> str:
        return self.analytics_employee_password or "kb_employee_dev_pw"

    @property
    def sync_analytics_employee_url(self):
        from sqlalchemy.engine import URL
        return URL.create(
            "postgresql+psycopg", username=self.analytics_employee_user,
            password=self.effective_employee_password,
            host=self.postgres_host, port=self.postgres_port, database=self.analytics_db,
        )

    def validate_security(self) -> None:
        """Production must never silently fall back to anonymous/privileged access."""
        if self.environment.strip().lower() not in {"production", "staging"}:
            return
        if not self.auth_enabled:
            raise ValueError("Production/staging requires RAG_TENANT_KEYS")
        if self.default_role != "employee":
            raise ValueError("Production/staging requires DEFAULT_ROLE=employee")
        if not self.analytics_ro_password or not self.analytics_employee_password:
            raise ValueError("Production/staging requires both analytics SQL passwords")
        if not self.redact_column_set:
            raise ValueError("Production/staging requires SQL_REDACT_COLUMNS")
        if self.analytics_employee_user in {self.analytics_ro_user, self.postgres_user}:
            raise ValueError("Restricted SQL must use a separate database login")
        for tenant in self.tenant_keys.values():
            if tenant in {"@anonymous@", "@unowned@"}:
                raise ValueError("Reserved tenant identity in RAG_TENANT_KEYS")
        if self.default_role not in {"employee", "analyst"} or any(
            role not in {"employee", "analyst"} for role in self.tenant_roles.values()
        ):
            raise ValueError("Unknown RBAC role")

    @property
    def sync_analytics_url(self) -> str:
        """建表 / 装载用的同步连接串（psycopg 驱动，用可写账号）。

        注意：这里刻意用 postgres_user（可写）而不是 kb_ro ——
        ETL 建表需要 DDL 权限，而 kb_ro 只应有 SELECT。
        """
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.analytics_db}"
        )

    @property
    def sync_analytics_ro_url(self) -> str:
        """只读执行器的**同步**连接串（psycopg 驱动 + kb_ro 只读角色）。

        为什么需要它：SqlExecutor 目前是同步的（与本项目 rag 内核的同步风格一致，
        由线程池桥接到异步路由）。而 `analytics_url` 用的是 asyncpg 驱动，
        只能配 create_async_engine —— 用 create_engine() 会报
        MissingGreenlet（greenlet_spawn has not been called）。
        故这里单独提供同步版本，驱动与角色都对齐执行器的需要。
        """
        auth = self.analytics_ro_user
        # 与 setup_db 使用同一来源，避免两处默认值漂移
        pwd = self.effective_ro_password
        auth = f"{auth}:{pwd}"
        return (
            f"postgresql+psycopg://{auth}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.analytics_db}"
        )

    @property
    def sync_maintenance_url(self) -> str:
        """维护连接串：连到默认库（rag），用于 CREATE DATABASE 等不能在本库内做的事。"""
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def supported_extensions(self) -> set[str]:
        return {".pdf", ".docx", ".xlsx", ".md", ".txt"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
