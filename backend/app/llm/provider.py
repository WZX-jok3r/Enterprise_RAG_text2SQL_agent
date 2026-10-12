"""
provider.py - LLM 网关（SiliconFlow / OpenAI 兼容 chat/completions）

忠实迁移自 src/rag_pipeline.py 的 _do_llm_request / _stream_llm，保留全部生产行为：
- 统一 Langfuse generation 埋点，记录 input/output 与 token usage；
- 非流式 complete(): 请求异常/解析异常均降级为友好提示（不抛出），返回 (answer, usage)；
- 流式 stream(): 仅处理 data: 帧，[DONE] 终止，纯 usage 帧只记录不产出；SSE 按字节行
  读取后显式 UTF-8 解码（规避响应头缺 charset 时 iter_lines 误用 latin-1 的历史坑）；
  请求异常向上抛，由调用方（rag.stream_chat）转成 error 事件。

provider 抽象：当前仅 SiliconFlow（OpenAI 兼容）。如需接入 OpenAI/Azure/Claude/本地模型，
新增实现同样的 complete()/stream() 契约的类，并在 get_llm_client() 里按配置选择即可，
上层 rag 无需改动。
"""

import json
import logging
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests

from app.core.config import settings
from app.core.retry import call_with_retry
from app.core.performance import measured_post
from app.observability import langfuse as obs

logger = logging.getLogger(__name__)


class LLMClient:
    """SiliconFlow（OpenAI 兼容）聊天补全客户端。"""

    def __init__(self):
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

    def complete(self, messages: List[Dict[str, str]],
                 temperature: Optional[float] = None,
                 max_tokens: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
        """
        非流式补全（统一入口，query 与 query_with_history 共用）。
        - 在一个 Langfuse generation 下发起请求，记录 input/output 与 token usage；
          可观测未启用时 generation 为 no-op，行为与之前完全一致。
        - 返回 (答案文本, usage 字典)；异常时返回友好提示与空 usage（不抛出）。
        """
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }

        with obs.generation("llm", model=self.model, input=messages) as gen:
            try:
                # 带重试：实测上游偶发 504（见 docs/经验教训.md L-013）。
                # 只对**非流式**路径加重试 —— 流式已开始推帧后重试会导致
                # 用户看到重复内容，因此流式保持"失败即报错"的既有行为。
                def _do_post():
                    r = measured_post("llm", self.model, url, headers=self._headers(), json=payload, timeout=120)
                    r.raise_for_status()
                    return r

                response = call_with_retry(_do_post, attempts=3, label="llm.complete")
                data = response.json()
                answer = data["choices"][0]["message"]["content"].strip()
                usage = obs.extract_usage(data)
                gen.update(output=answer, usage=usage, metadata={"model": self.model})
                return answer, usage

            except requests.exceptions.RequestException as e:
                logger.error(f"[LLM] API 请求失败: {e}")
                gen.update(output=f"抱歉，调用模型时出错: {str(e)}", level="ERROR", status_message=str(e))
                return f"抱歉，调用模型时出错: {str(e)}", {}
            except (KeyError, IndexError) as e:
                logger.error(f"[LLM] 响应解析失败: {e}")
                gen.update(output="抱歉，模型响应格式异常。", level="ERROR", status_message=str(e))
                return "抱歉，模型响应格式异常，请稍后重试。", {}

    def stream(self, messages: List[Dict[str, str]],
               temperature: Optional[float] = None,
               max_tokens: Optional[int] = None) -> Iterator[str]:
        """流式补全（stream=True），逐段 yield 文本增量。
        - 仅处理 `data:` 帧；`[DONE]` 终止；无 choices 的纯 usage 帧只记录不产出。
        - 全程包在 obs.generation 下，结束时以累计全文与 usage 更新；请求异常向上抛。"""
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        with obs.generation("llm", model=self.model, input=messages) as gen:
            parts: List[str] = []
            usage: Dict[str, Any] = {}
            try:
                resp = measured_post("llm", self.model, url, headers=self._headers(), json=payload, timeout=120, stream=True)
                resp.raise_for_status()
                # SSE 帧为 UTF-8；响应头无 charset 时 iter_lines(decode_unicode=True) 会误用 latin-1，
                # 故按字节行读取后显式以 UTF-8 解码（\n=0x0A 不会出现在 UTF-8 多字节序列中，逐行解码安全）
                for raw in resp.iter_lines():
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace")
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = obs.extract_usage(chunk)
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        parts.append(delta)
                        yield delta
                gen.update(output="".join(parts).strip(), usage=usage,
                           metadata={"model": self.model, "stream": True})
            except requests.exceptions.RequestException as e:
                logger.error(f"[LLM stream] 请求失败: {e}")
                gen.update(output="".join(parts), level="ERROR", status_message=str(e))
                raise


def get_llm_client() -> LLMClient:
    """获取 LLMClient 实例。"""
    return LLMClient()
