"""
runner.py - 评测脚本（忠实迁移自 src/eval_runner.py）

功能:
1. 读取 qa_test_v2.jsonl 评测集
2. 对每个问题执行 RAG 查询
3. 计算指标:
   - Recall@K: 期望来源文件是否在前 K 个检索结果中（口径排除 no_knowledge 拒答题）
   - Keyword Hit Rate: LLM 回答中包含多少 expected_keywords
   - MRR / Source Rank 分布: 区分"未召回"与"召回了但排序差"，为 rerank 决策提供依据
4. 输出分类统计报告

用法:
    python -m eval                              # 运行全部评测
    python -m eval --top-k 10                   # 指定 K 值
    python -m eval --retrieval-only             # 仅测检索指标（跳过 LLM，轻量快速）
    python -m eval --dataset ../test_dataset/qa_test_v2.jsonl --output eval_v2.json

迁移差异（仅接入点，逻辑与 src 保持一致）:
- 配置来自包内 app.core.config.settings（默认评测集 = settings.test_dataset_file）
- 检索口径来自 **Agent 的 retrieval-only 模式**（app.agent.loop.AgentLoop.run），
  它只执行 kb_search、不调用 LLM 生成答案；`--backend pipeline` 保留旧
  RAGPipeline 用于迁移期对跑，Step 3 删除 pipeline 后该选项一并移除
- 输出目录锚定项目根 output/（不随 cwd 变化）
"""

import json
import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from collections import defaultdict

# Windows 下 stdout 被重定向（管道/文件）时默认走 GBK，emoji 会直接崩溃；统一强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from app.core.config import settings
from app.agent.loop import AgentLoop
from app.agent.tools import AgentTools

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 评测输出统一目录（锚定项目根，避免受启动目录影响）
OUTPUT_EVAL_DIR = settings.base_dir / "output"


def dump_text_report(report: Dict[str, Any], txt_path: Path):
    """把评测报告写入txt文件"""
    s = report["summary"]
    lines = []
    lines.append("=" * 60)
    lines.append("📊 RAG 评测报告" + (" [retrieval-only]" if s.get("retrieval_only") else ""))
    lines.append("=" * 60)
    lines.append(f"口径来源: {s.get('backend', 'unknown')}")
    lines.append(f"总问题数: {s['total_questions']} (参与判定: {s.get('judged_questions', s['total_questions'])})")
    lines.append(f"通过: {s['passed']} | 失败: {s['failed']}")
    lines.append(f"通过率: {s['pass_rate'] * 100:.2f}%")
    lines.append(f"Recall@K 命中率: {s['recall_at_k_rate'] * 100:.2f}% (K={s['top_k']}, 排除拒答题)")
    lines.append(f"MRR: {s.get('mrr', 0):.4f}")
    lines.append(f"Rank 分布: {s.get('rank_distribution', {})}")
    lines.append(f"平均关键词命中率: {s['avg_keyword_hit_rate'] * 100:.2f}%")
    lines.append("=" * 60)
    lines.append("")

    lines.append("📁 按来源文件统计:")
    for src, stats in report["by_source"].items():
        lines.append(
            f"  {src}: 通过 {stats['passed']}/{stats['total']} (Recall命中 {stats['recall_hits']}/{stats['total']})")
    lines.append("")

    lines.append("📂 按类别统计:")
    for cat, stats in report["by_category"].items():
        lines.append(
            f"  {cat}: 通过 {stats['passed']}/{stats['total']}, 平均关键词命中率 {stats['avg_keyword_hit'] * 100:.2f}%")
    lines.append("")

    if report["failed_cases"]:
        lines.append("❌ 失败案例:")
        for i, fc in enumerate(report["failed_cases"], 1):
            khr = fc.get("keyword_hit_rate")
            khr_str = f"{khr * 100:.1f}%" if khr is not None else "N/A"
            lines.append(f"  {i}. [{fc['expected_source']}] {fc['question'][:80]}...")
            lines.append(f"     Recall@K={fc['recall_at_k']}, Rank={fc.get('source_rank', -1)}, KeywordHit={khr_str}")
            lines.append(f"     回答: {fc['actual_answer'][:150]}...")
            lines.append("")

    lines.append("=" * 60)
    content = "\n".join(lines)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(content)


