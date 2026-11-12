"""rewrite.py - 多轮追问的检索词改写（P1-2）。

## 要解决的问题（评测集里的真实失败）

强制 SQL / 强制 RAG 两条路径把**用户原话**直接当检索词：

    setup : Sales 部门有多少人？                        -> 1042
    追问  : 那 Engineering 呢？两个部门合起来一共多少人？
            规则层按"聚合词 + 列名词"强路由到 SQL，但 Text2SQL 拿到的是一个
            **缺主语**的句子 -> 模型反过来要求澄清指代（基线 mt-sql-01/05/06）。

RAG 侧同理：指代对象只存在于**上一轮答案**里（mt-rag-01 打到 V1.0 的片段，
把 12 个月答成 24 个月）。

## 只服务强制路由的两条路径

llm_decide 路径（关卡二）本来就能看到历史（见 loop._history_messages），
模型会自己把"那 Engineering 呢"补全成合理的工具参数 —— 实测能答对。
在那条路径上再改写一遍 = 多一次 LLM 调用换零收益。

## 三条硬约束

1. **不占 MAX_LLM_CALLS**：独立用 `rewrite_calls` 计数。改写是"为了省 token
   而花的 token"，若它挤占成本配额，预算紧时会把**答案生成**的配额吃掉 ——
   省了一次检索、丢了一次回答。
2. **fail-open**：超时 / 异常 / 输出不像一句话，一律退回原问题。
   改写失败绝不能变成回答失败（与"异常降级到纯 RAG"同一取向）。
3. **只改检索词，不改回答对象**：最终答案模板仍用用户原话（state.question），
   否则用户会看到自己在问"那 Engineering 呢"，答案却在复述一句被改写过的长问句。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Sequence, Tuple

logger = logging.getLogger(__name__)

# 上一轮上下文的字符上限。改写只需要"上一轮在说哪个对象"，
# 不需要完整答案（长答案既推高 token 成本，也不提升改写质量）。
MAX_CONTEXT_CHARS = 600

# 指代词：命中即认为"这句话离开上文读不懂"。
# ⚠️ 刻意不收"同 / 该 / 其"这类会混进普通词的单字
#    （"合同"含"同"、"应该"含"该"），否则闸门会在真实业务问题上误开。
REFER_WORDS = (
    "那", "呢", "两个", "三个", "这几个", "这两个", "它们",
    "这个", "那个", "这些", "那些", "它", "上面", "上述", "前面",
    "刚才", "上一", "还有", "再",
)

# 短句多半是追问（"那 Engineering 呢？""哪两个部门？"）。
SHORT_QUESTION_CHARS = 12

REWRITE_SYSTEM = """你是检索词改写器。把用户这一轮的问题改写成一句**可以独立理解**的问题。

