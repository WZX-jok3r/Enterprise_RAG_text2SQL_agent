"""可观测层（Langfuse 埋点 + LLM 用量记账）。

对外统一从包导入：``from app.observability import langfuse as obs``，
或 ``from app.observability import span, generation, trace``。
用量记账按子模块直接导入：``from app.observability.usage import record_usage, summarize_usage``。
"""

from app.observability.langfuse import (
    enabled,
    extract_usage,
    flush,
    generation,
    span,
    trace,
)

__all__ = ["enabled", "extract_usage", "flush", "generation", "span", "trace"]
