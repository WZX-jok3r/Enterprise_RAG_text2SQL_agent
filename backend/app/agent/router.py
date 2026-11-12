"""router.py - 三层混合路由的第一层：规则前置路由（纯函数，可穷举单测）。

架构定位（见改造方案 5.5.1）：**两道关卡串联，不是一道**
    关卡一（本模块，0 成本）：规则路由 —— 命中即强路由，跳过 LLM 决策
    关卡二（agent/loop.py）：LLM function calling 自主选工具
    关卡三：任何异常/超步数/超时 -> 强制退回纯 RAG

为什么规则层值得单独存在：
    它是一条**纯函数**，输入问题、输出目标，因此可以被**穷举单测**。
    改造方案里那 107 题检索评测集就是它的现成测试用例。
    换句话说 —— **「意图识别」在本项目里是可测试的，不是靠 prompt 祈祷。**

为什么规则层不能单独存在：
    它处理不了"研发薪资和文档带宽比一下"这类需要**同时**查库与查文档的问题。
    那类问题交给关卡二的 LLM 工具选择。

⚠️ 最关键的判定原则：**必须同时命中「聚合词」与「已注册列名词」才强路由 SQL**。
    反例（评测集里真实存在）：「布洛芬最多多久吃一次？」
    它命中聚合词"最多"，但**不是统计题** —— 只命中聚合词就强路由 SQL 会答错。
    这就是规则层必须可单测的根本原因。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, List, Optional, Sequence, Set

logger = logging.getLogger(__name__)


class Route(str, Enum):
    """路由目标。"""

    RAG = "rag"
    SQL = "sql"
    LLM_DECIDE = "llm_decide"     # 交给关卡二（LLM 工具选择）


@dataclass
class RouteDecision:
    route: Route
    reason: str
    # 命中的信号（便于审计与调试，也让测试能断言"为什么这么判"）
    matched_aggregation: List[str] = None      # type: ignore[assignment]
    matched_column: List[str] = None           # type: ignore[assignment]
    matched_doc_word: List[str] = None         # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.matched_aggregation = self.matched_aggregation or []
        self.matched_column = self.matched_column or []
        self.matched_doc_word = self.matched_doc_word or []


# ==================== 词表 ====================

# 聚合/统计意图词
AGGREGATION_WORDS: Set[str] = {
    "多少", "几个", "几万", "几人", "几条", "几笔",
    "最多", "最少", "最高", "最低", "最大", "最小", "最好", "最差",
    "平均", "均值", "总和", "总计", "总共", "一共", "合计", "累计",
    "统计", "排名", "排行", "排序", "占比", "百分比", "环比", "同比",
    "相差", "差值", "差额", "差距", "对比",
    "总计", "汇总", "数量是", "人数是", "金额是",
    "top", "前几", "前3", "前五", "前10",
}

# 文档/非结构化意图词 —— 命中即倾向 RAG
DOC_WORDS: Set[str] = {
    "质保", "保修", "条款", "合同", "政策", "规定", "制度",
    "手册", "说明书", "文档", "文档里", "资料", "规范",
    "如何", "怎么", "怎样", "步骤", "流程", "方法",
    "是什么", "什么是", "介绍", "说明", "解释", "含义", "定义",
    "为什么", "原因", "注意", "建议", "支持", "兼容",
    "规格", "参数", "型号", "接口", "安装", "使用",
    "多久", "多长时间",     # 注意：「最多多久吃一次」这类会命中 AGGREGATION 的"最多"，
                            # 但也会命中"多久" -> DOC_WORDS 优先，避免误路由到 SQL
}

# 已注册业务表的列语义词（与 analytics_tables 的列描述对齐）
# 命中这些词说明问题在问"库里有的字段"，才考虑走 SQL
DEFAULT_COLUMN_WORDS: Set[str] = {
    # employees
    "员工", "姓名", "名字", "部门", "薪资", "工资", "薪水", "薪酬",
    "入职", "工号", "邮箱", "岗位", "职位",
    # 单字兜底：口语问法常用"有多少人""几个人"这种极短表述，
    # 只有"员工/人数"这类双字词会漏判（实测发现："有多少人"匹配不到任何列名词）。
    # 注意加单字会略微放宽规则，因此用"人"这种高置信度的实体名词，不加泛化字。
    "人",
    # sales
    "销售", "营收", "销量", "产品", "订单", "单价",
    # expenses
    "费用", "支出", "开销", "类别",
    # cost_data
    "成本", "橱柜", "项目", "品牌", "型号", "规格", "数量", "单位", "计费",
}


# ==================== 判定 ====================

def _hits(text: str, words: Iterable[str]) -> List[str]:
    t = text.lower()
    return [w for w in words if w.lower() in t]


# ==================== 元问题（询问对话自身）====================
#
# 定义：问的是**这段对话**，而不是知识库或数据表。
#
# 为什么必须单独识别：这类问题会被规则层误判 ——
#     "我上一次问的是什么？" 命中 DOC_WORDS 的"是什么" -> RAG 强路由
#     -> 检索出一堆无关文档（实测 P1 基线：4 条元问题答案都对，
#        但 4/4 都白做了一次 kb_search，且上下文被无关片段稀释）
#
# 判定必须**保守**：误判成 meta 会让一个真实业务问题**跳过检索**，
# 这比多检索一次严重得多（答错 vs 多花一秒）。因此只认
# "我+刚才/上一次+问"、"上面那个问题"、"再问一遍"这几类强句式；
# 故意不收"上次"——它会误伤"上次采购的合同金额是多少"。
META_PATTERNS = (
    re.compile(r"我\s*(?:刚才|刚刚|之前|上一次|上一轮|上一条)\s*(?:问|说|提)"),
    re.compile(r"(?:刚才|刚刚|之前|上一次|上一轮|上面|上一条|上一个)\s*"
               r"(?:那个|这个)?\s*的?\s*(?:问题|提问)"),
    re.compile(r"(?:再|重新)\s*(?:问|说|复述)\s*(?:一遍|一次)"),
    re.compile(r"(?:复述|重复|重述)\s*.{0,6}(?:问题|提问)"),
)


def is_meta_question(question: str) -> bool:
    """是否在询问对话本身（不涉及知识库/数据表）。

    纯函数，不依赖历史 —— 但**是否走 meta 路径还要看调用方**：
    第一轮问"我上一次问的是什么"没有可依的历史，不应跳过检索，
    而应让模型如实说明没有上文（见 AgentLoop.run）。
    """
    q = re.sub(r"\s+", "", question or "")
    return any(p.search(q) for p in META_PATTERNS)


def route_by_rules(
    question: str,
    column_words: Optional[Sequence[str]] = None,
    aggregation_words: Optional[Iterable[str]] = None,
    doc_words: Optional[Iterable[str]] = None,
) -> RouteDecision:
    """规则前置路由。

    Args:
        column_words: 已注册表的列语义词。默认用内置表；
                      **生产应传实际 schema 的列描述词**（表结构变了这里要跟着变）。

    判定顺序（顺序即优先级，很重要）：
        1. 文档意图词命中 **且** 聚合词未同时命中 -> RAG
           （"最多多久吃一次"：命中"最多"也命中"多久" -> DOC 优先，不回 SQL）
        2. 聚合词 **且** 列名词同时命中 -> SQL（强路由）
        3. 仅命中列名词 -> LLM 决策（可能是"查某一行"而非统计，交给模型判断）
        4. 都没命中 -> LLM 决策
    """
    agg = aggregation_words if aggregation_words is not None else AGGREGATION_WORDS
    doc = doc_words if doc_words is not None else DOC_WORDS
    cols = column_words if column_words is not None else DEFAULT_COLUMN_WORDS

    hit_agg = _hits(question, agg)
    hit_col = _hits(question, cols)
    hit_doc = _hits(question, doc)

    # ---- 1) 文档意图优先（但聚合+列名同时命中时例外，交给下面）----
    # 例外条件：既有强聚合信号又有列名 -> 更像统计题（如"各部门有多少人"不含 doc 词）
    if hit_doc and not (hit_agg and hit_col):
        return RouteDecision(
            Route.RAG,
            f"命中文档意图词 {hit_doc[:3]}，且未同时构成统计信号",
            matched_doc_word=hit_doc,
            matched_aggregation=hit_agg,
            matched_column=hit_col,
        )

    # ---- 2) 强路由 SQL：聚合词 与 列名词**同时**命中 ----
    if hit_agg and hit_col:
        return RouteDecision(
            Route.SQL,
            f"同时命中聚合词 {hit_agg[:3]} 与列名词 {hit_col[:3]}",
            matched_aggregation=hit_agg,
            matched_column=hit_col,
        )

    # ---- 3) 仅命中列名：可能是查明细，交 LLM ----
    if hit_col:
        return RouteDecision(
            Route.LLM_DECIDE,
            f"仅命中列名词 {hit_col[:3]}（可能是查明细而非统计），交 LLM 决策",
            matched_column=hit_col,
            matched_aggregation=hit_agg,
        )

    # ---- 4) 无信号：交 LLM ----
    return RouteDecision(
        Route.LLM_DECIDE,
        "未命中任何规则信号，交 LLM 决策",
        matched_aggregation=hit_agg,
        matched_column=hit_col,
    )


def build_column_words(schemas: Sequence[object]) -> List[str]:
    """从 Schema 语义层动态构造列语义词（让规则层随 schema 自动演进）。

    取每列 description 里的中文片段 + 表 display_name。
    这样新增数据表后，规则层无需改代码即可识别新字段。
    """
    import re

    words: Set[str] = set(DEFAULT_COLUMN_WORDS)
    for s in schemas:
        display = getattr(s, "display_name", "") or ""
        for tok in re.findall(r"[\u4e00-\u9fff]{2,}", display):
            words.add(tok)
        for col in getattr(s, "columns", []) or []:
            desc = str(col.get("description") or "") if isinstance(col, dict) else ""
            for tok in re.findall(r"[\u4e00-\u9fff]{2,}", desc):
                words.add(tok)
    return sorted(words)
