# 评测基线记录（改动的对照基准，纳入 git）

> 本文件与同目录下的 `baseline_v1.json` 配套。**任何改动检索/分块/入库/评测的提交，
> 都必须先用 `scripts/check.ps1 -Full` 与本基线逐题比对，无回归才允许继续。**

## baseline_v1 · 冻结于 HEAD `159d639`（改造开始前）

**采集条件**
- 分支 `refactor`，commit `159d639`（feat(ingestion): 为大表 xlsx 生成聚合摘要块）
- 评测口径：`python -m eval --retrieval-only`（仅检索，跳过 LLM 生成 → **确定性、可复现**）
- 评测集：`test_dataset/qa_test_v2.jsonl`（107 题，参与判定 101 题，排除 6 题 no_knowledge 拒答）
- 向量库：Qdrant `personal_rag_v2`，1402 个点
- 检索配置：`retrieval_mode=hybrid`、`top_k=5`、`prefetch_k=20`、`rerank_enabled=true`、
  `rerank_candidates=20`

**基线指标**

| 指标 | 值 |
|---|---|
| 总问题数 | 107（参与判定 101） |
| 通过 | 100 |
| 失败 | 1 |
| 通过率 | **99.01%** |
| Recall@K | **99.01%**（K=5） |
| MRR | **0.9703** |
| rank 分布 | rank_1 **96** / rank_2_3 4 / rank_4_5 0 / miss **1** |

**已知的 1 道失败题（不算回归）**
- `[docx-sample-with-table.docx] Finance部门的员工有哪些岗位？` → 未召回（Rank=-1）
- 性质：跨文件同质词面 miss，与 `159d639` 提交说明一致（"剩余 1 道为跨文件同质词面 miss"）
- **因此基线不是 100%，这 1 道题在后续比对中应始终表现为"已知 miss"，不应被当作新回归**

**代码基线**
- 业务代码 7848 行 / 81 文件
- `pytest -q` → **45 passed**（P0 新增 10 个回归闸口测试后为 **55 passed**）

## answer_quality_v1 · 首次真实模型答案质量基线（2026-10-20）

**采集条件**
- 评测集：`backend/eval/data/answer_quality_v1.jsonl`（12 题：9 道可回答、3 道不可回答）
- 模式：`python -m eval.quality --live`（真实生成 + 真实语义评审，**含 LLM 调用**）
- 生成模型：`deepseek-ai/DeepSeek-V3.2`，`temperature=0.3`，`top_k=5`
- 评审模型：`deepseek-ai/DeepSeek-V3.2`（**与生成同源**），`temperature=0`，超时 120 秒
- 向量库：Qdrant `personal_rag_v2`；**未加租户过滤**（评测证据分散在 `demo` 与 `a`/`b` 两个租户，
  无单一租户可覆盖全部 4 份文档；本次因此不检验租户隔离，隔离另有专项测试）
- 评分策略：`require_citations=true`（严格引用，未使用 `--allow-uncited`）

**基线指标**

| 指标 | 值 | 口径 |
|---|---|---|
| 总题数 / 完成评审 | 12 / **12** | `errors=0`、`semantic_coverage=1.0` |
| correctness | **0.9583** | 11 题 correct、1 题 partial |
| faithfulness | **1.0** | 9 道可回答题 |
| citation_accuracy | **1.0** | 9 道可回答题 |
| citation_coverage | **1.0** | 9 道可回答题 |
| refusal_precision / recall | **1.0 / 1.0** | 3 道不可回答题全部恰当拒答 |
| false_refusal_rate | **0.0** | 无一道可回答题被误拒 |
| 逐题通过率 | **11/12（0.9167）** | 退出码 1（评审完整、存在不达标答案） |

**已知的 1 道失败题（不算回归）**
- `aq-07`「网页端 RSA 私钥应该放在哪里？」→ `correctness=partial`、`fact_coverage_diagnostic=0.667`
- 性质：答案只答"私钥放在服务端"，遗漏 `required_facts` 中的"公钥"配套流程
- 检索是完整的（`evidence_recall=1.0`，上下文含"公钥加密…客户端只用公钥加密数据"原文），
  **损失在生成端** —— 模型对"X 放在哪里"这类聚焦型问题有很强的直接回答倾向
- 已把"回答要完整"提升为 `prompts/rag.py` 核心规则第 2 条，对 aq-09/aq-10/aq-12 有效
  （答案长度分别 +64/+117/+66 字），但 aq-07 仍稳定漏答；**不再为该题细调提示词以免过拟合 12 题回归集**
