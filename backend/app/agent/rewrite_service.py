"""rewrite_service.py - 追问改写的**独立服务**（预算 / 熔断 / 失败兜底）。

## 为什么要从 loop 里抽出来

改写是整个 Agent 链路上唯一一个"**可选的、同步阻塞的**外呼"：

    它的收益只是让检索更准，代价却是每一次追问都往主链路里插一段等待。

把这件事留在 loop 的 `if` 里，最直接的后果是**没人对这段等待负责** ——
超时给多少、要不要重试、上游连续失败时还要不要试，全靠读代码猜。
实测证据（P1-2 首轮评测）：sql 组 8 条里出现 40s/52s 的单条耗时，
而当时以为"只是上游变慢"，真正的放大器是 `chat_with_tools` 内置的
3 次重试 —— `timeout=8` 不是总预算，最坏 8s×3 + 退避 ≈ 26s。

抽成服务后，三件事有了明确的归属：

1. **预算**：`timeout_ms` 是**总预算**，由 `future.result(timeout)` 强制生效；
   `retry_attempts` 恒为 1。一次 64 token 的调用正常 1~2s，给 4s 已是重尾余量；
   超了就按原文检索。
2. **熔断**：连续 N 次失败后冷却 M 秒，期间所有追问直接 `skip`。
   没有它，上游持续抽风时每个追问都会白白耗掉一个预算周期 ——
   故障不会因为"再试一次"而消失，只会从"慢"变成"每个请求都慢"。
3. **可观测**：每次调用都返回真实耗时与调用次数，loop 只需把它塞进 stats，
   "改写到底吃了多少延迟"从此有数字可查，不再靠整体耗时反推。

## 明确不做的事

- **不异步化**：异步改写需要"先按原文检索一次、改写完再检索一次"，
  等于多一次检索成本，且两次结果不一致时还要定仲裁规则 ——
  对一个省几百毫秒的优化来说，复杂度与后果都不成比例。
- **不重试**：重试的语义是"这次失败会毁掉结果"，而改写失败只是少了一次优化。
- **不缓存**：同会话连续追问本就各有各的上下文，缓存命中率低且容易串味。

## 失败语义（fail-open，任何分支都返回可用 query）

    disabled     开关关闭
    skip         闸门不过（首轮 / 不依赖上文 / 无可补的上下文）
    breaker_open 熔断冷却中，直接按原文检索
    rewritten    改写生效
    same         模型判断原问题已完整
    invalid      输出不像一句话（解释 / 过长 / 为空）
    timeout      超过硬预算（主链路已不再等它）
    error        调用报错（连接失败 / 上游 5xx / 不可重试的 4xx）
"""

from __future__ import annotations

import concurrent.futures as futures
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from app.agent.rewrite import (
    REWRITE_SYSTEM,
    last_round,
    needs_rewrite,
    parse_rewrite,
    render_rewrite_prompt,
)
from app.core.config import settings

logger = logging.getLogger(__name__)

# 改写的**硬预算**靠这个池实现：requests 的 timeout 只是 socket 级超时。
# 实测证据（P1-3 首轮验收）：配置 3s，实际一条跑了 4360ms 才抛错 ——
# 建连、重定向、响应体分块读取都不受 read timeout 约束。
# 只有 future.result(timeout) 能给主链路一个"到点就走"的真上限。
_EXECUTOR = futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="rewrite")

# socket 层超时取预算的倍数：主链路到点就走，但后台线程要能自行结束，
# 否则慢上游会长期占住线程池（4 个 worker 耗尽后，后续请求会排队等待）。
_SOCKET_FACTOR = 2.0


@dataclass(frozen=True)
class RewriteOutcome:
    """一次改写尝试的结果。

    `query` 永远可用：失败时就是原问题。调用方不需要先判 reason。
    """

    query: str
    reason: str
    elapsed_ms: int = 0     # 真实 LLM 调用耗时（未发起调用时为 0）
    attempts: int = 0       # 实际发出的调用次数（0 或 1）

    @property
    def applied(self) -> bool:
        return self.reason == "rewritten"


class _Breaker:
    """进程内连续失败熔断（够用即可，不引依赖）。

    为什么用"连续失败"而不是"滑动窗口失败率"：改写的调用量本来就稀疏
    （只有追问才触发），窗口统计在低流量下不可靠；连续失败已经能覆盖
    "上游持续抽风"这个唯一要防的场景。
    """

    def __init__(self, threshold: int, cooldown_s: float) -> None:
        self.threshold = max(1, int(threshold))
        self.cooldown_s = max(0.0, float(cooldown_s))
        self._failures = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            return time.monotonic() >= self._open_until

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0

    def record_failure(self) -> bool:
        """记一次失败；返回本次是否**刚刚**触发熔断（用于日志）。"""
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._open_until = time.monotonic() + self.cooldown_s
                self._failures = 0
                return True
            return False