规则：
1. 只把指代（"那两个部门""它""上面说的那个版本"）补全为上一轮里出现过的具体对象。
2. 完整保留用户原意与全部限定条件（时间范围、指标、比较方式）。
3. 不要回答问题，不要解释，不要加引号、序号或前缀。
4. 若原问题本身已经完整（不依赖上文），**原样输出**。
5. 只输出改写后的那一句话。"""

_PREFIXES = ("改写后的问题：", "改写后的独立问题：", "改写后：", "改写：", "问题：", "结果：")


def render_rewrite_prompt(context: str, question: str) -> str:
    """组装改写的 user 消息（历史摘要在前，本轮问题在后）。"""
    return (
        f"上一轮对话：\n{context}\n\n"
        f"这一轮的问题：{question}\n\n"
        "改写后的独立问题："
    )


def last_round(history: Sequence[Dict[str, Any]]) -> str:
    """最近一轮对话（含上一轮的工具轨迹），截断到 MAX_CONTEXT_CHARS。

    为什么带 tool_context：追问里的指代（"这两个部门"）往往指向**上一轮算出来的
    数字**，而那个数字只存在于工具轨迹里 —— 自然语言答案里可能只写了一个。
    """
    last_user = -1
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "user" and history[i].get("content"):
            last_user = i
            break
    if last_user < 0:
        return ""

    parts: List[str] = [f"用户：{history[last_user]['content']}"]
    for h in history[last_user + 1:]:
        if h.get("role") != "assistant" or not h.get("content"):
            continue
        parts.append(f"助手：{h['content']}")
        trace = h.get("tool_context")
        if trace:
            parts.append(f"[上一轮工具记录]\n{trace}")
    return "\n".join(parts)[:MAX_CONTEXT_CHARS]


def needs_rewrite(question: str, history: Sequence[Dict[str, Any]]) -> bool:
    """是否需要改写（纯函数，可穷举单测）。

    闸门存在的唯一理由是省钱 —— 改写本身是一次 LLM 调用。
    因此判据刻意偏宽松：**漏判**会让追问回到"要求澄清指代"的老毛病，
    **误判**只是多一次几十 token 的调用（且提示词要求模型对完整问题原样返回）。
    """
    if not history:
        return False        # 首轮没有上文可补，改写必然是空转
    q = (question or "").strip()
    if not q:
        return False
    if len(q) <= SHORT_QUESTION_CHARS:
        return True
    return any(w in q for w in REFER_WORDS)


def _clean(raw: str) -> str:
    """把模型的输出收敛成"一句话"。

    实测（与 text2sql 的澄清识别同源问题）：模型时不时会带上
    "改写后的问题：" 前缀、代码围栏，或者干脆多写一行解释。
    这些都直接当检索词会污染召回，因此只取首个有效行并剥掉常见前缀/引号。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    # 先剔除围栏行（```text / ```），否则会把语言名当成检索词
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("```")]
    if not lines:
        return ""
    text = lines[0].strip("`")
    for prefix in _PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text.strip().strip("「」『』\"'“”《》").strip()


def rewrite_query(
    question: str,
    history: Sequence[Dict[str, Any]],
    llm: Any,
    *,
    timeout_s: float = 8.0,
    max_tokens: int = 64,
) -> Tuple[str, str]:
    """把追问改写成可独立理解的问题。

    Returns:
        (query, reason)，reason 只用于观测：
            rewritten  改写生效（query 可直接当检索词）
            same       模型判断原问题已完整
            invalid    输出不像一句话（含解释 / 过长 / 为空）
            error      调用失败或超时

    ⚠️ **任何分支都返回可用的 query**（失败时就是原问题）——
       调用方不需要判断 reason 就能继续跑。
    """
    context = last_round(history)
    if not context or not question:
        return question, "skip"

    messages = [
        {"role": "system", "content": REWRITE_SYSTEM},
        {"role": "user", "content": render_rewrite_prompt(context, question)},
    ]
    try:
        chat = llm.chat_with_tools(
            messages, tools=None, temperature=0.0,
            max_tokens=max_tokens, timeout=max(1, int(round(timeout_s))),
            scene="query_rewrite",
        )
    except Exception as e:  # noqa: BLE001
        # 超时 / 上游 5xx / 参数不被接受的假实现，都在这里落地为"按原文检索"
        logger.warning("[agent] 追问改写失败(%s)，按原问题检索: %s", type(e).__name__, e)
        return question, "error"

    text = _clean(getattr(chat, "content", "") or "")
    if not text:
        return question, "invalid"
    # 模型偶尔把"原问题已经完整，无需改写。"这类解释写出来 ——
    # 比原问题长得多，当检索词只会污染召回。
    if len(text) > max(60, len(question) * 3):
        logger.info("[agent] 改写输出过长，按原问题检索: %s", text[:80])
        return question, "invalid"
    if re.sub(r"\s+", "", text) == re.sub(r"\s+", "", question):
        return question, "same"
    return text, "rewritten"
