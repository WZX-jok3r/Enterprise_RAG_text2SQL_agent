"""multiturn.py - 多轮追问评测（P1 的基线与对照工具）。

## 为什么不能复用 eval/runner.py

`runner.py` 评的是单轮 RAG：一次 question -> 一次检索 -> 一个答案。
而多轮缺陷的全部特征都在**轮与轮之间**（历史注入、tool_context 延续、
规则路由只看当前句），用单轮集永远测不出"第 2 轮答不出上一次问了什么"。

这里把每条用例构造成"setup 轮 + 追问轮"两次独立运行，中间把
answer / tool_context 拼成 history —— 与 `api/v1/agent.py` 落库的形状一致。

## 判定口径（三个独立指标，不要混成一个"通过率"）

- `passed`      : 追问轮 required_facts **全部命中**，且不出现拒答词
- `setup_ok`    : setup 轮是否答对。setup 挂了时追问失败**不能算到追问机制头上**
- `route_ok`    : 追问轮实际路由 == expect_route（不参与 passed）

把三者分开是刻意的：P1-1（元问题直答）改变的是 `route` 与检索次数，
**答案文本本就可能是对的**。若只看 passed，会得出"改了个寂寞"的错误结论。

## 成本口径

每题记录追问轮的 `llm_calls` 与耗时，用于判断 query 改写（P1-2）
是不是拿"多一次调用"换来了"多对一题"。

用法:
    python -m eval.multiturn --tag baseline
    python -m eval.multiturn --tag p1_1 --tenant a --limit 4
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

from app.agent.loop import get_agent_loop
from app.core.config import settings

# 拒答信号：这些用例全部可回答，出现即失败。
# ⚠️ 必须用**拒答句式**，不能用单字“无法” —— 实测“否则 uvx 依旧无法使用”
#    这类正确回答会被误判为失败（第一版 baseline 就踩到了，两条假失败）。
DEFAULT_FORBIDDEN = (
    "无法确定", "无法回答", "无法从", "未找到", "资料不足", "没有相关",
    "不能确定", "未提供", "未提及", "没有说明",
)

DEFAULT_DATASET = Path(__file__).parent / "data" / "multiturn_v1.jsonl"


def normalize(text: str) -> str:
    """NFKC + 去空白 + 去千分位逗号（模型常写 2,052）。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s,，]", "", text)


def hit_facts(facts: List[str], answer: str) -> List[str]:
    norm = normalize(answer)
    return [f for f in facts if normalize(f) in norm]


def run_turn(loop, question: str, history: List[Dict[str, Any]],
             tenant: str) -> Dict[str, Any]:
    """跑一轮，收集答案、路由、降级原因、工具轨迹与成本。"""
    route = degraded = None
    answer = tool_ctx = ""
    stats: Dict[str, Any] = {}
    t0 = time.monotonic()
    for ev in loop.run(question, tenant_id=tenant, history=history):
        etype = ev.get("type")
        if etype == "route":
            route = ev.get("target")
        elif etype == "degraded":
            degraded = ev.get("reason")
        elif etype == "done":
            answer = ev.get("answer") or ""
            tool_ctx = ev.get("_tool_context") or ""
            stats = ev.get("agent_stats") or {}
    return {
        "question": question,
        "answer": answer,
        "route": route,
        "degraded": degraded,
        "tool_context": tool_ctx,
        "llm_calls": int(stats.get("llm_calls", 0)),
        "tool_calls": int(stats.get("tool_calls", 0)),
        "elapsed_ms": int(stats.get("elapsed_ms") or (time.monotonic() - t0) * 1000),
    }