class RewriteService:
    """把追问补全成可独立理解的检索词。

    `llm` 缺省时惰性取 `get_tool_llm_client()` 单例；测试可直接注入假实现。
    `enabled` / `timeout_ms` / `max_tokens` 缺省时**每次调用**读 settings
    （而不是构造时快照），这样运行期改配置或测试 monkeypatch 都能生效。
    """

    def __init__(
        self,
        llm: Optional[Any] = None,
        *,
        enabled: Optional[bool] = None,
        timeout_ms: Optional[int] = None,
        max_tokens: Optional[int] = None,
        breaker_threshold: Optional[int] = None,
        breaker_cooldown_s: Optional[float] = None,
    ) -> None:
        self._llm = llm
        self._enabled = enabled
        self._timeout_ms = timeout_ms
        self._max_tokens = max_tokens
        self.breaker = _Breaker(
            settings.query_rewrite_breaker_threshold
            if breaker_threshold is None else breaker_threshold,
            settings.query_rewrite_breaker_cooldown_s
            if breaker_cooldown_s is None else breaker_cooldown_s,
        )

    # ---- 配置（惰性读取，见类 docstring）----
    @property
    def enabled(self) -> bool:
        return settings.query_rewrite_enabled if self._enabled is None else self._enabled

    @property
    def timeout_ms(self) -> int:
        raw = settings.query_rewrite_timeout_ms if self._timeout_ms is None else self._timeout_ms
        return max(1, int(raw))

    @property
    def max_tokens(self) -> int:
        raw = settings.query_rewrite_max_tokens if self._max_tokens is None else self._max_tokens
        return max(8, int(raw))

    @property
    def llm(self) -> Any:
        if self._llm is None:
            from app.llm.tool_client import get_tool_llm_client

            self._llm = get_tool_llm_client()
        return self._llm

    # ---- 主入口 ----
    def rewrite(self, question: str, history: Sequence[Dict[str, Any]]) -> RewriteOutcome:
        if not self.enabled:
            return RewriteOutcome(question, "disabled")
        if not needs_rewrite(question, history):
            return RewriteOutcome(question, "skip")
        if not self.breaker.allow():
            # 冷却期内不再尝试：这段时间的追问按原文检索（宁可少一次优化，
            # 也不要每个请求都卡满一个预算周期）
            return RewriteOutcome(question, "breaker_open")

        context = last_round(history)
        if not context or not question:
            return RewriteOutcome(question, "skip")

        messages = [
            {"role": "system", "content": REWRITE_SYSTEM},
            {"role": "user", "content": render_rewrite_prompt(context, question)},
        ]
        budget_s = self.timeout_ms / 1000.0
        t0 = time.monotonic()
        future = _EXECUTOR.submit(self._call, messages, budget_s)
        try:
            # 硬预算：不管上游多慢，到点就放弃（后台线程继续跑完并自行退出）
            content = future.result(timeout=budget_s)
        except Exception as e:  # noqa: BLE001
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            # ⚠️ 不能只看异常类型：Python 3.11+ 起 `futures.TimeoutError` 就是
            # 内置 `TimeoutError`，与上游 socket 读超时撞类型（单测已踩过：
            # 假 LLM 抛 TimeoutError 被误判成预算超时，elapsed=0ms）。
            # 用 future.done() 区分：**result() 自己超时时任务还没结束**。
            timed_out = isinstance(e, futures.TimeoutError) and not future.done()
            if timed_out:
                future.cancel()
                reason, detail = "timeout", f"超预算({elapsed_ms}ms > {self.timeout_ms}ms)"
            else:
                reason, detail = "error", f"{type(e).__name__}，{elapsed_ms}ms: {str(e)[:150]}"
            just_opened = self.breaker.record_failure()
            logger.warning(
                "[rewrite] 改写未生效(%s)，按原问题检索: %s%s",
                detail, question,
                f"（已熔断 {self.breaker.cooldown_s:g}s）" if just_opened else "",
            )
            return RewriteOutcome(question, reason, elapsed_ms=elapsed_ms, attempts=1)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        self.breaker.record_success()
        query, reason = parse_rewrite(question, content)
        return RewriteOutcome(query, reason, elapsed_ms=elapsed_ms, attempts=1)

    def _call(self, messages: Sequence[Dict[str, str]], budget_s: float) -> str:
        """实际外呼（在校程池里执行，主链路按预算等待它）。"""
        socket_timeout = max(2, int(round(budget_s * _SOCKET_FACTOR)))
        chat = self.llm.chat_with_tools(
            messages,
            tools=None,
            temperature=0.0,
            max_tokens=self.max_tokens,
            timeout=socket_timeout,
            retry_attempts=1,   # 预算内只试一次：重试会把 4s 变成 12s
            scene="query_rewrite",
        )
        return getattr(chat, "content", "") or ""


_service: Optional[RewriteService] = None


def get_rewrite_service() -> RewriteService:
    """进程级单例：熔断状态跨请求共享，才能对"上游故障"做出全局反应。"""
    global _service
    if _service is None:
        _service = RewriteService()
    return _service


def set_rewrite_service(service: Optional[RewriteService]) -> None:
    """替换单例（**仅供测试**；传 None 恢复默认）。"""
    global _service
    _service = service