- **因此本基线不是 12/12，这道题在后续比对中应始终表现为"已知 partial"，不应被当作新回归**

**如何复现与比对**

```powershell
cd backend
# 重新采样（含 LLM 调用，有随机性）
..\backend\.venv\Scripts\python.exe -m eval.quality --live --judge-model "deepseek-ai/DeepSeek-V3.2" `
    --judge-timeout 120 --output ../output/answer_quality.json
# 与基线逐题比对
..\backend\.venv\Scripts\python.exe -m eval.quality_compare `
    --baseline eval/baseline/answer_quality_v1.json --current ../output/answer_quality.json
# 只换评审规则、不重新生成答案（隔离变量，省一半调用）
..\backend\.venv\Scripts\python.exe -m eval.quality `
    --responses eval/baseline/answer_quality_v1.responses.jsonl `
    --judge-model "deepseek-ai/DeepSeek-V3.2" --judge-timeout 120 --output ../output/rejudge.json
```

> ⚠️ **不要把本基线当作强制门禁**，理由与上面 retrieval-only 的取舍一致且更强：
> 本基线**两次都调用 LLM**（生成 `temperature=0.3` + 评审），随机性来自两侧。
> 实测同一份答案快照、同一评审模型，先后两次评审结论可以不同。
> 因此它适合做**人工复核的对照基准**，不适合像 `baseline_v1` 那样直接卡在 CI 门禁上。
> `eval.quality_compare` 在数据哈希、策略、规则版本或评审配置不一致时会拒绝比较。
>
> ⚠️ **同源评审的自评偏差未消除**：生成与评审都是 DeepSeek-V3.2，
> 本基线不能当作绝对分数。换独立评审模型后需重新采样校准。
>
> ⚠️ **全绿不等于达标**：排障过程中曾产出一次 `pass_rate=1.0`、`gate_passed=true` 的
> **虚假满分**基线 —— 评审把"额外的正确信息不扣分"反向套用来豁免 aq-07 的真实遗漏。
> 复核时必须回看单题的 `fact_coverage_diagnostic` 等诊断信号交叉验证，不能只看汇总通过率。
> 完整排障记录见 [docs/答案质量评测.md](../../../docs/答案质量评测.md)。

## 如何使用（检索门禁 · baseline_v1）

```powershell
# 1) 改动后跑完整验证（含评测 + 逐题比对）
.\scripts\check.ps1 -Full

# 2) 若只想手工比对
cd backend
python -m eval --retrieval-only
python -m eval.compare --baseline baseline_v1
```

**闸口判定规则**（`backend/eval/compare.py`）
- 聚合指标（通过率 / Recall@K / MRR）**不得下降**
- **逐题**比对 `recall_at_k` / `source_rank` / `passed`，任一劣化即判回归
- 为什么必须逐题：聚合指标会掩盖"等量置换"（A 题修好、B 题坏了，召回率仍 99.01%）
- 退出码 1 = 有回归 = **禁止继续下一步**

> ⚠️ **一个重要设计取舍**：检索门禁用的 `baseline_v1` 是 **retrieval-only**，它**不含 LLM 生成**，
> 因此它只能守住"检索行为不退化"，**守不住"答案质量不退化"**。
> 答案质量评测（faithfulness / citation / LLM-as-judge）**已于 2026-10-20 完成并首次取得真实基线**，
> 见上面的 `answer_quality_v1` 小节。它按上述理由**不进 CI 门禁**，
> 而是作为人工复核的对照基准；检索类改动仍以 `check.ps1 -Full` 为准。

