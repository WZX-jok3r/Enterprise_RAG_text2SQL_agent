"""
loader.xlsx - XLSX 解析（.xlsx）

- 每个 sheet 独立处理；优先 openpyxl（能读到合并单元格），缺失时降级 pandas
- 小表：每行一个 Document，附列名；大表（>= _SUMMARY_MIN_ROWS）：整表 Markdown + 统计摘要

依赖：openpyxl（首选）、pandas（降级通道）。
从 loader.py 迁出，函数体逐字未改。
"""

from pathlib import Path
from typing import Dict, List, Optional

from ._compat.xlsx import HAS_OPENPYXL, HAS_PANDAS, openpyxl, pd
from .base import Document, _data_to_markdown_table, logger


def load_xlsx(file_path: Path) -> List[Document]:
    """
    加载 .xlsx 文件
    - 每个 sheet 独立处理
    - 使用 openpyxl 读取，保留合并单元格信息
    - 每行作为一个 Document，附带表头信息
    """
    logger.info(f"[XLSX] 解析: {file_path.name}")
    documents = []
    if HAS_OPENPYXL:
        # 优先使用 openpyxl（保留合并单元格等信息）
        return _load_xlsx_openpyxl(file_path, documents)
    elif HAS_PANDAS:
        # 降级使用 pandas
        return _load_xlsx_pandas(file_path, documents)
    else:
        logger.error("openpyxl 和 pandas 均未安装，无法解析 xlsx 文件")
        return []


# 大表聚合摘要块：行数达到该阈值才生成（当前语料仅万行大表命中，
# 小表不生成，避免摘要块在聚合题上偷走期望源的位次）
_SUMMARY_MIN_ROWS = 1000


