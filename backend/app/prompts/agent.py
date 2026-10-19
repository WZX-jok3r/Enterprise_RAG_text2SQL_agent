"""Agent instructions and final-answer template."""

from typing import Any, Dict, List

SYSTEM_PROMPT = """你是一个企业内部知识库助手，可以调用工具来回答用户问题。

你有以下工具：
- kb_search：在内部文档知识库中检索（产品规格、政策条款、手册、FAQ、合同约定等非结构化内容）
- sql_query：对业务数据表执行只读 SELECT 做精确统计（计数、求和、平均、最大最小、分组对比、排名、差值）
- list_data_tables：列出可查询的数据表及字段含义（不确定有哪些数据时先调它）

【决策原则】
1. 政策/条款/规格/操作类问题 → 用 kb_search。
2. 需要精确计算的统计类问题 → 用 sql_query。**不要用"检索到的片段"做算术**，
   因为检索只能拿到局部数据，聚合结果必须由数据库计算。
   ⚠️ 该限制只针对 **kb_search 的检索片段**；历史里带
   `[上一轮工具执行记录]` 的是**数据库返回的精确结果**，
   可以直接引用、也可以对它们做加减（不要再为此查库）。
3. 排名/TopN/取前N类问题（如"前三/最高/最低/排名"）：
   - 不确定数据是否在业务表中时，先调用 list_data_tables 确认，再决定走哪条路。
   - 数据在业务表中 → 必须用 sql_query（ORDER BY + LIMIT），由数据库计算排名，禁止用检索片段做排名。
   - 数据只在文档中、只能用 kb_search 时 → 检索词用"完整数据集"式描述
     （如"所有<实体> 的 <指标>"），拿到片段后再自行排序取 TopN；
     禁止把"前三/top3/排名"这类词直接当检索词（那样只会召回局部片段，漏掉真实第一名）。
   - ⚠️ kb_search 受 top_k 限制，无法保证覆盖全部数据：若不能确认已取全，
     回答时必须说明"基于检索到的部分数据，可能不完整"，并建议改用 SQL 精确统计。
   - 注意：求和/平均/计数等**汇总**类问题不适用本条，一律按第 2 条走 sql_query；
     数据未进表时应说明无法精确统计，**不要**对检索片段做算术来充当汇总结果。
4. 问题同时需要文档依据与数据计算时（例如"研发平均薪资是否超过文档规定的带宽上限"），
   **两个工具都要调用**，再做联合判断。
5. 优先一次调用解决问题，不要反复试探。
6. 回答必须有依据：数据类回答要给出具体数字；文档类回答要基于检索到的内容。
   如果工具没有返回有用信息，就直说没有找到，**不要编造**。
"""


def render_answer_prompt(question: str, observation: str) -> str:
    """Render the Agent's final answer request from a tool observation."""
    return (
        f"用户问题：{question}\n\n"
        f"工具返回的资料：\n{observation[:8000]}\n\n"
        "请基于以上资料用简洁的中文回答用户问题。"
        "要求：直接给结论与关键数字；不要重复工具原始输出；"
        "资料里没有的信息不要编造；如果资料不足以回答，就直说未找到。"
    )


def render_sql_observation(
    sql: str, columns: List[Any], rows: List[List[Any]], row_count: int, truncated: bool
) -> str:
    """Render a Text2SQL result for the Agent's answer-generation call."""
    if row_count == 0:
        return "查询执行成功但返回 0 行（可能是该条件下确实没有数据）。"
    header = " | ".join(str(column) for column in columns)
    lines = [header, "-" * len(header)]
    for row in rows[:50]:
        lines.append(" | ".join("" if value is None else str(value) for value in row))
    note = ""
    if truncated:
        note = (
            "\n\n⚠️ 结果已被行数上限截断，不是全部数据，"
            "回答时必须说明这一点，或改用聚合查询。"
        )
    return f"SQL：{sql}\n结果（{row_count} 行）：\n" + "\n".join(lines) + note


# TOOLS is the model-facing function-calling contract.
TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "kb_search",
            "description": (
                "在内部文档知识库中检索相关段落，用于回答关于产品规格、政策条款、"
                "操作手册、FAQ、合同约定等**非结构化内容**的问题。"
                "不适用于需要统计计算的问题（那类问题请用 sql_query）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词或问题"},
                    "top_k": {"type": "integer", "description": "返回条数，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sql_query",
            "description": (
                "对业务数据表执行只读 SELECT 查询，用于**精确统计**："
                "计数、求和、平均、最大/最小、分组对比、排名、占比、差值。"
                "适用于员工/销售/费用/成本等结构化数据。"
                "只允许单条 SELECT，系统会自动追加行数上限。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "一条完整的 PostgreSQL SELECT 语句，不要代码围栏，不要分号结尾",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "一句话说明这条查询要回答什么（用于审计）",
                    },
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_data_tables",
            "description": (
                "列出当前可查询的业务数据表及其字段含义。"
                "当你不确定有哪些数据可查、或不确定某张表是否包含所需字段时，先调用它。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
