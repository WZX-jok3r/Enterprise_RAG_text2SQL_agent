"""fewshot.py - question→SQL 示例库（Text2SQL 准确率性价比最高的手段）。

思路与主流方案一致（Vanna / DB-GPT / CHASE-SQL 都这么做）：
把「自然语言问题 + 正确 SQL」成对存起来，生成新 SQL 前先检索最相似的 3 条
塞进 prompt。这本质上就是一个 RAG —— 只是检索对象从"文档片段"换成了"问答-SQL 对"。

为什么不直接存进 Qdrant（复用现有向量库）：
- 示例库规模很小（几十条），用轻量词面打分即可，省一次网络往返；
- 更重要的是**不触碰现有 collection**，避免给已冻结的检索评测引入变量
  （基线 Recall@5 99.01% 必须保持逐题一致）。
- 接口刻意留成 `retrieve()` 单函数：将来要换 Qdrant 只需替换它，上层不动。

评分策略（中英混排场景）：
  中文按字（bigram + 单字）、英文/数字按词，做 Jaccard 式重叠。
  另加"结构信号"加权：问题里出现聚合词（多少/最多/平均）时，
  优先召回同样含聚合的示例 —— 这类示例对生成 GROUP BY/ORDER BY 最有帮助。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from app.prompts.text2sql import FEWSHOT_EXAMPLES_PATH, FEWSHOT_HEADER

logger = logging.getLogger(__name__)

DEFAULT_EXAMPLES = FEWSHOT_EXAMPLES_PATH

# 聚合意图词（用于结构信号加权 + 供路由层复用）
AGGREGATION_WORDS = {
    "多少", "几个", "几", "最多", "最少", "最高", "最低", "最大", "最小",
    "平均", "总计", "总共", "一共", "合计", "统计", "排名", "排序", "占比",
    "top", "前几", "对比", "相差", "差额", "数量", "总数",
}

# 枚举类词（提示可能是"查某个具体取值"而非聚合）
LOOKUP_WORDS = {"是谁", "哪个", "哪些", "属于", "叫什么", "什么名字", "列出", "查询"}


@dataclass
class FewShotExample:
    question: str
    sql: str
    note: str = ""
    tables: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tables is None:
            self.tables = []


def _tokens(text: str) -> set[str]:
    """中英混排分词：中文取单字 + 相邻 bigram，英文数字取词。"""
    t = text.lower()
    latin = re.findall(r"[a-z0-9_.]+", t)
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", t)

    tokens: set[str] = set(latin)
    for run in cjk_runs:
        tokens.update(run)                                   # 单字
        tokens.update(run[i:i + 2] for i in range(len(run) - 1))  # bigram
    return tokens


def has_aggregation_intent(question: str) -> bool:
    """问题是否含聚合/统计意图。供 few-shot 加权与三层路由的 L1 复用。"""
    q = question.lower()
    return any(w in q for w in AGGREGATION_WORDS)


def load_examples(path: Optional[Path] = None) -> List[FewShotExample]:
    """从 JSONL 加载示例库。文件缺失时返回空列表（不阻断）。"""
    p = path or DEFAULT_EXAMPLES
    if not p.exists():
        logger.warning("[fewshot] 示例库不存在: %s（将退化为无 few-shot）", p)
        return []

    out: List[FewShotExample] = []
    with open(p, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("[fewshot] 第 %d 行不是合法 JSON，跳过: %s", line_no, e)
                continue
            if not obj.get("question") or not obj.get("sql"):
                logger.warning("[fewshot] 第 %d 行缺少 question/sql，跳过", line_no)
                continue
            out.append(FewShotExample(
                question=obj["question"],
                sql=obj["sql"],
                note=obj.get("note", ""),
                tables=obj.get("tables", []) or [],
            ))
    logger.info("[fewshot] 已加载 %d 条示例", len(out))
    return out


def score_example(question: str, ex: FewShotExample) -> float:
    """示例与问题的相关度打分。"""
    q_tokens = _tokens(question)
    e_tokens = _tokens(ex.question)
    if not q_tokens or not e_tokens:
        return 0.0

    overlap = len(q_tokens & e_tokens)
    # 用问题侧覆盖率（而非 Jaccard）：短问题匹配到长示例时不该被惩罚
    base = overlap / max(1, len(q_tokens))

    # 结构信号：聚合意图一致时加权（对生成 GROUP BY 帮助最大）
    if has_aggregation_intent(question) and has_aggregation_intent(ex.question):
        base += 0.35

    # 表名命中：问题直接点名表/实体时强相关
    for t in ex.tables:
        if t and t.lower() in question.lower():
            base += 0.5

    return base


def retrieve(
    question: str,
    examples: Sequence[FewShotExample],
    k: int = 3,
    min_score: float = 0.12,
) -> List[FewShotExample]:
    """召回 top-k 相似示例。低于 min_score 的不返回（宁缺毋滥，避免误导）。"""
    if not examples:
        return []
    scored = [(score_example(question, ex), ex) for ex in examples]
    scored.sort(key=lambda x: -x[0])
    return [ex for s, ex in scored[:k] if s >= min_score]


def render_examples(examples: Sequence[FewShotExample]) -> str:
    """把示例渲染进 prompt。"""
    if not examples:
        return ""
    lines = [FEWSHOT_HEADER]
    for i, ex in enumerate(examples, 1):
        lines.append(f"{i}. 问题：{ex.question}")
        lines.append(f"   SQL：{ex.sql}")
        if ex.note:
            lines.append(f"   说明：{ex.note}")
    return "\n".join(lines)


# ==================== 进程级缓存 ====================

_cache: Optional[List[FewShotExample]] = None


def get_examples() -> List[FewShotExample]:
    global _cache
    if _cache is None:
        _cache = load_examples()
    return _cache