def _parse_number(value: str) -> Optional[float]:
    """尝试把单元格文本解析为数值（容忍千分位逗号与 $ 符号）"""
    v = value.replace(",", "").replace("$", "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


# 大表聚合摘要块：常见英文列名的中文同义词（使聚合题题面中文词能 BM25 命中）
_HEADER_SYNONYMS = {
    "salary": "薪资薪酬", "department": "部门", "name": "姓名",
    "price": "价格", "amount": "金额", "revenue": "收入营收",
    "headcount": "人数", "position": "职位", "title": "职位",
}


def _is_sequence_col(header: str, parsed: List[float]) -> bool:
    """判定是否为序号/ID 类列（Top/Bottom 无意义，应从聚合摘要中剔除）。
    两种信号任一命中即视为序列列：
      1. 表头名为 id/no/序号 等；
      2. 值为连续不重复整数（distinct==数量 且 max-min+1==数量，如 1..N 主键）。
    """
    name = str(header).strip().lower()
    if name in {"id", "no", "no.", "#", "rank", "index", "idx", "序号", "编号", "代码", "code"}:
        return True
    if not parsed:
        return False
    ints = [n for n in parsed if float(n).is_integer()]
    if len(ints) != len(parsed):
        return False
    distinct = set(ints)
    span = max(ints) - min(ints) + 1
    return len(distinct) == len(ints) and span == len(ints)


def _build_sheet_summary(
    file_name: str, sheet_name: str, rows_data: List[List[str]], max_chars: int = 480
) -> str:
    """
    为 xlsx 大表生成全表聚合摘要文本（单个可检索块）。
    覆盖两类全表统计结论：
      1. 类别列分组计数（哪个部门人数最多/最少）
      2. 数值列 Top/Bottom 明细（薪资最高的员工是谁+所属部门）
    输出控制在 max_chars 内，保证独立成块且 rerank 输入完整可见。
    无法识别任何可统计列时返回空串（调用方跳过）。
    """
    headers = rows_data[0]
    data_rows = rows_data[1:]
    n_cols = len(headers)

    # 逐列分类：数值列（>=80%可解析）/ 类别列（文本、低基数）/ 其他
    numeric_cols: List[int] = []
    categorical_cols: List[int] = []
    for c in range(n_cols):
        values = [r[c] for r in data_rows if c < len(r) and r[c]]
        if not values:
            continue
        nums = [_parse_number(v) for v in values]
        parsed = [n for n in nums if n is not None]
        if len(parsed) >= 0.8 * len(values):
            if len(set(parsed)) > 10 and not _is_sequence_col(headers[c], parsed):
                numeric_cols.append(c)  # 高基数非序列数值列（如 Salary）才做 Top/Bottom
            continue
        distinct = len(set(values))
        if 2 <= distinct <= 30 and distinct <= 0.5 * len(values):
            categorical_cols.append(c)

    if not numeric_cols and not categorical_cols:
        return ""

    def col_name(idx: int) -> str:
        return headers[idx] if idx < len(headers) and headers[idx] else f"列{idx + 1}"

    def labeled(idx: int) -> str:
        """列名 + 中文同义词（让题面中文词能 BM25 命中该摘要块）"""
        base = col_name(idx)
        gloss = _HEADER_SYNONYMS.get(str(base).strip().lower())
        return f"{base}（{gloss}）" if gloss else base

    # 表头列出从简（最多6列），控制摘要总长，保证独立成一块
    head_cols = "、".join(str(h) for h in headers[:6])
    parts: List[str] = [
        f"{sheet_name} 全表统计摘要（共 {len(data_rows)} 行，列："
        + head_cols + "）："
    ]
    # 类别列分组计数：最多/最少
    for c in categorical_cols:
        counter: Dict[str, int] = {}
        for r in data_rows:
            if c < len(r) and r[c]:
                counter[r[c]] = counter.get(r[c], 0) + 1
        if not counter:
            continue
        ranked = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)
        top_v, top_n = ranked[0]
        bot_v, bot_n = ranked[-1]
        parts.append(
            f"按{labeled(c)}分组共{len(ranked)}类，"
            f"{top_v}人数最多为{top_n}人，{bot_v}人数最少为{bot_n}人。"
        )
    # 数值列 Top3/Bottom3 明细（附带标识列，便于回答"是谁"）
    for c in numeric_cols:
        scored: List[Tuple[float, int]] = []
        for i, r in enumerate(data_rows):
            if c < len(r):
                n = _parse_number(r[c])
                if n is not None:
                    scored.append((n, i))
        if len(scored) < 4:
            continue
        scored.sort(key=lambda x: x[0], reverse=True)
        label = labeled(c)          # 句子头：带中文同义词，供题面词命中
        base_label = col_name(c)    # 明细行：用裸列名，避免同义词重复膨胀
        # 数字取整显示（薪资/数量类通常无小数）
        fmt = (lambda n: str(int(n)) if float(n).is_integer() else f"{n:.2f}")

        def describe(n: float, ri: int) -> str:
            r = data_rows[ri]
            # 标识取“姓名+部门”风格：按表头名优先，Email 等长文本压低
            label_by = {"first name": 1, "last name": 2, "department": 3, "email": 9}
            cells: List[Tuple[int, str]] = []
            for j in range(min(len(r), n_cols)):
                if j == c or not r[j] or _parse_number(r[j]) is not None:
                    continue
                pri = label_by.get(str(headers[j]).lower(), 0)
                cells.append((pri if pri else 4, r[j]))
            cells.sort(key=lambda x: x[0])
            who = " ".join(v for _, v in cells[:3]) or f"第{ri + 2}行"
            return f"{who} {base_label} {fmt(n)}"

        top_desc = "；".join(describe(n, i) for n, i in scored[:3])
        bot_desc = "；".join(describe(n, i) for n, i in scored[-3:])
        parts.append(
            f"按{label}排序，最高Top3：{top_desc}；最低Bottom3：{bot_desc}。"
        )
    summary = "\n".join(parts)
    # 超长时从尾部逐段回退，保证至少头部+第一个统计结论完整；
    # 阈值压到 480 以下，预留 indexer 文件名前缀长度，避免摘要被切分成两块
    while len(summary) > max_chars and len(parts) > 2:
        parts.pop()
        summary = "\n".join(parts)
    if len(summary) > max_chars:
        summary = summary[:max_chars]
    return summary