> 🔴 **已知缺陷（2026-10-20 实测发现）：retrieval-only 并非确定性，门禁会误报回归。**
>
> 本文原先声称"retrieval-only 是确定性的（同样的 query+索引→同样的结果），适合做回归闸口"。
> **这个假设不成立。** 实测：**代码与索引完全未变**（检索路径文件全部 clean、
> Qdrant `personal_rag_v2` 点数 1402 未变），连续跑两次 `python -m eval --retrieval-only`：
>
> | 运行 | 通过 | 差异题 |
> |---|---|---|
> | 第 1 次 | 99/101 | `ID为42的员工叫什么名字，属于哪个部门？` → **Rank=-1（miss）** |
> | 第 2 次 | 100/101 | 同题 → **Rank=2，与基线完全一致** |
>
> 第 1 次触发了 `eval.compare` 报"回归 4 项 / 禁止继续下一步"（exit 1），第 2 次"无回归"（exit 0）。
> 也就是说**同一份未改动的代码，门禁判定可以在通过与失败之间翻转**。
>
> 推断成因（未逐层证实）：该题目标是 10000 行大表的 668 个行级语义化近似重复块，
> 相关块得分卡在 rank 2 边缘；远程 rerank（BGE-reranker-v2-m3 经 SiliconFlow）
> 或 Qdrant 多通道 prefetch 的并列打分/排序存在微小波动，足以把边缘块挤出 top-5。
>
> **对使用者的影响**：`check.ps1 -Full` 报出的**单题、rank 在 2~5 边缘**的"丢召回"回归，
> 可能是噪声而非真回归。**处置方式**：先原样重跑 1~2 次确认能否稳定复现；
> 不能复现的按噪声处理，可复现的才是真回归。
> 聚合指标（通过率/Recall@K/MRR）下降 0.01 量级同样可能由单题翻转造成，不要直接当作劣化证据。
>
> **根治方向**（本次未实施）：把 rerank 与检索改为可复现（固定 rerank 候选顺序、
> 记录并列分数、或对边缘题做多次采样取众数），或把门禁判定从"单次运行"改为"N 次运行取稳定结果"。
> 在此之前，**不要仅凭一次 `-Full` 失败就回退代码**。

## 2026-10-28 复测：连跑三次，Recall@5 落在 98.02%–99.01%

复现条件与 `baseline_v1` 一致：`QDRANT_COLLECTION_NAME=personal_rag_v2`（1402 点）、
`retrieval_mode=hybrid`、`top_k=5`、`rerank_enabled=true`、`rerank_candidates=20`；
未改代码、未重建索引。

| 运行 | 模式 | 通过 | Recall@5 | MRR | miss 题 |
|---|---|---|---|---|---|
| 第 1 次 | `--retrieval-only` | 99/101 | 98.02% | 0.9653 | Finance 岗位、ID=42 |
| 第 2 次 | `--retrieval-only` | 99/101 | 98.02% | 0.9653 | Finance 岗位、ID=42 |
| 第 3 次 | 完整（含 LLM 生成） | 100/101 | 99.01% | 0.9703 | Finance 岗位 |

**成因证据（比上文推断更具体）**

- `ID为42的员工叫什么名字，属于哪个部门？`：第 1、2 次运行**只返回 1 条结果**
  （日志 `[Search] ... 返回 1 条结果，已过滤低相关 4 条`，仅剩 `sample-pdf-with-table.pdf`），
  第 3 次运行返回 3 条、金标来源位于 rank 2。
- 即该题金标块的 rerank 分数贴在 `SCORE_ABS_MIN=0.05` 的**绝对阈值**附近：分数抖动决定它
  被保留还是被低相关过滤整块删掉，表现为整题丢召回，而不是排名小幅下降。
- `Finance部门的员工有哪些岗位？` 三次都 miss，属于上文记录的稳定已知 miss。

**结论**

- `99.01%` 是基线快照值，不能当作“当前代码必然达到”的数；当前可复现区间应写作
  **98.02%–99.01%**。
- 处置方式不变：单题丢召回先原样重跑 1~2 次；根治方向见上文（固定阈值行为、记录 rerank
  原始分数、对边缘题多次采样取众数）。

## 2026-10-28 答案质量复测

`python -m eval.quality --live --judge-model "deepseek-ai/DeepSeek-V3.2" --judge-timeout 120`
（12 题，生成与评审同源）

| 指标 | 值 | 口径 |
|---|---|---|
| 逐题通过 | 11/12（0.9167） | |
| correctness | 1.0 | 11/12 计分（coverage 0.9167） |
| faithfulness / citation_accuracy / citation_coverage | 1.0 | 8/9 覆盖 |
| errors | 1 | aq-09 `JudgeValidationError: Judge claim is not an exact answer substring` |
| gate_passed | false | 由 errors=1 触发 |

- aq-09 是**评审器缺陷**而非答案缺陷：答案本身正确（“不包括进水造成的损坏”），但评审回传的
  claim 文本带引用标记，不是答案的严格子串，被计为 error。因此本次结果**不能被
  `eval.quality_compare` 与 `answer_quality_v1` 比对**（该工具在 `errors>0` 时拒绝比较）。
- 本次 correctness 为 1.0（含 aq-07 判为 correct），与基线 `aq-07=partial` 不一致，属于同源评审的
  判定抖动；按上文“全绿不等于达标”的要求，需回看单题 `fact_coverage_diagnostic` 交叉验证，
  不要把 1.0 当作质量提升的证据。

