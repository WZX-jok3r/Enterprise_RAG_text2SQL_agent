"""analytics - Text2SQL 数据层与分析通道（运行态）。

职责边界（与既有 RAG 通道完全解耦）：
- 本包只负责「结构化数据的建表、装载、语义描述、只读执行」；
- 不碰 loader / chunking / vector / qdrant 任何代码（RAG 通道一行不改）；
- 依赖方向单向：analytics -> core(config)。core 不反向依赖 analytics。
- 建库建角色、备份、对账、恢复、灌数据 CLI 等**运维命令在 `app/ops/`**，
  本包不再包含任何 `python -m app.analytics.*` 入口。

模块：
    ddl.py         : 建业务表 / 元数据表（幂等）
    schema_infer.py: 从 xlsx 推断类型与表头（纯函数，可单测）
    seed.py        : 从 knowledge_base 的 xlsx 装载数据（ETL 引擎 + worker 入口）
    schema.py      : 生成给 LLM 看的「数据字典」
    guard.py       : SQL AST 白名单校验（P2）
    executor.py    : 只读执行 + 超时 + 上限（P2）

安全设计见 docs/text2sql-数据层设计.md。
"""

__all__ = [
    "ddl",
    "schema_infer",
    "seed",
    "schema",
]