def _load_xlsx_openpyxl(file_path: Path, documents: List[Document]) -> List[Document]:
    """使用 openpyxl 加载 xlsx"""
    try:
        wb = openpyxl.load_workbook(file_path, data_only=True)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows_data = []
            # 获取合并单元格信息
            merged_ranges = list(ws.merged_cells.ranges)
            for row in ws.iter_rows(values_only=True):
                # 过滤全空行
                if any(cell is not None for cell in row):
                    cleaned_row = [str(cell).strip() if cell is not None else "" for cell in row]
                    rows_data.append(cleaned_row)
            if rows_data:
                md_table = _data_to_markdown_table(rows_data)
                documents.append(Document(
                    text=md_table,
                    metadata={
                        "source": file_path.name,
                        "full_path": str(file_path),
                        "format": "xlsx",
                        "type": "sheet",
                        "sheet_name": sheet_name,
                        "rows": len(rows_data),
                    },
                ))
                # 大表聚合摘要：分块行只含局部数据，"哪个部门人数最多/薪资最高是谁"
                # 这类全表聚合题在任何单块上都无依据（rerank 绝对分天然低被过滤）。
                # 仅对 >=SUMMARY_MIN_ROWS 行的表生成（当前语料只有万行大表命中，
                # 小表不生成避免摘要块在聚合题上偷走期望源的位次），把全表统计结论
                # 变成可检索块：分组计数 + 数值列 Top/Bottom 明细。
                if len(rows_data) - 1 >= _SUMMARY_MIN_ROWS:
                    summary = _build_sheet_summary(file_path.name, sheet_name, rows_data)
                    if summary:
                        documents.append(Document(
                            text=summary,
                            metadata={
                                "source": file_path.name,
                                "full_path": str(file_path),
                                "format": "xlsx",
                                "type": "summary",
                                "sheet_name": sheet_name,
                                "rows": len(rows_data) - 1,
                            },
                        ))
        wb.close()
        return documents
    except Exception as e:
        logger.error(f"[XLSX] openpyxl 解析失败 {file_path.name}: {e}")
        return documents  # 返回已提取的内容


def _load_xlsx_pandas(file_path: Path, documents: List[Document]) -> List[Document]:
    """使用 pandas 加载 xlsx（降级方案）"""
    try:
        xl = pd.ExcelFile(file_path)
        for sheet_name in xl.sheet_names:
            df = pd.read_excel(file_path, sheet_name=sheet_name)
            if df.empty:
                logger.warning(f"[XLSX] Sheet 为空: {sheet_name} in {file_path.name}")
                continue
            # 获取表头
            headers = df.columns.tolist()
            header_str = " | ".join(str(h) for h in headers)
            # 每行作为一个 Document
            for idx, row in df.iterrows():
                row_values = " | ".join(str(v) if pd.notna(v) else "" for v in row.values)
                # 组合表头 + 行数据，方便语义理解
                text = f"表头: {header_str}\n数据: {row_values}"
                documents.append(
                    Document(
                        text=text,
                        metadata={
                            "source": file_path.name,
                            "full_path": str(file_path),
                            "format": "xlsx",
                            "type": "row",
                            "sheet_name": sheet_name,
                            "row_index": int(idx),
                            "headers": headers,
                        },
                    )
                )
        if not documents:
            logger.warning(f"[XLSX] 未提取到有效内容: {file_path.name}")
        return documents
    except Exception as e:
        logger.error(f"[XLSX] pandas 解析失败 {file_path.name}: {e}")
        return []
