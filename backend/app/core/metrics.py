"""metrics.py - 轻量指标注册表 + Prometheus 文本格式导出（P7）。

## 为什么用标准库手写而不是引入 prometheus_client

`prometheus_client` 是事实标准，但它会带来：
- 一个新的运行时依赖（本项目镜像构建一次要几分钟，依赖越少越好）；
- 它的 multiprocess / registry 语义对本项目（单进程 uvicorn）是过剩的。

我们需要的其实很小：**计数器 + 直方图 + 标签维度 + 文本导出**。
标准库即可，且行为完全可控、可单测。
（将来若要多进程聚合/推送网关，再换 prometheus_client 也不影响上层 ——
 本模块刻意只暴露 register/counter_inc/observe/render 四个函数。）

## 暴露的指标（与 Grafana 看板一一对应）

| 指标 | 类型 | 标签 | 回答什么问题 |
|---|---|---|---|
| `rag_http_requests_total` | counter | method, path, status | QPS、错误率 |
| `rag_http_request_duration_seconds` | histogram | method, path | P95 延迟 |
| `rag_agent_runs_total` | counter | route, degraded | 各通道占比、**降级率** |
| `rag_agent_steps` | histogram | — | Agent 平均步数（成本相关） |
| `rag_llm_tokens_total` | counter | scene, kind | token 消耗趋势 |
| `rag_sql_guard_rejections_total` | counter | reason | **安全网关在真实流量下挡了多少** |
| `rag_sql_redactions_total` | counter | column | **有多少人在试探敏感数据** |
| `rag_sql_queries_total` | counter | ok | SQL 成功/失败 |
| `rag_sql_duration_seconds` | histogram | — | SQL 耗时 |
| `rag_retrieval_chunks` | histogram | — | 召回条数分布 |

后几个是安全/运营指标，也是简历上"真实数字"的来源。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Dict, List, Sequence, Tuple

# 延迟直方图分桶（秒）——覆盖"检索 <1s"到"Agent 超时 60s"的区间
LATENCY_BUCKETS: Sequence[float] = (
    0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
)
# Agent 步数分桶（离散小整数）
STEPS_BUCKETS: Sequence[float] = (1, 2, 3, 4, 5, 6, 8, 10)
# 召回条数分桶
CHUNKS_BUCKETS: Sequence[float] = (0, 1, 2, 3, 5, 8, 10, 20, 50)
# SQL 耗时分桶（毫秒级查询为主）
SQL_BUCKETS: Sequence[float] = (0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0)

_lock = threading.Lock()

# name -> {label_tuple: value}
_counters: Dict[str, Dict[Tuple[str, ...], float]] = defaultdict(lambda: defaultdict(float))

# name -> {label_tuple: {"sum": float, "count": int, "counts": {bucket: n}}}
_histograms: Dict[str, Dict[Tuple[str, ...], Dict]] = defaultdict(dict)

# name -> (help, label_names, type)
_META: Dict[str, Tuple[str, List[str], str]] = {}
# name -> buckets（**注册时确定**，observe 时按名字取用，保证一致）
_BUCKETS: Dict[str, List[float]] = {}


def register_counter(name: str, help_text: str, labels: Sequence[str] = ()) -> None:
    _META[name] = (help_text, list(labels), "counter")


def register_histogram(name: str, help_text: str, labels: Sequence[str] = (),
                       buckets: Sequence[float] = LATENCY_BUCKETS) -> None:
    _META[name] = (help_text, list(labels), "histogram")
    _BUCKETS[name] = [float(b) for b in buckets]


def counter_inc(name: str, labels: Sequence[str] = (), value: float = 1.0) -> None:
    key = tuple(str(x) for x in labels)
    with _lock:
        _counters[name][key] += value


def observe(name: str, value: float, labels: Sequence[str] = ()) -> None:
    """记录一次观测值。

    ⚠️ 分桶取自注册时的配置（`_BUCKETS[name]`），**不是**硬编码默认值 ——
    否则 `rag_agent_steps`（分桶 1..10）会按延迟分桶（0.01..60）统计，
    导致所有观测都落在第一个桶里，指标静默失真。

    ⚠️⚠️ **只累加命中的那一个桶**（`value <= b` 中**最小**的 b），
        累计由 render() 负责。这是本模块最容易写错的地方，见下方说明。

    ## 为什么必须"存原始计数、渲染时再累计"（一次真实事故）

    初版写成「observe 时把所有 `value <= b` 的桶都 +1」（即 observe 侧就做累计），
    而 render() 又对 `counts` 做了一次前缀和 —— **累计了两次**。
    后果（实测，3 次观测）：
        _count = 3，+Inf = 3，但最后一个桶 = 20
        桶值随观测数**二次增长**，完全不是计数
    这违反 Prometheus exposition format：`_bucket{le=X}` 必须是
    「观测值 ≤ X 的**次数**」。`histogram_quantile()` 依赖该前提，
    喂给它这种序列会算出**无意义的 P95**，而且是静默的（指标照常有输出）。

    顺带说明：初版当时被"验证通过"过一次，但那次验证脚本本身就是错的 ——
    它复刻了 observe 的**错误逻辑**再和导出结果比，等于自己和自己比。
    参见 docs/经验教训.md L-017 的更正。
    """
    key = tuple(str(x) for x in labels)
    buckets = _BUCKETS.get(name, list(LATENCY_BUCKETS))
    with _lock:
        store = _histograms[name]
        entry = store.get(key)
        if entry is None:
            entry = {"sum": 0.0, "count": 0, "counts": {}}
            store[key] = entry
        entry["sum"] += float(value)
        entry["count"] += 1
        counts = entry["counts"]
        # 只落进"能容纳它的最小桶"。若超出所有桶，则不落任何桶
        # （+Inf 桶由 render 用 _count 输出，天然覆盖这种情况）。
        for b in buckets:
            if value <= b:
                counts[b] = counts.get(b, 0) + 1
                break


def _escape(v: object) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_labels(names: Sequence[str], values: Sequence[str],
                extra: Sequence[Tuple[str, str]] = ()) -> str:
    pairs = [f'{n}="{_escape(v)}"' for n, v in zip(names, values)]
    pairs += [f'{n}="{_escape(v)}"' for n, v in extra]
    return "{" + ",".join(pairs) + "}" if pairs else ""


def _num(v: object) -> str:
    """Prometheus 数值格式：整数不带小数点，浮点保留足够精度。"""
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "0"
    if f.is_integer():
        return str(int(f))
    return repr(round(f, 6))


def render() -> str:
    """导出 Prometheus 文本格式（exposition format 0.0.4）。"""
    lines: List[str] = []
    with _lock:
        for name in sorted(_META):
            help_text, label_names, mtype = _META[name]
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")

            if mtype == "counter":
                data = _counters.get(name) or {}
                if not data:
                    # 无数据也输出 0，避免 Grafana 报 "no data"
                    lines.append(f"{name}{_fmt_labels(label_names, [''] * len(label_names))} 0")
                for key, val in sorted(data.items()):
                    lines.append(f"{name}{_fmt_labels(label_names, key)} {_num(val)}")

            elif mtype == "histogram":
                buckets = _BUCKETS.get(name, list(LATENCY_BUCKETS))
                for key, entry in sorted((_histograms.get(name) or {}).items()):
                    counts = entry["counts"]
                    cumulative = 0
                    for b in buckets:
                        cumulative += int(counts.get(b, 0))
                        lines.append(
                            f"{name}_bucket{_fmt_labels(label_names, key, [('le', _num(b))])} {cumulative}"
                        )
                    total = int(entry["count"])
                    lines.append(
                        f"{name}_bucket{_fmt_labels(label_names, key, [('le', '+Inf')])} {total}"
                    )
                    lines.append(f"{name}_sum{_fmt_labels(label_names, key)} {_num(entry['sum'])}")
                    lines.append(f"{name}_count{_fmt_labels(label_names, key)} {total}")
    return "\n".join(lines) + "\n"


def reset() -> None:
    """清空所有指标数据（**仅供测试**；保留注册信息与分桶配置）。"""
    with _lock:
        _counters.clear()
        _histograms.clear()


# ==================== 指标声明（模块导入即注册）====================

register_counter("rag_http_requests_total", "HTTP 请求总数",
                 ("method", "path", "status"))
register_histogram("rag_http_request_duration_seconds", "HTTP 请求耗时（秒）",
                   ("method", "path"), LATENCY_BUCKETS)
register_counter("rag_agent_runs_total",
                 "Agent 运行次数（route=实际通道，degraded=是否降级）",
                 ("route", "degraded"))
register_histogram("rag_agent_steps", "Agent LLM 工具选择循环的轮次分布", (), STEPS_BUCKETS)
# 为什么还需要单独一个 tool_calls 指标（实测得出的教训）：
#   `steps` 只统计 LLM 工具选择循环的轮次，而**规则强路由路径根本不进那个循环**
#   （它由 router 直接决定通道），于是那条路径的 steps 恒为 0 ——
#   可它明明做了「读表清单 + 执行 SQL」两件实事。
#   若只看 steps，会误判"Agent 什么也没干"。tool_calls 才反映**实际工作量**。
register_histogram("rag_agent_tool_calls", "Agent 单次运行的工具调用次数分布",
                   (), (0, 1, 2, 3, 4, 6, 8, 12, 20))
# 追问改写：跟随强制路由的追问才会有，reason 区分“改写生效”与四类 fail-open。
# 为什么要单独计数：它**不计入** MAX_LLM_CALLS，若只看 llm_calls 会把这块成本漏掉。
register_counter("rag_agent_rewrites_total",
                 "追问改写次数（reason=rewritten|same|invalid|error）", ("reason",))
register_counter("rag_llm_tokens_total", "LLM token 消耗（kind=prompt|completion）",
                 ("scene", "kind"))
register_counter("rag_sql_guard_rejections_total",
                 "被安全网关拒绝的 SQL 数（真实流量下的安全拦截量）", ("reason",))
register_counter("rag_sql_redactions_total",
                 "敏感列脱敏次数（按列，反映敏感数据访问尝试）", ("column",))
register_counter("rag_sql_queries_total", "SQL 执行次数", ("ok",))
register_histogram("rag_sql_duration_seconds", "SQL 执行耗时（秒）", (), SQL_BUCKETS)
register_histogram("rag_retrieval_chunks", "单次检索召回的片段数", (), CHUNKS_BUCKETS)
