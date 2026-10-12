"""app/llm/tools.py - 带 tool calling 的 LLM 客户端 + 流式 tool_calls 累积器。

为什么单独一个模块，而不是改造 llm/provider.py：
    provider.py 的 `complete()` / `stream()` 是既有 RAG 链路的稳定契约，
    被 pipeline / eval / 测试共同依赖，且**行为已被冻结**（"结果字典的键与流式事件
    类型保持与 src 完全一致，确保前端与评测无感"）。
    新增能力应当**并列而非侵入** —— 所以这里新建模块，provider.py 一行不改。
    这与本项目既有的"provider 抽象"注释一致（新实现同样的契约即可）。

⚠️ 流式 tool_calls 的三个必须照做的细节（**已用真实 API 调用逐帧验证**，
    见 docs/升级为内部知识库Agent-完整改造方案.md 4.2 节）：

    帧 #13      delta.tool_calls[0] = {index:0, id:"01a1…", type:"function",
                                       function:{name:"sql_query", arguments:""}}
                ^^^ 第一帧给 id + name
    帧 #14..#35 delta.tool_calls[0].function.arguments = "{" / "\\"sql\\": \\"SELECT" / …
                ^^^ 参数是**逐字符 JSON 字符串分片**，且 id/type/name 都是 null/空
    帧 #36      finish_reason = "tool_calls"

    1. `id` 和 `function.name` **只在第一帧出现**，后续帧为 null/空串。
       → 必须"首次出现时记录"，用朴素的"后写覆盖"合并会把 name 覆盖成空串。
    2. `arguments` 是 JSON 字符串分片，**必须等 finish_reason 后才 json.loads**，
       中途解析必然失败。
    3. 每一帧都带累计 `usage`（单调递增）→ 成本记账只需取最后一帧，无需自己累加。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests

from app.core.config import settings
from app.core.retry import call_with_retry
from app.core.performance import measured_post

logger = logging.getLogger(__name__)


# ==================== 工具调用累积器 ====================

@dataclass
class ToolCall:
    """一个完整的工具调用（累积后的结果）。"""

    id: str
    name: str
    arguments: str                      # 原始 JSON 字符串
    parsed: Optional[Dict[str, Any]] = None

    def parse_args(self) -> Dict[str, Any]:
        """解析参数；解析失败返回空 dict（调用方应检查 parse_error）。"""
        if self.parsed is not None:
            return self.parsed
        try:
            self.parsed = json.loads(self.arguments) if self.arguments.strip() else {}
        except json.JSONDecodeError as e:
            logger.warning("[tools] 参数 JSON 解析失败: %s | raw=%s", e, self.arguments[:200])
            self.parsed = {}
        return self.parsed


class ToolCallAccumulator:
    """按 index 累积流式 tool_calls 分片。

    这是手写 SSE 下最容易写错的地方，因此**独立成类并单独单测** ——
    纯数据变换，不需要网络就能穷举验证。
    """

    def __init__(self) -> None:
        # index -> {"id","name","arguments"}
        self._slots: Dict[int, Dict[str, str]] = {}
        self._order: List[int] = []

    def feed_delta(self, tool_calls_delta: List[Dict[str, Any]]) -> None:
        """喂入一帧里的 delta.tool_calls 数组。"""
        for tc in tool_calls_delta or []:
            idx = tc.get("index")
            if idx is None:
                # 少数实现不带 index：退化为"当前最后一个槽位"
                idx = self._order[-1] if self._order else 0

            if idx not in self._slots:
                self._slots[idx] = {"id": "", "name": "", "arguments": ""}
                self._order.append(idx)

            slot = self._slots[idx]

            # 细节 1：id / name 只在首帧出现 —— 非空才写，绝不用空值覆盖
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            # 细节 2：arguments 逐片追加（可以为空串，追加无副作用）
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]

    def finish(self) -> List[ToolCall]:
        """收尾：产出按 index 排序的完整工具调用列表。"""
        out: List[ToolCall] = []
        for idx in sorted(self._slots):
            s = self._slots[idx]
            out.append(ToolCall(id=s["id"], name=s["name"], arguments=s["arguments"]))
        return out

    def __bool__(self) -> bool:
        return bool(self._slots)


# ==================== 客户端 ====================

@dataclass
class ToolChatResult:
    """一次带工具的对话结果。"""

    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    raw_message: Dict[str, Any] = field(default_factory=dict)


class ToolLLMClient:
    """OpenAI 兼容的 chat/completions 客户端（支持 tools）。

    刻意用手写 requests 而非 openai SDK：与既有 provider.py 风格一致
    （项目内 HTTP 调用统一 requests），且我们已实测确认流式 tool_calls 的确切结构，
    自己累积反而**更可预测、更好单测**（ToolCallAccumulator 可脱离网络测试）。
    """

    def __init__(self) -> None:
        self.api_key = settings.siliconflow_api_key
        self.base_url = settings.siliconflow_base_url.rstrip("/")
        self.model = settings.llm_model
        self.temperature = settings.llm_temperature
        self.max_tokens = settings.llm_max_tokens
        if not self.api_key:
            raise ValueError("SILICONFLOW_API_KEY 未配置")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ---- 非流式（Text2SQL 生成主路径：拿到完整 tool_calls 才继续，无需流式）----
    def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tool_choice: str = "auto",
        timeout: int = 120,
        scene: str = "chat_with_tools",
        actor: str = "",
        tenant_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> ToolChatResult:
        """非流式带工具对话。

        Args:
            scene: 调用场景标识，用于用量记账分组
                   （如 text2sql_generate / answer_compose / summarize）
            actor / tenant_id / session_id: 归因信息，供成本看板按角色/租户/会话聚合

        ⚠️ tool_choice 只用 "auto"：
           DeepSeek 官方文档明确 "required and named tool choices are not supported
           in thinking mode; the API returns a 400 error"。为兼容性统一用 auto。
           （虽然我们当前用的是非思考模式，但保持 auto 可以随时切换模型不炸。）
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        # 带重试：实测上游偶发 504，而答案生成是链路的最后一步，
        # 失败会直接让用户看到"回退为原始观察"的降级输出（见 docs/经验教训.md L-013）
        def _do_post():
            r = measured_post("llm", self.model,
                f"{self.base_url}/chat/completions",
                headers=self._headers(), json=payload, timeout=timeout,
            )
            r.raise_for_status()
            return r

        resp = call_with_retry(_do_post, attempts=3, label="chat_with_tools")
        data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}

        calls: List[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            calls.append(ToolCall(
                id=tc.get("id") or "",
                name=fn.get("name") or "",
                arguments=fn.get("arguments") or "",
            ))

        usage = data.get("usage") or {}
        # 记账（失败不影响主流程）
        from app.analytics.usage import record_usage
        record_usage(scene, self.model, usage=usage, actor=actor,
                     tenant_id=tenant_id, session_id=session_id)

        return ToolChatResult(
            content=(msg.get("content") or "").strip(),
            tool_calls=calls,
            finish_reason=choice.get("finish_reason") or "",
            usage=usage,
            raw_message=msg,
        )

    # ---- 流式（Agent 的最终答案生成用）----
    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: int = 120,
    ) -> Iterator[Dict[str, Any]]:
        """流式对话。yield 事件字典：

            {"type": "delta",  "text": str}           文本增量
            {"type": "done",   "content": str,
                               "tool_calls": [ToolCall],
                               "finish_reason": str,
                               "usage": dict}
            {"type": "error",  "message": str}

        实测要点：每帧都带累计 usage，故只保留最后一帧即可。
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        acc = ToolCallAccumulator()
        parts: List[str] = []
        usage: Dict[str, Any] = {}
        finish_reason = ""

        try:
            resp = measured_post("llm", self.model,
                f"{self.base_url}/chat/completions",
                headers=self._headers(), json=payload, timeout=timeout, stream=True,
            )
            resp.raise_for_status()
            # 按字节行读取后显式 UTF-8 解码（与 provider.py 同口径，
            # 规避响应头缺 charset 时 iter_lines 误用 latin-1 的历史坑）
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace")
                if not line.startswith("data:"):
                    continue
                body = line[len("data:"):].strip()
                if body == "[DONE]":
                    break
                try:
                    chunk = json.loads(body)
                except json.JSONDecodeError:
                    continue

                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue

                ch = choices[0]
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                delta = ch.get("delta") or {}

                if delta.get("content"):
                    parts.append(delta["content"])
                    yield {"type": "delta", "text": delta["content"]}

                # 细节 3：tool_calls 在 delta 里增量到达，交给累积器
                if delta.get("tool_calls"):
                    acc.feed_delta(delta["tool_calls"])

            yield {
                "type": "done",
                "content": "".join(parts).strip(),
                "tool_calls": acc.finish(),
                "finish_reason": finish_reason,
                "usage": usage,
            }
        except requests.exceptions.RequestException as e:
            logger.error("[tools] 流式请求失败: %s", e)
            yield {"type": "error", "message": f"调用模型时出错: {e}"}


_client: Optional[ToolLLMClient] = None


def get_tool_llm_client() -> ToolLLMClient:
    """进程级单例。"""
    global _client
    if _client is None:
        _client = ToolLLMClient()
    return _client
