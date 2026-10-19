"""loop.py - 有界状态机 Agent Loop（手写，不引入框架）。

## 为什么手写（改造方案 4.5 / 5.4）
只有 3 个工具、循环上界明确、无多 Agent 协作、无人在回路审批；
且必须复用既有 SSE 契约与 hermetic 测试体系（45 例单测不真连外部服务）。
引入图编排框架 = 用 1000 行抽象解决 300 行问题，并让调试链路变长。
切换边界：多 Agent 协作 / HITL 审批 -> LangGraph；复杂结构化输出 -> Pydantic AI。

## 五重防死循环（面试必问，也是生产必须）
    1. MAX_STEPS = 5          步数硬上界（**状态机而非 while True**）
    2. WALL_CLOCK = 60s       墙钟超时（防单步慢）
    3. MAX_LLM_CALLS = 8      **成本上界**（框架默认往往没有，但生产必须有）
    4. 相同工具+相同参数去重   连续两次相同调用判为循环，立即跳出
    5. 任一步失败 -> **降级到纯 RAG**，而不是把错误抛给用户（保证可用性不倒退）

## 降级必须"原样走 RAG"
降级时若只是"调 kb_search 然后生成答案"，前端拿不到标准 `meta` 帧，
来源列表与「已隐藏 N 条」徽标会**永久空着**（见 events.py 说明）。
因此 `kb_search` 一旦执行，本模块**必定补发 meta 帧**。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from app.agent import events as ev
from app.agent.router import Route, RouteDecision, route_by_rules
from app.agent.tools import AgentTools, ToolOutcome
from app.core.config import settings
from app.prompts.agent import (
    SYSTEM_PROMPT,
    TOOLS,
    render_answer_prompt,
    render_sql_observation,
)

logger = logging.getLogger(__name__)

# ---- 上界（五重保护的前三重）----
MAX_STEPS = 5
WALL_CLOCK_SECONDS = 60.0
MAX_LLM_CALLS = 8

# 每轮写入 tool_context 的结果行数上限。
# 刻意只留少量：tool_context 会进入**每一轮**的 prompt，
# 全量塞进去会让长对话的 token 成本随轮数线性膨胀。
# 追问通常只需要"上一轮算出来的那几个数"，不需要明细。
MAX_TRACE_ROWS = 5

# ⚠️ 这里曾有一段更详细的"【多轮追问的处理】"提示（约 500 字），
#    说明历史里的工具轨迹是精确结果、可以直接做算术、指代默认指上一轮对象等。
#    实测 A/B（各 5 次真实多轮对话，看第 2 轮能否答出 1042+1010=2052）：
#       带该提示块   : 2/5
#        不带该提示块 : 2/5
#    **没有证据表明它有效**，而它会让每次 LLM 调用多约 500 token。
#    因此删除，只保留上面第 2 条里那句最短的澄清。
#
#    剩余的失败模式是模型**要求澄清指代**（"您指的是哪个部门？"），
#    提示词层面压不住 —— 这是模型行为，不是能靠 prompt 修的缺陷。
#    机制本身（轨迹确实进入 prompt）已由
#    tests/test_agent_multiturn.py 用确定性断言锁住。


def _fmt_cell(v: Any) -> str:
    """把结果单元格渲染成单行短文本（供 tool_context）。"""
    if v is None:
        return "NULL"
    s = str(v)
    return s if len(s) <= 40 else s[:37] + "..."


@dataclass
class AgentState:
    """Agent 运行状态（显式状态机，便于断言与调试）。

    `redact` / `actor` 放在这里而不是逐个方法传参：
    它们属于"本次调用的身份权限"，每个方法都可能需要，
    逐层传参会污染 8 个方法签名（且容易漏传导致**权限静默失效**）。
    """

    question: str
    history: List[Dict[str, str]] = field(default_factory=list)
    steps: int = 0
    llm_calls: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    seen_calls: set = field(default_factory=set)   # 去重键
    final_answer: Optional[str] = None
    degraded: bool = False
    degraded_reason: str = ""
    route: Optional[RouteDecision] = None
    # RBAC / 审计：本次调用的身份权限
    redact: Optional[frozenset] = None
    actor: str = ""
    # 本轮的工具轨迹（供**下一轮**追问延续上下文）。
    # 只记"对追问有用"的东西：执行过的 SQL + 结果行（含列名）。
    # llm_context 由 _finalize 渲染成紧凑文本，随 assistant 消息入库。
    executed_sql: List[str] = field(default_factory=list)
    sql_result_rows: List[List[Any]] = field(default_factory=list)
    sql_result_columns: List[str] = field(default_factory=list)
    last_outcome: Optional[ToolOutcome] = None
    forced_observation: Optional[ToolOutcome] = None
    pending_meta: Optional[Dict[str, Any]] = None


class AgentLoop:
    """有界 Agent 状态机。"""

    def __init__(
        self,
        tools: Optional[AgentTools] = None,
        llm: Optional[Any] = None,
        max_steps: int = MAX_STEPS,
        wall_clock: float = WALL_CLOCK_SECONDS,
        max_llm_calls: int = MAX_LLM_CALLS,
    ) -> None:
        self.tools = tools or AgentTools()
        self._llm = llm
        self.max_steps = max_steps
        self.wall_clock = wall_clock
        self.max_llm_calls = max_llm_calls

    @property
    def llm(self):
        if self._llm is None:
            from app.llm.tools import get_tool_llm_client
            self._llm = get_tool_llm_client()
        return self._llm

    # ==================== 主入口 ====================

    def run(
        self,
        question: str,
        tenant_id: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        forced_route: Optional[Route] = None,
        redact: Optional[frozenset] = None,
        actor: str = "",
    ) -> Iterator[Dict[str, Any]]:
        """执行 Agent，逐事件 yield（可直接转 SSE）。

        Args:
            forced_route: 由调用方（API 层）预先算好的规则路由结果。不传则内部计算。
            redact: RBAC 需脱敏的列集合（来自 Principal.redact_columns()）
            actor: 审计标识（形如 "tenant:role"）
        """
        state = AgentState(
            question=question, history=list(history or []),
            redact=redact, actor=actor,
        )
        deadline = time.monotonic() + self.wall_clock
        t_start = time.monotonic()

        # ---- 关卡一：规则前置路由 ----
        decision = state.route = (
            RouteDecision(forced_route, "调用方指定") if forced_route
            else route_by_rules(question)
        )
        target = "sql" if decision.route == Route.SQL else (
            "rag" if decision.route == Route.RAG else "auto"
        )
        yield ev.ev_route(target, decision.reason)

        # ---- 强制路由：跳过 LLM 决策，直接调对应工具 ----
        if decision.route == Route.SQL:
            yield from self._run_forced_sql(state, tenant_id, deadline)
            yield from self._finalize(state, tenant_id, t_start)
            return
        if decision.route == Route.RAG:
            yield from self._run_forced_rag(state, tenant_id, deadline)
            yield from self._finalize(state, tenant_id, t_start)
            return

        # ---- 关卡二：LLM 工具选择循环（有界）----
        yield from self._run_llm_loop(state, tenant_id, deadline)
        yield from self._finalize(state, tenant_id, t_start)

    # ==================== 三条执行路径 ====================

    def _run_forced_sql(self, state: AgentState, tenant_id: Optional[str],
                        deadline: float) -> Iterator[Dict[str, Any]]:
        """规则强路由到 SQL：直接执行，省掉一次 LLM 决策（0 成本、低延迟）。"""
        call_id = f"forced-sql-{state.steps}"
        args = {"query": state.question}
        yield ev.ev_tool_call(call_id, "list_data_tables", {}, purpose="确认可用数据表")
        listing = self._exec_tool(state, "list_data_tables", {}, tenant_id, deadline)
        if listing is not None:
            # 记进 state.tool_calls：它是"实际工作量"的度量口径，
            # 漏记会让 rag_agent_tool_calls 少算一次（_exec_tool 内部只记 outcome，
            # 由调用方负责累计到 state）。
            state.tool_calls.append({"name": "list_data_tables", "ok": True})
            yield listing

        # 用 Text2SQL 引擎生成并执行（它内部含 schema linking + few-shot + 自修正）
        yield ev.ev_tool_call(call_id, "sql_query", args, purpose=state.question)
        outcome = self._exec_sql_via_engine(state, tenant_id)
        yield ev.ev_tool_result(call_id, outcome.ok, outcome.summary,
                                **{k: v for k, v in outcome.payload.items()
                                   if k in ("row_count", "truncated", "elapsed_ms")})
        if outcome.payload.get("sql"):
            yield ev.ev_sql(
                outcome.payload["sql"],
                int(outcome.payload.get("row_count", 0)),
                int(outcome.payload.get("elapsed_ms", 0)),
                truncated=bool(outcome.payload.get("truncated")),
            )
        if outcome.payload.get("needs_clarification"):
            yield ev.ev_clarify(outcome.payload.get("clarification", ""))
        state.tool_calls.append({"name": "sql_query", "ok": outcome.ok})
        # 强制路由同样要记轨迹：追问很可能接在这条路径的结果上
        # （实测：第 1 轮走强制 SQL 得 1042，第 2 轮追问时正需要它）
        if outcome.ok:
            sql = outcome.payload.get("sql")
            rows = outcome.payload.get("rows") or []
            if sql and rows:
                state.executed_sql.append(str(sql))
                state.sql_result_columns = list(outcome.payload.get("columns") or [])
                state.sql_result_rows = [list(r) for r in rows[:MAX_TRACE_ROWS]]
        # 结果存下来供 _finalize 生成答案
        state.forced_observation = outcome

    def _run_forced_rag(self, state: AgentState, tenant_id: Optional[str],
                        deadline: float) -> Iterator[Dict[str, Any]]:
        """规则强路由到 RAG：直接检索 + 生成（复用既有链路能力）。"""
        call_id = f"forced-rag-{state.steps}"
        yield ev.ev_tool_call(call_id, "kb_search", {"query": state.question}, purpose="知识库检索")
        outcome = self._exec_tool(state, "kb_search", {"query": state.question}, tenant_id, deadline)
        if outcome is not None:
            yield outcome
        state.forced_observation = state.last_outcome

    @staticmethod
    def build_messages(state: AgentState) -> List[Dict[str, Any]]:
        """把「系统提示 + 历史（含工具轨迹）+ 本轮问题」组装成 LLM messages。

        抽成独立静态方法有两个好处：
          1. 可以直接单测"轨迹有没有进 prompt"，不必凑一个会走 LLM 循环的问题
             （很多问题会被规则强路由到 SQL，根本不走 LLM 循环 —— 实测踩到）；
          2. 注入逻辑只有一处，避免将来又出现"某条路径忘了带上下文"。
        """
        messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for h in state.history[-6:]:
            role = h.get("role", "user")
            content = h.get("content", "")
            # 把上一轮的**工具轨迹**附在 assistant 消息里一起给模型。
            #
            # ⚠️ 实测的多轮缺陷：原先只给 role/content（问题 + 自然语言答案），
            #    模型看不到上一轮执行过什么查询、拿到过哪些行，只能重新推导。
            #    实测：第 1 轮答 Sales=1042；第 2 轮问"那 Engineering 呢？
            #    两个部门合起来多少人？" -> 只答出 1010，合计**算不出来**
            #    （自称"资料未找到"），而 1042 就在它的上下文里。
            #
            # 用"系统注记"的措辞而不是伪造 tool 消息：
            # 伪造 `role=tool` 需要配套的 tool_call_id 与上游 tool_calls 消息，
            # 而会话存储里并没有完整的那一串；措辞成注记更诚实，
            # 也不会因缺少配对 id 被上游 API 拒绝。
            if role == "assistant" and h.get("tool_context"):
                content = (f"{content}\n\n"
                           f"[上一轮工具执行记录，供你延续上下文]\n{h['tool_context']}")
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": state.question})
        return messages

    def _run_llm_loop(self, state: AgentState, tenant_id: Optional[str],
                      deadline: float) -> Iterator[Dict[str, Any]]:
        """LLM 自主工具选择循环（有界）。"""
        messages = self.build_messages(state)

        while state.steps < self.max_steps:
            # 三重上界检查
            if time.monotonic() > deadline:
                state.degraded, state.degraded_reason = True, "wall_clock"
                yield ev.ev_degraded("wall_clock", message="处理超时，已切换为直接检索回答")
                yield from self._run_forced_rag(state, tenant_id, deadline)
                return
            if state.llm_calls >= self.max_llm_calls:
                state.degraded, state.degraded_reason = True, "max_llm_calls"
                yield ev.ev_degraded("max_llm_calls", message="模型调用次数达上限")
                yield from self._run_forced_rag(state, tenant_id, deadline)
                return

            state.steps += 1
            state.llm_calls += 1

            try:
                chat = self.llm.chat_with_tools(messages, tools=TOOLS, temperature=0.1)
            except Exception as e:  # noqa: BLE001
                logger.error("[agent] LLM 调用失败: %s", e)
                state.degraded, state.degraded_reason = True, "llm_error"
                yield ev.ev_degraded("llm_error", message=str(e)[:200])
                yield from self._run_forced_rag(state, tenant_id, deadline)
                return

            # 无工具调用 -> 模型直接给答案，结束循环
            if not chat.tool_calls:
                state.final_answer = chat.content or ""
                return

            # 执行工具（可能多个）
            made_progress = False
            for tc in chat.tool_calls:
                args = tc.parse_args()

                # ---- 保护 4：相同工具 + 相同参数去重 ----
                key = f"{tc.name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
                if key in state.seen_calls:
                    logger.info("[agent] 检测到重复工具调用，判定为循环: %s", key[:120])
                    state.degraded, state.degraded_reason = True, "repeated_tool_call"
                    yield ev.ev_degraded("repeated_tool_call",
                                         message="检测到重复调用，已停止循环")
                    if not state.final_answer:
                        yield from self._run_forced_rag(state, tenant_id, deadline)
                    return
                state.seen_calls.add(key)

                yield ev.ev_tool_call(tc.id or f"call-{state.steps}", tc.name, args,
                                      purpose=str(args.get("purpose", "")))

                evt = self._exec_tool(state, tc.name, args, tenant_id, deadline)
                if evt is not None:
                    outcome = state.last_outcome
                    yield evt
                    made_progress = made_progress or outcome.ok
                    if tc.name == "sql_query" and outcome.payload.get("sql"):
                        yield ev.ev_sql(
                            outcome.payload["sql"],
                            int(outcome.payload.get("row_count", 0)),
                            int(outcome.payload.get("elapsed_ms", 0)),
                            truncated=bool(outcome.payload.get("truncated")),
                        )
                    # 工具结果回灌给模型，继续下一轮
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": tc.id or f"call-{state.steps}",
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }],
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id or f"call-{state.steps}",
                        "content": outcome.observation[:8000],   # 防御性截断，避免撑爆上下文
                    })

            if not made_progress:
                logger.info("[agent] 所有工具均失败，降级到 RAG")
                state.degraded, state.degraded_reason = True, "all_tools_failed"
                yield ev.ev_degraded("all_tools_failed", message="数据查询未成功，改用文档检索")
                yield from self._run_forced_rag(state, tenant_id, deadline)
                return

        # 步数用尽
        state.degraded, state.degraded_reason = True, "max_steps"
        yield ev.ev_degraded("max_steps", message="已达最大步数，直接作答")

    # ==================== 工具执行包装 ====================

    def _exec_tool(self, state: AgentState, name: str, args: Dict[str, Any],
                   tenant_id: Optional[str], deadline: float) -> Optional[Dict[str, Any]]:
        """执行工具并产出 tool_result 事件。结果同时存到 state.last_outcome。"""
        outcome = self.tools.dispatch(
            name, args, tenant_id=tenant_id,
            redact=state.redact, actor=state.actor,
        )
        state.last_outcome = outcome
        state.tool_calls.append({"name": name, "ok": outcome.ok})

        # ⚠️ kb_search 一旦执行，必须补发标准 meta 帧（否则前端来源列表与
        #    「已隐藏 N 条」徽标会永久空着 —— 用户会以为功能退化了）。
        #    帧先暂存，由 _finalize 在 delta/done 之前统一发出。
        payload_evt: Dict[str, Any] = {}
        if name == "kb_search" and outcome.ok:
            p = outcome.payload
            state.pending_meta = ev.ev_meta(
                p.get("sources", []), int(p.get("retrieved_count", 0)), int(p.get("dropped", 0))
            )

        return ev.ev_tool_result(
            f"{name}-{state.steps}", outcome.ok, outcome.summary, **payload_evt,
        )

    def _exec_sql_via_engine(self, state: AgentState, tenant_id: Optional[str]) -> ToolOutcome:
        """用 Text2SQL 引擎处理自然语言问题（含 schema linking + few-shot + 自修正）。

        注意：通过 `tools.sql_answer()` 调用，而不是自己去开 session ——
        这样 Loop 只依赖工具契约，测试可注入纯假实现（不碰数据库）。
        """
        try:
            if not hasattr(self.tools, "sql_answer"):
                raise AttributeError("工具集未实现 sql_answer（无法走自然语言 SQL 路径）")
            res = self.tools.sql_answer(
                state.question, tenant_id=tenant_id,
                redact=state.redact, actor=state.actor,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[agent] text2sql 失败: %s", e)
            return ToolOutcome(ok=False, name="sql_query",
                               observation=f"数据查询失败：{str(e)[:200]}",
                               summary="数据查询失败")

        payload: Dict[str, Any] = {
            "sql": res.sql,
            "columns": res.columns,
            "rows": res.rows,
            "row_count": res.row_count,
            "elapsed_ms": res.elapsed_ms,
            "truncated": res.truncated,
            "needs_clarification": res.needs_clarification,
            "clarification": res.clarification,
            "attempts": res.attempts,
        }
        if res.needs_clarification:
            return ToolOutcome(
                ok=True, name="sql_query",
                observation=f"需要向用户澄清口径：{res.clarification}",
                summary="需要澄清统计口径", payload=payload,
            )
        if not res.ok:
            return ToolOutcome(
                ok=False, name="sql_query",
                observation=res.answer or "查询失败",
                summary="数据查询未成功",
                payload=payload,
            )
        # 记入本轮工具轨迹（供下一轮追问延续上下文）——只记**成功且有结果**的查询
        if res.rows:
            state.executed_sql.append(res.sql)
            state.sql_result_columns = list(res.columns)
            state.sql_result_rows = [list(r) for r in res.rows[:MAX_TRACE_ROWS]]
        return ToolOutcome(
            ok=True, name="sql_query",
            observation=self._render_sql_observation(res),
            summary=f"查询返回 {res.row_count} 行（{res.elapsed_ms}ms）"
                    + ("［已截断］" if res.truncated else ""),
            payload=payload,
        )

    @staticmethod
    def _render_sql_observation(res: Any) -> str:
        """把 SQL 结果渲染成给 LLM 的观察文本。"""
        return render_sql_observation(
            res.sql, res.columns, res.rows, res.row_count, res.truncated
        )

    # ==================== 收尾：生成最终答案并补发 meta ====================

    def _finalize(self, state: AgentState, tenant_id: Optional[str],
                  t_start: float) -> Iterator[Dict[str, Any]]:
        """产出最终答案。**先补发 meta 帧**（若有检索），再 delta + done。"""
        # 补发 meta（kb_search 曾执行过）—— 保证前端来源列表与徽标正常
        if state.pending_meta:
            yield state.pending_meta
            state.pending_meta = None

        if state.final_answer is None:
            state.final_answer = self._compose_answer(state)

        answer = state.final_answer or "抱歉，未能找到相关信息。"
        # 逐段 yield delta 便于前端保持既有渲染路径
        yield ev.ev_delta(answer)
        stats = {
            "steps": state.steps,
            "llm_calls": state.llm_calls,
            "tool_calls": len(state.tool_calls),
            "degraded": state.degraded,
            "degraded_reason": state.degraded_reason,
            "elapsed_ms": int((time.monotonic() - t_start) * 1000),
        }
        self._record_metrics(state)
        # 暴露给调用方（API 层要把轨迹随 assistant 消息落库，
        # 供下一轮追问延续上下文；见 build_tool_context 的说明）
        yield ev.ev_done(answer, agent_stats=stats,
                         _tool_context=self.build_tool_context(state))

    @staticmethod
    def build_tool_context(state: AgentState) -> str:
        """把本轮工具轨迹渲染成**给下一轮模型看**的紧凑文本。

        ## 为什么需要它（实测的多轮缺陷）

        原先会话只存 `role + content`（问题 + 自然语言答案），追问时模型
        看不到上一轮执行过什么查询、拿到过哪些行，只能重新推导。
        实测（真实 LLM）：

            第 1 轮「Sales 部门有多少人？」           -> 1042
            第 2 轮「那 Engineering 呢？两个部门合起来一共多少人？」
                  -> 只答出 Engineering=1010，**合计算不出来**
                     （自称"两颗部门合起来的人数资料未找到"）

        而 1042 就在它的上下文里 —— 缺的不是"数字"，
        而是"这些数字是怎么算出来的、还可以怎么用"。

        ## 设计取舍

        - **只记 SQL 与结果行**，不记 prompt/tool 原始报文（体积与敏感性）
        - 行数上限 `MAX_TRACE_ROWS`：该文本会进入**每一轮** prompt，
          全量塞入会让 token 成本随轮数线性膨胀
        - 返回空串表示"本轮没有可延续的查询"，调用方据此不写该字段
        """
        if not state.executed_sql and not state.sql_result_rows:
            return ""
        parts: List[str] = []
        if state.executed_sql:
            # 只留最后一条：追问通常接着最近一次查询
            parts.append(f"已执行 SQL: {state.executed_sql[-1]}")
        if state.sql_result_rows:
            cols = state.sql_result_columns or [
                f"col_{i + 1}" for i in range(len(state.sql_result_rows[0]))
            ]
            parts.append("列: " + ", ".join(str(c) for c in cols))
            for r in state.sql_result_rows:
                parts.append("  " + " | ".join(_fmt_cell(v) for v in r))
        return "\n".join(parts)

    def _record_metrics(self, state: AgentState) -> None:
        """把本次运行写入 Prometheus 指标。

        记录三类信息：
          - `route` / `degraded`：各通道占比 + **降级率**（可用性告警核心指标）
          - `rag_agent_steps`：LLM 工具选择循环的轮次（成本相关）
          - `rag_agent_tool_calls`：实际工具调用次数（**真实工作量**）

        ⚠️ 为什么必须同时记 tool_calls（实测发现的缺口）：
            `steps` 只在 `_run_llm_loop` 里自增，而**规则强路由路径不进入那个循环**
            —— 于是"哪个部门人数最多"这类被 router 直接判定为 SQL 的问题，
            `steps` 恒为 0，可它实际调用了 list_data_tables + sql_query 两次工具。
            只看 steps 会得出"Agent 什么都没做"的错误结论。
            两个指标一起看才有意义：steps=0 且 tool_calls=2 ⇒ 规则路由生效（好事）。

        指标写入失败不影响主流程。
        """
        try:
            from app.core.metrics import counter_inc, observe

            route = state.route.route.value if state.route else "unknown"
            counter_inc("rag_agent_runs_total",
                        (route, "true" if state.degraded else "false"))
            observe("rag_agent_steps", float(state.steps))
            observe("rag_agent_tool_calls", float(len(state.tool_calls)))
        except Exception:  # noqa: BLE001
            pass

    def _compose_answer(self, state: AgentState) -> str:
        """用模型把工具观察组织成自然语言答案。"""
        obs = state.forced_observation
        if obs is not None and not obs.ok:
            return obs.observation or "未能获取到所需数据。"

        # 澄清优先：直接把反问作为答案
        if obs is not None and obs.payload.get("needs_clarification"):
            clar = obs.payload.get("clarification") or ""
            return clar or "请补充统计口径。"

        observation = obs.observation if obs is not None else "（无可用资料）"

        if state.llm_calls >= self.max_llm_calls:
            # 成本上界已到：不再调 LLM，直接回观察文本（保证有答案）
            return observation[:2000]

        prompt = render_answer_prompt(state.question, observation)
        try:
            state.llm_calls += 1
            chat = self.llm.chat_with_tools(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": prompt}],
                tools=None, temperature=0.2,
            )
            if chat.content:
                return chat.content
        except Exception as e:  # noqa: BLE001
            logger.warning("[agent] 答案生成失败，回退为原始观察: %s", e)
        return observation[:2000]


_loop: Optional[AgentLoop] = None


def get_agent_loop() -> AgentLoop:
    global _loop
    if _loop is None:
        _loop = AgentLoop()
    return _loop