class MultiTurnRunner:
    def __init__(self, tenant: str = "a", loop: Optional[Any] = None) -> None:
        self.tenant = tenant
        self.loop = loop or get_agent_loop()

    def load_dataset(self, path: Path) -> List[Dict[str, Any]]:
        cases = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    cases.append(json.loads(line))
        return cases

    def run_case(self, case: Dict[str, Any]) -> Dict[str, Any]:
        history: List[Dict[str, Any]] = []
        turns: Dict[str, Dict[str, Any]] = {}

        for label in ("setup", "question"):
            question = case[label]
            facts = case.get("setup_facts", []) if label == "setup" else case["required_facts"]
            turn = run_turn(self.loop, question, history, self.tenant)
            turn["fact_hits"] = hit_facts(facts, turn["answer"])
            turn["fact_miss"] = [f for f in facts if f not in turn["fact_hits"]]
            turns[label] = turn
            # 与 API 层落库同形：assistant 消息带 tool_context（空串省略）
            history.append({"role": "user", "content": question})
            history.append({
                "role": "assistant",
                "content": turn["answer"],
                "tool_context": turn["tool_context"] or None,
            })

        setup, final = turns["setup"], turns["question"]
        setup_facts = case.get("setup_facts", [])
        setup_ok = (not setup["fact_miss"]) if setup_facts else None
        forbidden = [w for w in DEFAULT_FORBIDDEN if normalize(w) in normalize(final["answer"])]
        passed = bool(case["required_facts"]) and not final["fact_miss"] and not forbidden
        # expect_route 可以是字符串或列表：追问句常常不含“是什么”这类文档词，
        # 实际走 auto（模型自选 kb_search）—— 功能上是正确通道，不该计为不命中。
        expect = case["expect_route"]
        expect_set = expect if isinstance(expect, list) else [expect]
        route_ok = final["route"] in expect_set

        return {
            "id": case["id"],
            "category": case["category"],
            "note": case.get("note", ""),
            "expect_route": case["expect_route"],
            "setup": setup,
            "final": final,
            "setup_ok": setup_ok,
            "route_ok": route_ok,
            # 是否被规则强路由（sql/rag）。P1-1 之后 meta 类应从 1 变成 0。
            "rule_routed": final["route"] in ("sql", "rag"),
            # 追问轮的工具调用数：P1-1 的收益口径（元问题应把 kb_search 降为 0）
            "final_tool_calls": final["tool_calls"],
            "forbidden_hits": forbidden,
            "passed": passed,
        }

    def run(self, dataset: Path) -> Dict[str, Any]:
        cases = self.load_dataset(dataset)
        results = [self.run_case(c) for c in cases]
        return {"results": results, "report": self._report(results)}

    def _report(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        total = len(results)
        passed = sum(1 for r in results if r["passed"])
        route_ok = sum(1 for r in results if r["route_ok"])
        rule_routed = sum(1 for r in results if r["rule_routed"])
        judged_setup = [r for r in results if r["setup_ok"] is not None]
        setup_ok = sum(1 for r in judged_setup if r["setup_ok"])

        by_cat: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"total": 0, "passed": 0, "route_ok": 0, "llm_calls": 0, "tool_calls": 0})
        for r in results:
            b = by_cat[r["category"]]
            b["total"] += 1
            b["passed"] += 1 if r["passed"] else 0
            b["route_ok"] += 1 if r["route_ok"] else 0
            b["llm_calls"] += r["final"]["llm_calls"]
            b["tool_calls"] += r["final_tool_calls"]

        llm_calls = [r["final"]["llm_calls"] for r in results]
        tool_calls = [r["final_tool_calls"] for r in results]
        elapsed = [r["final"]["elapsed_ms"] for r in results]
        return {
            "total": total,
            "passed": passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "route_ok": route_ok,
            "route_acc": round(route_ok / total, 4) if total else 0.0,
            "rule_routed": rule_routed,
            "setup_judged": len(judged_setup),
            "setup_ok": setup_ok,
            "avg_llm_calls": round(sum(llm_calls) / total, 2) if total else 0.0,
            "avg_tool_calls": round(sum(tool_calls) / total, 2) if total else 0.0,
            "avg_elapsed_ms": int(sum(elapsed) / total) if total else 0,
            "by_category": {k: dict(v) for k, v in by_cat.items()},
            "failed_cases": [
                {
                    "id": r["id"],
                    "category": r["category"],
                    "question": r["final"]["question"],
                    "route": r["final"]["route"],
                    "expect_route": r["expect_route"],
                    "fact_miss": r["final"]["fact_miss"],
                    "forbidden_hits": r["forbidden_hits"],
                    "setup_ok": r["setup_ok"],
                    "answer": r["final"]["answer"][:300],
                }
                for r in results if not r["passed"]
            ],
        }


def dump_text(report: Dict[str, Any], tag: str) -> str:
    lines = [
        "=" * 66,
        f"多轮追问评测 [{tag}]",
        "=" * 66,
        f"追问轮通过   : {report['passed']}/{report['total']}  ({report['pass_rate'] * 100:.1f}%)",
        f"路由命中     : {report['route_ok']}/{report['total']}  ({report['route_acc'] * 100:.1f}%)",
        f"规则强路由   : {report['rule_routed']}/{report['total']}（未命中则交 LLM 自选工具）",
        f"setup 轮通过 : {report['setup_ok']}/{report['setup_judged']}（不看它会把 setup 失败算错账）",
        f"平均 LLM 调用: {report['avg_llm_calls']}  平均工具调用: {report['avg_tool_calls']}"
        f"  平均耗时: {report['avg_elapsed_ms']} ms",
        "=" * 66,
    ]
    for cat, s in report["by_category"].items():
        lines.append(f"  {cat:<14} 通过 {s['passed']}/{s['total']}  "
                     f"路由 {s['route_ok']}/{s['total']}  "
                     f"LLM {s['llm_calls']}  工具 {s['tool_calls']}")
    if report["failed_cases"]:
        lines.append("")
        lines.append("失败明细:")
        for i, fc in enumerate(report["failed_cases"], 1):
            lines.append(f"  {i}. [{fc['id']}] {fc['question']}")
            lines.append(f"     route={fc['route']} (期望 {fc['expect_route']}) "
                         f"setup_ok={fc['setup_ok']} 缺={fc['fact_miss']} "
                         f"拒答词={fc['forbidden_hits']}")
            lines.append(f"     回答: {fc['answer'][:200]}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="多轮追问评测")
    parser.add_argument("--tag", default="run", help="报告标签（输出文件名后缀）")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--tenant", default="a")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    args = parser.parse_args()

    runner = MultiTurnRunner(tenant=args.tenant)
    dataset = Path(args.dataset)
    cases = runner.load_dataset(dataset)
    if args.limit:
        cases = cases[: args.limit]

    results, t0 = [], time.monotonic()
    for i, case in enumerate(cases, 1):
        r = runner.run_case(case)
        results.append(r)
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"[{i}/{len(cases)}] {mark} {r['id']} route={r['final']['route']} "
              f"({r['final']['elapsed_ms']}ms)")

    report = runner._report(results)
    out_dir = Path(settings.base_dir) / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"tag": args.tag, "tenant": args.tenant,
               "dataset": dataset.name, "report": report, "results": results}
    json_path = out_dir / f"multiturn_{args.tag}.json"
    txt_path = out_dir / f"multiturn_{args.tag}.txt"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    text = dump_text(report, args.tag)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)

    print()
    print(text)
    print(f"\n耗时 {time.monotonic() - t0:.1f}s")
    print(f"JSON -> {json_path}")
    print(f"TXT  -> {txt_path}")


if __name__ == "__main__":
    main()
