"""Text2SQL generation, retry, and result-summary prompts."""

from pathlib import Path
from typing import Any, Dict, List


FEWSHOT_EXAMPLES_PATH = Path(__file__).resolve().parent / "data" / "fewshot_examples.jsonl"
FEWSHOT_HEADER = "以下是相似问题的正确 SQL 写法（请模仿其风格与口径）："


SYSTEM_PROMPT = """你是一个严谨的数据分析助手，负责把用户的统计类问题翻译成 PostgreSQL 查询。

【硬性规则】
1. 只能使用下面「可用数据表」里出现过的表和列名，**绝对不要编造列名**。
2. 只允许生成**单条 SELECT** 语句；禁止任何写操作（INSERT/UPDATE/DELETE/DROP/CREATE 等）。
3. 字符串比较请严格使用列出的**取值原文**（区分大小写）。
4. 「最多/最少/最高/最低」类问题用 ORDER BY + LIMIT 1，不要用 MAX/MIN 去套分组计数。
5. 对聚合结果过滤用 HAVING，不要用 WHERE。
6. 「每组取最大/最新的一条」用窗口函数 row_number() OVER (PARTITION BY ... ORDER BY ...)。
7. 日期区间用半开区间（>= 起点 AND < 终点），便于走索引。
8. 如果问题的统计口径不唯一，**必须调用 ask_clarification**，不要猜一个口径就查。

【可用数据表】
{schema}

{fewshot}
"""

RESTRICTED_SCHEMA_INSTRUCTION = (
    "\n当前身份权限受限：只查询数据字典列出的字段，禁止 SELECT *、整行转换和敏感字段的派生查询。"
)


def render_summary_prompt(
    question: str, sql: str, row_count: int, table: str, truncated: bool
) -> str:
    """Render the question and verified SQL result for answer synthesis."""
    return (
        f"用户问题：{question}\n\n"
        f"已执行的 SQL：\n{sql}\n\n"
        f"查询结果（{row_count} 行）：\n{table}\n\n"
        + ("注意：结果被行数上限截断，不是全部数据，回答时必须说明这一点。\n"
           if truncated else "")
        + "请基于以上真实查询结果，用简洁的中文回答用户问题。"
          "直接给结论和关键数字，不要重复 SQL，不要编造结果里没有的数据。"
    )


def build_retry_messages(bad_sql: str, error: str) -> list[dict[str, Any]]:
    """Show the attempted SQL and execution error before asking for a rewrite."""
    return [
        {"role": "assistant", "content": f"我尝试的 SQL 是：\n{bad_sql}"},
        {
            "role": "user",
            "content": (
                f"这条 SQL 没有通过校验或执行失败，错误信息如下：\n{error}\n\n"
                f"请修正后重新生成一条可执行的 SELECT。"
                f"特别注意：只使用 schema 里列出的表和列名，不要编造列名。"
            ),
        },
    ]


def render_executor_observation(
    *,
    ok: bool,
    observation: str,
    redacted_columns: List[str],
    row_count: int,
    columns: List[Any],
    elapsed_ms: int,
    rows: List[List[Any]],
    truncated: bool,
    inline_max_rows: int,
) -> str:
    """Render SQL results and safety instructions for the Agent tool context."""
    if not ok:
        return observation

    prefix = ""
    if redacted_columns:
        cols = "、".join(redacted_columns)
        prefix = (
            f"⚠️ 权限说明：列 [{cols}] 属敏感数据，当前账号权限不足已脱敏"
            f"（显示为 ***）。**不要猜测或编造这些列的具体数值**；"
            f"若用户问的正是这些数据，请说明需要更高权限。\n\n"
        )

    if row_count == 0:
        return prefix + (
            "查询执行成功，但返回 0 行。注意：0 行不等于出错 —— "
            "它可能是「该条件下确实没有数据」。"
            "若你认为应有数据，请检查筛选条件（尤其是字符串大小写与日期范围）后重试。"
        )

    header = " | ".join(str(column) for column in columns)
    truncation_note = ""
    if truncated:
        truncation_note = (
            f"\n\n⚠️ 结果已被 LIMIT 截断（仅返回前 {row_count} 行，并非全部数据）。"
            f"**不要声称这是完整结果**；如需全貌请改写为聚合查询"
            f"（COUNT/SUM/AVG/MAX/MIN/GROUP BY）。"
        )

    if row_count <= inline_max_rows:
        lines = [header, "-" * len(header)]
        for row in rows:
            lines.append(" | ".join("" if value is None else str(value) for value in row))
        body = "\n".join(lines)
        return prefix + f"查询返回 {row_count} 行（耗时 {elapsed_ms}ms）：\n{body}{truncation_note}"

    lines = [header, "-" * len(header)]
    for row in rows[:5]:
        lines.append(" | ".join("" if value is None else str(value) for value in row))
    body = "\n".join(lines)
    return prefix + (
        f"查询返回 **{row_count} 行**（耗时 {elapsed_ms}ms），"
        f"结果集较大，此处只给前 5 行样例：\n{body}\n\n"
        f"⚠️ 明细未全部提供。请基于以下方式作答之一：\n"
        f"  1) 若用户要的是统计结论，请改写为聚合查询（COUNT/SUM/AVG/MAX/MIN/GROUP BY）；\n"
        f"  2) 若用户确实要明细，请说明只展示了前 5 行，并建议加筛选条件或限定数量。\n"
        f"  **不要**把上面的样例当成完整数据来汇报。"
        f"{truncation_note}"
    )


# SQL_TOOLS is the model-facing function-calling contract.
SQL_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "sql_query",
            "description": (
                "对业务数据表执行一条 PostgreSQL 只读 SELECT 查询来回答统计类问题。"
                "适用于计数、求和、平均、最大/最小、分组对比、排名、占比。"
                "只允许单条 SELECT，系统会自动追加行数上限。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "一条完整的 PostgreSQL SELECT 语句，不要包含 markdown 代码围栏，不要以分号结尾",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "一句话说明这条查询要回答什么，用于审计",
                    },
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": (
                "当问题的统计口径不唯一、无法确定该用哪个指标或哪种算法时，"
                "用它向用户反问，而不是自行猜测口径。"
                "例如「平均客单价」可能指 平均每笔营收 或 总营收/总销量。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "要向用户澄清的具体问题，给出候选口径",
                    }
                },
                "required": ["question"],
            },
        },
    },
]