class AgentRetrievalBackend:
    """把 Agent 的 retrieval-only 模式包装成"和旧 pipeline 同形"的检索接口。

    ## 为什么保留 `.query()` 这个形状（而不是让 runner 直接读事件流）
    `evaluate_single` 与整套报告口径都只依赖四个字段
    （answer / sources / retrieved_count / hidden_count）。让新后端适配旧形状，
    迁移期才能**同参数对跑两个后端再逐题 diff** —— 这是"口径一致"唯一
    可验证的证明方式；反过来改 runner，就失去可比基线了。

    口径等价性由 `app.rag.executor.retrieve` 保证：两者是同一次
    `Retriever.search` 的产物，sources 顺序即 rank，这里不得二次排序。
    """

    name = "agent"

    def __init__(self, loop: Optional[AgentLoop] = None) -> None:
        self._loop = loop

    @property
    def loop(self) -> AgentLoop:
        # 延迟构造：AgentLoop 只在 `llm` 被访问时才连模型，
        # 而 retrieval-only 全程不碰 llm（见 tests/test_agent_loop.py
        # TestRetrievalOnly::test_doc_question_retrieves_without_llm）。
        if self._loop is None:
            self._loop = AgentLoop(tools=AgentTools())
        return self._loop

    def query(self, question: str, top_k: Optional[int] = None,
              generate: bool = True, filter_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """等价于 `RAGPipeline.query(question, top_k=..., generate=False)`。

        `filter_dict` 刻意不支持：评测在**无租户**上下文下运行，
        真实调用方的租户隔离由 `/agent/stream` 的 Principal 注入，
        这里放开等于给评测留一个绕过 ACL 的口子。
        """
        if generate:
            # 显式拒绝而不是静默降级：静默会让"完整评测"跑出关键词命中率 0%，
            # 看起来像答案质量崩了，实际是根本没生成。
            raise ValueError("AgentRetrievalBackend 只支持检索口径（generate=False）")
        events = list(self.loop.run(question, retrieval_only=True, top_k=top_k))
        done = next((e for e in reversed(events) if e.get("type") == "done"), None)
        if done is None:  # 事件流被截断（不应发生）：显式失败，别让指标静默变
            raise RuntimeError(f"Agent 未产出 done 事件: {question[:50]}")
        r = done.get("_retrieved") or {}
        return {
            "query": question,
            "answer": done.get("answer", ""),
            "sources": r.get("sources", []),
            "retrieved_count": int(r.get("retrieved_count", 0)),
            "hidden_count": int(r.get("hidden_count", 0)),
        }


def get_backend(name: str):
    """按名字构造检索后端（迁移期双跑用；Step 3 只剩 agent）。"""
    if name == "agent":
        return AgentRetrievalBackend()
    if name == "pipeline":
        from app.rag.pipeline import get_pipeline  # 兼容期：Step 3 一并删除

        return get_pipeline()
    raise ValueError(f"未知后端: {name}")


class EvalRunner:
    """评测执行器"""

    def __init__(self, backend: Any, top_k: int = 5, retrieval_only: bool = False):
        # backend 只需实现 `query(question, top_k=..., generate=...)`
        self.pipeline = backend
        self.top_k = top_k
        # retrieval_only: 跳过 LLM 生成，仅评测检索指标（Recall@K / Source Rank / MRR）
        self.retrieval_only = retrieval_only

    @property
    def backend_name(self) -> str:
        """报告里记录口径来源：同一组指标必须能追溯到是哪条链路产出的。"""
        return getattr(self.pipeline, "name", type(self.pipeline).__name__)

    def load_dataset(self, dataset_path: Path) -> List[Dict[str, Any]]:
        """加载 JSONL 评测集"""
        questions = []
        with open(dataset_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    questions.append(item)
                except json.JSONDecodeError as e:
                    logger.warning(f"[Eval] 解析 JSONL 失败: {e}")
        logger.info(f"[Eval] 加载评测集: {len(questions)} 条")
        return questions

    def evaluate_single(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        评测单条数据
        Returns: 包含原始数据 + 评测结果的字典
        """
        question = item["question"]
        golden_answer = item.get("golden_answer", "")
        expected_source = item.get("source_file", "")
        expected_keywords = item.get("expected_keywords", [])

        logger.info(f"[Eval] 评测: {question[:50]}...")

        # 执行 RAG 查询（retrieval_only 模式跳过 LLM 生成）
        result = self.pipeline.query(question, top_k=self.top_k, generate=not self.retrieval_only)
        actual_answer = result["answer"]
        sources = result["sources"]

        # 添加字符标准化函数
        def normalize_source_name(source_str):
            """标准化source名称，将各种破折号替换为标准短横线"""
            if not source_str:
                return source_str
            return source_str.replace("‑", "-").replace("–", "-").replace("—", "-")

        retrieved_sources = [s["source"] for s in sources]

        # 标准化source名称
        normalized_retrieved_sources = [normalize_source_name(src) for src in retrieved_sources]
        normalized_expected_source = normalize_source_name(expected_source) if expected_source else None

        recall_at_k = normalized_expected_source in normalized_retrieved_sources if normalized_expected_source else None

        # 2. 计算 Source Rank: 期望来源在结果中的排名（1-based，未找到为 -1）
        # 与 recall_at_k 保持一致，使用标准化后的名称对比（防御破折号变体）
        source_rank = -1
        if normalized_expected_source:
            for i, src in enumerate(normalized_retrieved_sources):
                if src == normalized_expected_source:
                    source_rank = i + 1
                    break

        # 3. 计算 Keyword Hit Rate: 回答中包含多少期望关键词（retrieval-only 模式无 LLM 回答，跳过）
        if self.retrieval_only:
            keyword_hits = []
            keyword_hit_rate = None
        else:
            answer_lower = actual_answer.lower()
            keyword_hits = []
            for kw in expected_keywords:
                # 支持模糊匹配：去除空格和特殊字符后比较
                kw_clean = kw.lower().replace(" ", "").replace("‑", "-")
                ans_clean = answer_lower.replace(" ", "").replace("‑", "-")
                hit = kw_clean in ans_clean
                keyword_hits.append({"keyword": kw, "hit": hit})

            keyword_hit_rate = sum(1 for h in keyword_hits if h["hit"]) / len(keyword_hits) if keyword_hits else 0

        # 4. 综合评分（按 category / 评测模式区分逻辑）
        category = item.get("category", "未分类")

        if self.retrieval_only:
            # 快速检索模式：仅按 Recall@K 判定；no_knowledge 类无期望来源，不参与判定
            passed = (recall_at_k is True) if category != "no_knowledge" else None
        elif category == "no_knowledge":
            # 知识库外问题：期望模型正确"拒答"，而非给出编造的内容。
            # 正确拒答的判定：回答中包含明确的无法回答/未收录信号。
            # 注意: 此场景下 expected_keywords 通常是问题概念词（如 LangGraph、MemorySaver），
            # 即使模型正确拒答也会在解释原因时提到它们，因此 keyword_hit_rate 无评判意义，
            # 不能作为通过条件，只能作为参考信息保留。
            refusal_keywords = [
                "无法回答",
                "未收录",
                "没有相关",
                "未能找到",
                "未提及",
                "没有提及",
                "无法确定",
                "无法找到",
                "没有找到",
                "无法提供",
                "没有提供",
                "未提供",
                "未包含",
                "没有说明",
                "未说明",
            ]
            refusal_detected = any(kw in actual_answer for kw in refusal_keywords)
            passed = refusal_detected
        else:
            # 知识库内问题：Recall@K 命中且回答覆盖大部分期望关键词，认为通过
            passed = recall_at_k is True and keyword_hit_rate >= 0.5

        return {
            # 原始数据
            "question": question,
            "golden_answer": golden_answer,
            "expected_source": expected_source,
            "expected_keywords": expected_keywords,
            "category": item.get("category", "未分类"),

            # RAG 结果
            "actual_answer": actual_answer,
            "retrieved_sources": retrieved_sources,
            "retrieved_count": result["retrieved_count"],

            # 评测指标
            "recall_at_k": recall_at_k,
            "source_rank": source_rank,
            "keyword_hits": keyword_hits,
            "keyword_hit_rate": round(keyword_hit_rate, 4) if keyword_hit_rate is not None else None,
            "passed": passed,
        }

    def run(self, dataset_path: Path) -> Dict[str, Any]:
        """
        执行完整评测
        Returns: 评测报告
        """
        questions = self.load_dataset(dataset_path)
        if not questions:
            logger.error("[Eval] 评测集为空")
            return {}

        results = []
        for item in questions:
            try:
                eval_result = self.evaluate_single(item)
                results.append(eval_result)
            except Exception as e:
                logger.error(f"[Eval] 评测失败 [{item.get('question', 'unknown')}]: {e}")
                results.append({
                    "question": item.get("question", ""),
                    "error": str(e),
                    "passed": False,
                })

        # 生成报告
        report = self._generate_report(results)
        return {"results": results, "report": report}

    def _generate_report(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """生成评测报告"""
        total = len(results)
        # 判定样本：passed 为 None（retrieval-only 下的拒答题）不参与通过率统计
        judged = [r for r in results if r.get("passed") is not None]
        passed = sum(1 for r in judged if r.get("passed") is True)
        failed = len(judged) - passed

        # 检索指标口径：仅统计知识库内问题（no_knowledge 期望来源不存在，纳入分母会失真）
        kb_results = [r for r in results if r.get("category") != "no_knowledge"]
        recall_hits = [r for r in kb_results if r.get("recall_at_k") is True]
        recall_misses = [r for r in kb_results if r.get("recall_at_k") is False]

        # MRR: 期望来源排名倒数的均值（未命中计 0），衡量排序质量
        ranks = [r["source_rank"] for r in kb_results if r.get("source_rank", -1) > 0]
        mrr = sum(1.0 / rank for rank in ranks) / len(kb_results) if kb_results else 0

        # Source Rank 分布: 区分"未召回(miss)"与"召回但排序差(rank 靠后)"
        rank_dist = {"rank_1": 0, "rank_2_3": 0, f"rank_4_{self.top_k}": 0, "miss": 0}
        for r in kb_results:
            sr = r.get("source_rank", -1)
            if sr == 1:
                rank_dist["rank_1"] += 1
            elif 2 <= sr <= 3:
                rank_dist["rank_2_3"] += 1
            elif 4 <= sr <= self.top_k:
                rank_dist[f"rank_4_{self.top_k}"] += 1
            else:
                rank_dist["miss"] += 1

        hit_results = [r for r in results if r.get("keyword_hit_rate") is not None]
        avg_keyword_hit = sum(r["keyword_hit_rate"] for r in hit_results) / len(hit_results) if hit_results else 0

        # 按 source_file 分类统计
        by_source = defaultdict(lambda: {"total": 0, "passed": 0, "recall_hits": 0})
        for r in results:
            src = r.get("expected_source", "unknown")
            by_source[src]["total"] += 1
            if r.get("passed"):
                by_source[src]["passed"] += 1
            if r.get("recall_at_k") is True:
                by_source[src]["recall_hits"] += 1

        # 按 category 分类统计
        by_category = defaultdict(lambda: {"total": 0, "passed": 0, "avg_keyword_hit": []})
        for r in results:
            cat = r.get("category", "未分类")
            by_category[cat]["total"] += 1
            if r.get("passed"):
                by_category[cat]["passed"] += 1
            if r.get("keyword_hit_rate") is not None:
                by_category[cat]["avg_keyword_hit"].append(r["keyword_hit_rate"])

        # 计算分类平均
        for cat in by_category:
            hits = by_category[cat]["avg_keyword_hit"]
            by_category[cat]["avg_keyword_hit"] = round(sum(hits) / len(hits), 4) if hits else 0

        # 失败案例（仅统计有明确判定的题）
        failed_cases = [r for r in judged if not r.get("passed")]

        report = {
            "summary": {
                "total_questions": total,
                "judged_questions": len(judged),
                "passed": passed,
                "failed": failed,
                "pass_rate": round(passed / len(judged), 4) if judged else 0,
                "recall_at_k_rate": round(len(recall_hits) / (len(recall_hits) + len(recall_misses)), 4) if (
                                                                                                                    len(recall_hits) + len(
                                                                                                                recall_misses)) > 0 else 0,
                "avg_keyword_hit_rate": round(avg_keyword_hit, 4),
                "mrr": round(mrr, 4),
                "rank_distribution": rank_dist,
                "top_k": self.top_k,
                "retrieval_only": self.retrieval_only,
                # 口径出处：agent / pipeline。删掉 pipeline 之后这个字段
                # 就是"这份报告是 Agent 产出的"的书面证据。
                "backend": self.backend_name,
            },
            "by_source": dict(by_source),
            "by_category": dict(by_category),
            "failed_cases": [
                {
                    "question": r["question"],
                    "expected_source": r.get("expected_source", ""),
                    "recall_at_k": r.get("recall_at_k"),
                    "source_rank": r.get("source_rank", -1),
                    "keyword_hit_rate": r.get("keyword_hit_rate"),
                    "actual_answer": r.get("actual_answer", "")[:200],
                }
                for r in failed_cases
            ],
        }

        return report

    def print_report(self, report: Dict[str, Any]):
        """打印评测报告到控制台"""
        s = report["summary"]

        mode_tag = " [retrieval-only]" if s.get("retrieval_only") else ""
        print("\n" + "=" * 60)
        print(f"📊 RAG 评测报告{mode_tag}")
        print("=" * 60)
        print(f"口径来源: {s.get('backend', 'unknown')}")
        print(f"总问题数: {s['total_questions']} (参与判定: {s.get('judged_questions', s['total_questions'])})")
        print(f"通过: {s['passed']} | 失败: {s['failed']}")
        print(f"通过率: {s['pass_rate'] * 100:.2f}%")
        print(f"Recall@K 命中率: {s['recall_at_k_rate'] * 100:.2f}% (K={s['top_k']}, 排除拒答题)")
        print(f"MRR: {s.get('mrr', 0):.4f}")
        print(f"Rank 分布: {s.get('rank_distribution', {})}")
        print(f"平均关键词命中率: {s['avg_keyword_hit_rate'] * 100:.2f}%")
        print("=" * 60)

        print("\n📁 按来源文件统计:")
        for src, stats in report["by_source"].items():
            print(
                f"  {src}: 通过 {stats['passed']}/{stats['total']} (Recall命中 {stats['recall_hits']}/{stats['total']})")

        print("\n📂 按类别统计:")
        for cat, stats in report["by_category"].items():
            print(
                f"  {cat}: 通过 {stats['passed']}/{stats['total']}, 平均关键词命中率 {stats['avg_keyword_hit'] * 100:.2f}%")

        if report["failed_cases"]:
            print("\n❌ 失败案例:")
            for i, fc in enumerate(report["failed_cases"][:5], 1):
                khr = fc.get("keyword_hit_rate")
                khr_str = f"{khr * 100:.1f}%" if khr is not None else "N/A"
                print(f"  {i}. [{fc['expected_source']}] {fc['question'][:60]}...")
                print(f"     Recall@K={fc['recall_at_k']}, Rank={fc.get('source_rank', -1)}, KeywordHit={khr_str}")
                print(f"     回答: {fc['actual_answer'][:100]}...")

        print("\n" + "=" * 60)


def main():
    parser = argparse.ArgumentParser(description="RAG 评测脚本")
    default_dataset_path = settings.test_dataset_file
    parser.add_argument("--dataset", type=str, default=str(default_dataset_path),
                        help="评测集路径 (JSONL 格式)")
    parser.add_argument("--top-k", type=int, default=5, help="检索 Top‑K")
    parser.add_argument("--retrieval-only", action="store_true",
                        help="仅评测检索指标（跳过 LLM 生成，轻量快速）")
    parser.add_argument("--backend", choices=["agent", "pipeline"], default="agent",
                        help="检索口径来源：agent=Agent retrieval-only（默认，唯一长期入口）"
                             "；pipeline=旧 RAGPipeline（仅迁移期对跑，Step 3 删除）")
    # 这里只传文件名，不要文件夹路径
    parser.add_argument("--output", type=str, default="eval_result.json",
                        help="输出文件名，保存到项目根 output/")
    args = parser.parse_args()

    if args.backend == "agent" and not args.retrieval_only:
        # 显式拦在入口：Agent 后端目前只实现检索口径，
        # 静默跑完整评测会产出"关键词命中率 0%"这种误导性报告。
        parser.error("--backend agent 目前只覆盖检索指标，请加 --retrieval-only；"
                     "完整答案评测暂用 --backend pipeline")

    # 自动创建输出目录
    OUTPUT_EVAL_DIR.mkdir(parents=True, exist_ok=True)

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        # 尝试 .json 后缀
        json_path = dataset_path.with_suffix(".json")
        if json_path.exists():
            dataset_path = json_path
        else:
            logger.error(f"[Eval] 评测集不存在: {dataset_path}")
            return

    # 初始化
    logger.info(f"[Eval] 初始化检索后端: {args.backend}")
    backend = get_backend(args.backend)
    runner = EvalRunner(backend, top_k=args.top_k, retrieval_only=args.retrieval_only)

    # 执行评测
    mode_desc = "retrieval-only（跳过 LLM）" if args.retrieval_only else "完整评测"
    logger.info(f"[Eval] 开始评测 ({mode_desc}, Top‑K={args.top_k}, backend={args.backend})...")
    eval_result = runner.run(dataset_path)

    if not eval_result:
        return

    # 打印报告到控制台
    runner.print_report(eval_result["report"])

    # 拼接完整输出路径
    output_path = OUTPUT_EVAL_DIR / args.output
    txt_output_path = output_path.with_suffix(".txt")

    # 保存json
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(eval_result, f, ensure_ascii=False, indent=2)
    logger.info(f"[Eval] JSON详细结果已保存: {output_path}")

    # 保存txt报告
    dump_text_report(eval_result["report"], txt_output_path)
    logger.info(f"[Eval] TXT评测报告已保存: {txt_output_path}")


if __name__ == "__main__":
    main()
