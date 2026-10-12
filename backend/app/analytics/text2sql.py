"""text2sql.py - Text2SQL 引擎（自然语言 -> SQL -> 执行 -> 自然语言答案）。

链路（与主流方案一致：schema/示例检索 -> 生成 -> 执行反馈修正）：

    问题
     ├─ 1. Schema Linking：按问题裁剪相关表（schema.select_relevant_schema_text）
     ├─ 2. Few-shot 检索：召回相似 question-SQL 示例（fewshot.retrieve）
     ├─ 3. 口径歧义检查：易歧义指标 + 问题未给口径 -> 反问（不猜）
     ├─ 4. LLM 生成 SQL（function calling，含"执行"与"澄清"两个工具）
     ├─ 5. guard 校验（AST 白名单 + LIMIT 注入）
     ├─ 6. EXPLAIN 预检（不执行，提前发现列名/类型错误）
     ├─ 7. 执行（只读事务 + 超时 + 结果上限）
     ├─ 8. 失败则把**数据库报错原样回灌**让 LLM 重写（最多 sql_max_retry 次）
     └─ 9. 结果 -> 自然语言答案（并回传 SQL 与结果表，保证可解释）

为什么把"数据库报错"当作一等公民：
    错误信息（如 `column "dept" does not exist`）是**最高质量的修正信号** ——
    它精确指出了模型错在哪，比任何 prompt 提示都直接。
    实测中"带错误重写"显著提升成功率。

为什么要有口径歧义检查：
    ✅ 已实测：DeepSeek-V3.2 面对「平均客单价是多少」会主动反问
    "需要先澄清'客单价'的统计口径"。把它产品化，避免模型自行猜一个口径
    然后给出一个"看起来对但口径错"的数字 —— 那正是本项目要消灭的静默错误。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.analytics import fewshot as fs
from app.analytics import schema as schema_mod
from app.analytics.executor import QueryResult, SqlExecutor, get_executor
from app.analytics.guard import guard_and_record
from app.core.config import settings
from app.llm.tools import ToolCall, get_tool_llm_client

logger = logging.getLogger(__name__)

# 生成 SQL 的工具定义。**两个工具而不是一个**：
#   sql_query —— 正常生成
#   ask_clarification —— 口径不明时反问，而不是猜
# 让"反问"成为一个显式动作（而不是靠模型自由发挥），
# 这样它可以被统计、被评测、被前端专门渲染。
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

# 易歧义指标：命中即要求模型先澄清口径（可在元数据/配置中扩展）。
#
# ⚠️ 设计教训（实测后收缩，见 docs/经验教训.md L-011）：
#   初版把「平均薪资」一律视为歧义，结果把「Sales 部门的平均薪资是多少？」
#   和「全公司平均薪资是多少？」这类**问题本身已把口径说清楚**的题也拦下了 ——
#   过度澄清会把正常问题变成答不了，比不澄清更糟。
#   因此只保留**真的会导致两个不同数字**的口径分歧：
#     「所有部门平均薪资的平均」 vs 「按人头平均」会给出不同结果；
#     「平均每笔营收」 vs 「总营收/总销量」也会。
#   而"公司整体平均薪资"只有一种自然解读，不该追问。
AMBIGUOUS_METRICS = {
    "客单价": ["平均每笔营收 avg(revenue)", "总营收/总销量 sum(revenue)/sum(quantity)"],
    "增长率": ["同比", "环比"],
    "转化率": ["按订单数", "按人数"],
    "总额": ["含税", "不含税"],
}

# 「平均薪资」类：仅在**未限定范围**时才歧义（按人头平均 vs 各部门平均再平均）。
AVG_SALARY_METRICS = ("平均薪资", "平均工资", "薪资平均")
# 已限定范围的信号词：出现即说明用户说清了口径，不再追问。
#
# ⚠️ 这里刻意收得很宽（宁可漏问也不要误问）：
#   只要问题提到"部门"或"团队"等分组维度，口径就是明确的
#   ——「Sales 部门的平均薪资」只有一种自然解读。
#   实测教训（docs/经验教训.md L-011）：初版只列了"各部门/每个部门"等精确短语，
#   结果「Sales 部门的平均薪资」被误判为歧义并反问，把能答的问题变成答不了。
SCOPE_QUALIFIERS = (
    # 分组维度：出现任一即视为已限定范围
    "部门", "团队", "岗位", "职位", "地区", "城市",
    # 明确表示"整体"的说法
    "全公司", "公司整体", "整体", "所有人", "所有员工", "全体员工", "全员",
    "按人头", "总体", "总的", "一共", "全部",
    # 明确的口径词
    "口径", "按", "分别", "每个", "各",
)

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


@dataclass
class Text2SqlResult:
    """Text2SQL 的完整结果（同时服务 API 与 Agent 工具两种调用方）。"""

    ok: bool
    question: str
    sql: str = ""
    columns: List[str] = field(default_factory=list)
    rows: List[List[Any]] = field(default_factory=list)
    row_count: int = 0
    elapsed_ms: int = 0
    truncated: bool = False
    answer: str = ""                 # 自然语言答案（给用户）
    needs_clarification: bool = False
    clarification: str = ""          # 反问内容
    attempts: int = 0                # 生成尝试次数（含自修正）
    errors: List[str] = field(default_factory=list)   # 每轮的错误（审计用）
    usage: Dict[str, Any] = field(default_factory=dict)
    # RBAC：因权限不足被脱敏的列（需透传给前端，否则用户以为看到了全部）
    redacted_columns: List[str] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        """给前端/SSE 的紧凑结构。"""
        return {
            "ok": self.ok,
            "question": self.question,
            "sql": self.sql,
            "columns": self.columns,
            "row_count": self.row_count,
            "elapsed_ms": self.elapsed_ms,
            "truncated": self.truncated,
            "needs_clarification": self.needs_clarification,
            "clarification": self.clarification,
            "attempts": self.attempts,
            "redacted_columns": self.redacted_columns,
        }


class Text2SqlEngine:
    """把自然语言问题转成 SQL 并执行。"""

    def __init__(
        self,
        executor: Optional[SqlExecutor] = None,
        llm: Optional[Any] = None,
    ) -> None:
        self.executor = executor or get_executor()
        self.llm = llm or get_tool_llm_client()
        self.max_retry = settings.sql_max_retry

    # ---- 口径歧义检测（规则前置：不依赖模型自觉）----
    def detect_ambiguity(self, question: str, history: Optional[List[Dict[str, str]]] = None) -> Optional[str]:
        """若问题命中易歧义指标，返回应澄清的问题；否则 None。

        判定顺序：
          1. 强歧义指标（客单价/增长率/…）—— 命中即问（除非已自带口径词）
          2. 「平均薪资」类 —— **仅在未限定范围时**才问
             （"Sales 部门的平均薪资"只有一种解读，不该被拦）
          3. 已澄清过（历史里出现过该指标与候选口径）则不再重复追问
        """
        hist_text = " ".join(m.get("content", "") for m in (history or []))

        def already_clarified(metric: str, options: List[str]) -> bool:
            return metric in hist_text and any(o in hist_text for o in options)

        # 1) 强歧义指标
        for metric, options in AMBIGUOUS_METRICS.items():
            if metric not in question:
                continue
            if any(k in question for k in ("按", "口径", "分别", "每笔", "平均每")):
                continue
            if already_clarified(metric, options):
                continue
            opts = "；".join(f"{i}) {o}" for i, o in enumerate(options, 1))
            return f"「{metric}」的统计口径不唯一，请确认你要的是哪一种：{opts}？"

        # 2) 「平均薪资」类：已限定范围则不追问
        if any(m in question for m in AVG_SALARY_METRICS):
            if not any(q in question for q in SCOPE_QUALIFIERS):
                options = [
                    "按人头平均（全体员工薪资总和 / 员工数）",
                    "各分组平均值再平均（先按部门平均，再对部门取平均）",
                ]
                if not already_clarified("平均薪资", options):
                    opts = "；".join(f"{i}) {o}" for i, o in enumerate(options, 1))
                    return f"「平均薪资」的口径不唯一，请确认你要的是哪一种：{opts}？"

        return None

    # ---- 主入口 ----
    def answer(
        self,
        question: str,
        session: Session,
        tenant_id: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        max_tables: int = 4,
        redact: Optional[frozenset] = None,
        actor: str = "",
    ) -> Text2SqlResult:
        """完整链路。session 用于读取 schema 元数据（rag 库）。

        Args:
            redact: RBAC 需脱敏的列集合（来自 Principal.redact_columns()）
            actor: 审计标识（形如 "tenant:role"）
        """
        result = Text2SqlResult(ok=False, question=question)

        # ---- 1. 口径歧义前置检查 ----
        ambiguous = self.detect_ambiguity(question, history)
        if ambiguous:
            result.needs_clarification = True
            result.clarification = ambiguous
            result.answer = ambiguous
            return result

        # ---- 2. Schema Linking ----
        schemas = schema_mod.load_table_schemas(session, tenant_id=tenant_id)
        schemas = schema_mod.filter_sensitive_columns(schemas, redact)
        if not schemas:
            result.answer = "当前没有可查询的数据表，请先在「数据表」中导入结构化数据。"
            return result
        schema_text = schema_mod.select_relevant_schema_text(question, schemas, max_tables=max_tables)

        # ---- 3. Few-shot 检索 ----
        examples = fs.retrieve(question, fs.get_examples(), k=3)
        if redact:
            from app.analytics.permissions import sensitive_sql_reason
            examples = [e for e in examples if not sensitive_sql_reason(e.sql, redact)]
        fewshot_text = fs.render_examples(examples)

        # ---- 4. 生成 + 执行 + 自修正循环 ----
        system = SYSTEM_PROMPT.format(schema=schema_text, fewshot=fewshot_text)
        if redact:
            system += "\n当前身份权限受限：只查询数据字典列出的字段，禁止 SELECT *、整行转换和敏感字段的派生查询。"
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ]

        last_error = ""
        for attempt in range(1, self.max_retry + 2):     # 首次 + 最多 max_retry 次重写
            result.attempts = attempt
            try:
                chat = self.llm.chat_with_tools(
                    messages, tools=SQL_TOOLS, temperature=0.0,
                    scene="text2sql_generate", actor=actor, tenant_id=tenant_id,
                )
            except Exception as e:  # noqa: BLE001
                logger.error("[text2sql] LLM 调用失败: %s", e)
                result.errors.append(str(e))
                result.answer = f"生成查询时调用模型失败：{str(e)[:200]}"
                return result

            if chat.usage:
                result.usage = chat.usage

            # 模型选择反问
            clarify_call = _find_call(chat.tool_calls, "ask_clarification")
            if clarify_call is not None:
                args = clarify_call.parse_args()
                result.needs_clarification = True
                result.clarification = args.get("question", "").strip() or "请补充统计口径。"
                result.answer = result.clarification
                return result

            sql_call = _find_call(chat.tool_calls, "sql_query")
            if sql_call is None:
                # 模型没调工具，只回了文本。两种情况必须区分：
                #   ① 它在**反问澄清**（即使我们给了 ask_clarification 工具，
                #      实测模型有时仍直接用自由文本发问 —— 见下方 _looks_like_clarification）
                #   ② 它认为问题不需要查库
                # 若不区分，同一个澄清行为会时而被标成 clarify、时而被标成普通答案，
                # 前端呈现与统计口径都不一致（实测复现率约 1/3）。
                if _looks_like_clarification(chat.content):
                    result.needs_clarification = True
                    result.clarification = chat.content.strip()
                    result.answer = result.clarification
                    return result
                result.answer = chat.content or "无法为这个问题生成数据查询。"
                return result

            raw_sql = sql_call.parse_args().get("sql", "")

            # ---- 5. guard 校验 ----
            g = guard_and_record(raw_sql, settings.sql_max_rows)
            if not g.ok:
                last_error = g.user_message or g.reason
                result.errors.append(f"guard 拒绝: {g.reason}")
                logger.info("[text2sql] guard 拒绝(第%d次): %s", attempt, g.reason)
                messages = _append_retry(messages, raw_sql, last_error)
                continue

            result.sql = g.sql

            # ---- 6+7. 执行（内部含 EXPLAIN 预检 + RBAC 脱敏 + 审计）----
            qr: QueryResult = self.executor.execute(
                g.sql, tenant_id=tenant_id, redact=redact, actor=actor,
            )
            if not qr.ok:
                last_error = qr.error
                result.errors.append(f"执行失败: {qr.error[:300]}")
                logger.info("[text2sql] 执行失败(第%d次): %s", attempt, qr.error[:200])
                messages = _append_retry(messages, raw_sql, qr.observation)
                continue

            # ---- 成功 ----
            result.ok = True
            result.columns = qr.columns
            result.rows = qr.rows
            result.row_count = qr.row_count
            result.elapsed_ms = qr.elapsed_ms
            result.truncated = qr.truncated
            result.redacted_columns = list(qr.redacted_columns or [])
            return result

        # 重试用尽
        result.answer = (
            f"未能生成可执行的查询（已尝试 {result.attempts} 次）。\n"
            f"最后一次错误：{last_error[:300]}"
        )
        return result

    # ---- 结果 -> 自然语言（供 API 与 Agent 复用）----
    def summarize(self, result: Text2SqlResult, question: str) -> str:
        """把查询结果转成自然语言答案。

        刻意**把数据事实交给 LLM，但把 SQL 与结果表原样回传前端** ——
        可解释性不能依赖 LLM 转述。
        """
        if result.needs_clarification:
            return result.answer
        if not result.ok:
            return result.answer

        if result.row_count == 0:
            return "查询执行成功，但没有符合条件的数据。"

        # 小结果集：交给 LLM 组织语言
        table = _render_table(result.columns, result.rows[:20])
        prompt = (
            f"用户问题：{question}\n\n"
            f"已执行的 SQL：\n{result.sql}\n\n"
            f"查询结果（{result.row_count} 行）：\n{table}\n\n"
            + ("注意：结果被行数上限截断，不是全部数据，回答时必须说明这一点。\n"
               if result.truncated else "")
            + "请基于以上真实查询结果，用简洁的中文回答用户问题。"
              "直接给结论和关键数字，不要重复 SQL，不要编造结果里没有的数据。"
        )
        try:
            chat = self.llm.chat_with_tools(
                [{"role": "user", "content": prompt}], tools=None, temperature=0.2,
                scene="text2sql_summarize",
            )
            if chat.usage:
                result.usage = chat.usage
            if chat.content:
                return chat.content
        except Exception as e:  # noqa: BLE001
            logger.warning("[text2sql] 结果摘要生成失败，回退为表格: %s", e)

        return f"查询结果（{result.row_count} 行）：\n{table}"


# ==================== 辅助 ====================

def _find_call(calls: List[ToolCall], name: str) -> Optional[ToolCall]:
    for c in calls:
        if c.name == name:
            return c
    return None


def _looks_like_clarification(content: str) -> bool:
    """判断模型的自由文本回复是不是在"反问澄清口径"。

    为什么需要这个启发式（实测驱动）：
        我们给了 `ask_clarification` 工具，但实测模型有约 1/3 的概率
        **不调工具**、直接用自由文本列出候选口径反问。
        若不识别这种形态，同一个澄清行为会时而走 clarify 事件、
        时而变成普通答案，前端口径不一致。

    判据（**全部满足**才算，避免把正常答案误判为反问）：
        1. 含疑问语气（问号 或 "请确认/请澄清/请说明/哪一种/是指"）
        2. 出现**多个候选项**（编号列表 "1." / "2." / "1)"）或"或"/"还是"这类择一连接
        3. 文本较短（< 400 字）—— 真正口径澄清不会长篇大论

    保守设计：宁可漏判（当成普通答案），也不要把正常回答误判成反问
    —— 因为误判会让用户收到一个"其实不需要回答的反问"。
    """
    if not content:
        return False
    text = content.strip()
    if len(text) > 400:
        return False

    has_question_tone = (
        "？" in text or "?" in text
        or any(w in text for w in ("请确认", "请澄清", "请说明", "哪一种", "哪一类",
                                   "是指", "需要先确认", "具体是什么意思"))
    )
    if not has_question_tone:
        return False

    # 多个候选项：编号列表或择一连接词
    option_count = 0
    for marker in ("1.", "2.", "3.", "1)", "2)", "3)", "1、", "2、", "3、", "①", "②"):
        if marker in text:
            option_count += 1
    has_choice = option_count >= 2 or ("还是" in text) or ("或者" in text)

    return has_choice


def _append_retry(messages: List[Dict[str, Any]], bad_sql: str, error: str) -> List[Dict[str, Any]]:
    """把失败的 SQL 与数据库错误回灌，要求模型重写。

    用 assistant/user 交替而非直接塞 system：
    这样模型能看到"我刚才写了什么"与"数据库怎么报错的"之间的因果关系。
    """
    return messages + [
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


def _render_table(columns: List[str], rows: List[List[Any]]) -> str:
    if not columns:
        return "(无列)"
    header = " | ".join(str(c) for c in columns)
    sep = "-" * len(header)
    lines = [header, sep]
    for r in rows:
        lines.append(" | ".join("" if v is None else str(v) for v in r))
    return "\n".join(lines)


_engine: Optional[Text2SqlEngine] = None


def get_text2sql_engine() -> Text2SqlEngine:
    global _engine
    if _engine is None:
        _engine = Text2SqlEngine()
    return _engine
