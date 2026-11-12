#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_demo/server.py —— 零依赖的 MCP Server（stdio 传输）

刻意不使用官方 `mcp` SDK：SDK 会把协议全部封装掉，你就看不到线路上跑的
到底是什么。这里手写 JSON-RPC 2.0 的收发，目的是**把协议摊开给你看**。

默认实现协议版本 2026-07-28（当前正式版，**无状态**）。
加 --legacy 切到 2025-06-18 风格（有 initialize 握手 + 有状态），
因为现存的大多数 MCP server 仍是旧版，两种都得看得懂。

═══════════════════════════════════════════════════════════════
⚠️ 写 MCP server 的第一条铁律：stdout 是协议信道
═══════════════════════════════════════════════════════════════
stdio 传输下，服务端**只能**往 stdout 写 JSON-RPC 报文。
任何 print()、日志、调试输出、甚至第三方库的告警，一旦进了 stdout，
客户端 json.loads 就会崩，而且报错信息完全看不出原因。

所以本文件所有日志一律写 stderr（见 log()）。这是新手最常踩的坑。

⚠️ 第二条铁律：报文内不能含裸换行
stdio 传输按「行」分帧（newline-delimited JSON），
所以写出去时用 json.dumps(..., ensure_ascii=False) 后再补一个 \n，
中间不能有其它换行符。

⚠️ 第三条：工具参数 = 不可信输入
tools/call 的 arguments 由**大模型**填写，模型可以被 prompt 注入操纵。
任何身份/权限信息（租户、角色、密钥）绝不能从 arguments 取。
本文件用 kb_search（正确）与 kb_search_unsafe（错误）对照演示这一点。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

# ==================== 协议常量 ====================

VERSION_STATELESS = "2026-07-28"   # 当前正式版：无状态、无握手
VERSION_LEGACY = "2025-06-18"      # 旧版：有 initialize 握手

SERVER_INFO = {"name": "demo-rag-server", "version": "0.1.0"}

# ---- JSON-RPC 2.0 标准错误码 ----
ERR_PARSE = -32700            # 收到非法 JSON
ERR_INVALID_REQUEST = -32600  # 不符合 JSON-RPC 2.0 结构
ERR_METHOD_NOT_FOUND = -32601  # 方法不存在
ERR_INVALID_PARAMS = -32602   # 参数非法
ERR_INTERNAL = -32603         # 服务端内部错误

# 2026-07-28 起 _meta 里的保留键（反向 DNS 前缀）
K_VERSION = "io.modelcontextprotocol/protocolVersion"
K_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
K_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"


def log(msg: str) -> None:
    """日志一律走 stderr，绝不污染 stdout 这条协议信道。"""
    print(f"[server] {msg}", file=sys.stderr, flush=True)


# ==================== 假数据（模拟你项目的知识库与业务表）====================
#
# 注意每条记录都带 tenant 字段 —— 多租户隔离是本 demo 的重点之一。

FAKE_KB: List[Dict[str, Any]] = [
    {
        "id": "doc-a1", "tenant": "tenant_a",
        "source": "保修政策_2026版.pdf",
        "text": "整机保修期为自签收之日起 12 个月，主要部件 24 个月。",
    },
    {
        "id": "doc-a2", "tenant": "tenant_a",
        "source": "保修政策_2026版.pdf",
        "text": "人为损坏、私自拆机、非授权维修不在保修范围内。",
    },
    {
        "id": "doc-b1", "tenant": "tenant_b",
        "source": "内部定价表_机密.xlsx",
        "text": "tenant_b 专属折扣：旗舰型号出厂价 8,800 元，渠道返点 12%。",
    },
    {
        "id": "doc-b2", "tenant": "tenant_b",
        "source": "客户合同_机密.docx",
        "text": "tenant_b 与某集团签订年度框架合同，合同金额 4,300 万元。",
    },
]

FAKE_TABLES = [
    {"table_name": "employees", "columns": ["id", "dept", "name", "salary"]},
    {"table_name": "sales_orders", "columns": ["id", "region", "amount", "created_at"]},
]

# ══════════════════════════════════════════════════════════════
# 租户身份从**环境变量**取 —— 这是带外通道（out-of-band），模型碰不到。
#
# 对应你项目里的做法：tenant_id 来自 api/deps.py 解析出的 Principal，
# 再由 loop.py 显式传给 dispatch()，全程不进模型的 args。
# MCP 场景下等价物就是：环境变量（stdio）或 HTTP header（streamable HTTP）。
# ══════════════════════════════════════════════════════════════
TENANT_ID = os.environ.get("DEMO_TENANT_ID", "tenant_a")


# ==================== 工具定义（tools/list 返回的东西）====================
#
# inputSchema 是标准 JSON Schema（2026-07-28 默认 dialect 为 2020-12）。
# 这份 schema 会被客户端转成大模型的 function-calling 声明 ——
# 也就是说，description 的措辞直接影响模型选不选这个工具。
#
# 对比你项目 backend/app/prompts/agent.py:68 那份手写 TOOLS：
# 结构几乎一样，只是字段名从 "parameters" 变成 "inputSchema"，
# 且外层不用再包 {"type":"function","function":{...}}。

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "kb_search",
        "title": "知识库检索（安全版）",
        "description": (
            "在内部文档知识库中检索相关段落，用于回答产品规格、政策条款等非结构化问题。"
            "不适用于需要统计计算的问题（那类请用 sql_query）。"
            "检索范围自动限定在当前调用方所属租户，无法跨租户查询。"
        ),
        # ⚠️ 关键设计：schema 里**根本没有 tenant_id 这个参数**。
        #    身份不是模型可以填写的东西，所以它压根不该出现在 inputSchema 里。
        #    模型看不到这个参数，就没法伪造它。
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词或问题"},
                "top_k": {"type": "integer", "description": "返回条数，默认 3", "minimum": 1},
            },
            "required": ["query"],
        },
    },
    {
        "name": "kb_search_unsafe",
        "title": "知识库检索（❌ 反面教材）",
        "description": "演示用：把 tenant_id 暴露成工具参数，可被模型任意指定。",
        # ⚠️ 这是**错误的写法**，故意留着做对照实验。
        #    tenant_id 出现在 inputSchema 里 = 模型可以填任意租户 = 跨租户泄露。
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词"},
                "tenant_id": {"type": "string", "description": "要查询的租户 ID"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "sql_query",
        "title": "只读 SQL 查询",
        "description": (
            "对业务数据表执行只读 SELECT 查询，用于精确统计。"
            "只允许单条 SELECT；写操作会被拒绝。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "一条完整的 SELECT 语句，不要代码围栏，不要分号结尾",
                },
            },
            "required": ["sql"],
        },
        # outputSchema 是 2025-06-18 引入的结构化输出契约。
        # 声明了它，server 就**必须**返回符合该结构的 structuredContent，
        # 客户端**应该**做校验。好处是上层能直接拿结构化数据，不用再解析文本。
        "outputSchema": {
            "type": "object",
            "properties": {
                "row_count": {"type": "integer"},
                "columns": {"type": "array", "items": {"type": "string"}},
                "rows": {"type": "array", "items": {"type": "array"}},
            },
            "required": ["row_count", "columns", "rows"],
        },
    },
    {
        "name": "list_data_tables",
        "title": "列出可查数据表",
        "description": "列出当前可查询的业务数据表及字段。不确定有哪些数据可查时先调用它。",
        # 无参数工具的规范写法：additionalProperties: false，
        # 明确「只接受空对象」，比单写 {"type":"object"} 更严格。
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
]


# ==================== 工具实现 ====================

def _text(s: str) -> Dict[str, Any]:
    """构造一个 TextContent 块。"""
    return {"type": "text", "text": s}


def tool_kb_search(args: Dict[str, Any]) -> Dict[str, Any]:
    """安全版：租户身份来自环境变量，模型无法干预。"""
    query = str(args.get("query", "")).strip()
    top_k = int(args.get("top_k") or 3)

    if not query:
        # 这是「工具执行错误」——模型看得懂、能自己改参数重试。
        # 所以用 isError: true 返回，**不是** JSON-RPC error。
        return _execution_error("query 不能为空，请提供检索关键词。")

    # ↓↓↓ 隔离发生在这里：先用带外身份过滤，再谈检索 ↓↓↓
    visible = [d for d in FAKE_KB if d["tenant"] == TENANT_ID]
    hits = [d for d in visible if query.lower() in d["text"].lower()
            or query.lower() in d["source"].lower()][:top_k]

    if not hits:
        return _ok("知识库中未找到相关内容。",
                   {"hit_count": 0, "tenant": TENANT_ID})

    lines = [f"[{i}]（来源：{h['source']}）{h['text']}" for i, h in enumerate(hits, 1)]
    return _ok("\n".join(lines),
               {"hit_count": len(hits), "tenant": TENANT_ID,
                "ids": [h["id"] for h in hits]})


def tool_kb_search_unsafe(args: Dict[str, Any]) -> Dict[str, Any]:
    """❌ 反面教材：租户身份从 args 取，等于把权限凭据交给了模型。"""
    query = str(args.get("query", "")).strip()
    # 这一行就是漏洞本身：模型传什么租户，就查什么租户。
    spoofable = str(args.get("tenant_id") or TENANT_ID)

    visible = [d for d in FAKE_KB if d["tenant"] == spoofable]
    hits = [d for d in visible if query.lower() in d["text"].lower()
            or query.lower() in d["source"].lower()][:5]

    if not hits:
        return _ok(f"（以 tenant_id={spoofable} 检索）未找到相关内容。",
                   {"hit_count": 0, "tenant_used": spoofable})

    lines = [f"[{i}]（来源：{h['source']}，租户 {h['tenant']}）{h['text']}"
             for i, h in enumerate(hits, 1)]
    return _ok("\n".join(lines),
               {"hit_count": len(hits), "tenant_used": spoofable,
                "ids": [h["id"] for h in hits]})


def tool_sql_query(args: Dict[str, Any]) -> Dict[str, Any]:
    """模拟只读 SQL 网关（对应你项目的 guard_and_record）。"""
    sql = str(args.get("sql", "")).strip().rstrip(";")
    if not sql:
        return _execution_error("sql 不能为空。")

    # 简化版只读校验。生产环境请用 AST 解析（你项目用的 sqlglot），
    # 字符串前缀判断能被注释、CTE、多语句等绕过。
    head = sql.lstrip("(").lstrip().lower()
    if not head.startswith("select"):
        return _execution_error(
            f"只允许 SELECT 查询，检测到写操作。请改写为只读查询后重试。"
            f"（收到：{sql[:60]}）"
        )
    if ";" in sql:
        return _execution_error("不允许多条语句，请只提交一条 SELECT。")

    # 假结果集
    columns = ["dept", "headcount"]
    rows = [["Sales", 1042], ["Engineering", 1010]]
    observation = (
        f"SQL：{sql}\n结果（{len(rows)} 行）：\n"
        + "\n".join(" | ".join(str(c) for c in r) for r in rows)
    )
    return _ok(observation,
               {"row_count": len(rows), "columns": columns, "rows": rows})


def tool_list_data_tables(args: Dict[str, Any]) -> Dict[str, Any]:
    """无参数工具。"""
    lines = []
    for t in FAKE_TABLES:
        lines.append(f"- {t['table_name']}：{', '.join(t['columns'])}")
    return _ok("\n".join(lines),
               {"tables": [t["table_name"] for t in FAKE_TABLES]})


TOOL_IMPL = {
    "kb_search": tool_kb_search,
    "kb_search_unsafe": tool_kb_search_unsafe,
    "sql_query": tool_sql_query,
    "list_data_tables": tool_list_data_tables,
}


# ==================== 结果构造：两种错误必须分清 ====================
#
# MCP 有两套错误通道，用错了模型就没法自我修正：
#
#   1) 协议错误  -> JSON-RPC 的 error 字段
#      用于「请求本身有问题」：未知工具名、报文格式错、缺必填 _meta。
#      模型通常**改不了**这类错误。
#
#   2) 工具执行错误 -> result 里 isError: true
#      用于「工具跑了但业务上失败」：参数越界、日期格式错、SQL 被拒。
#      模型**能看懂并自己改参数重试** —— 所以必须走这条通道，
#      并且 content 里要写清楚「错在哪、该怎么改」。
#
# 规范原文：客户端 SHOULD 把工具执行错误反馈给模型以支持自我修正。

def _ok(text: str, structured: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "resultType": "complete",
        "content": [_text(text)],
        "isError": False,
    }
    if structured is not None:
        # 规范建议：返回 structuredContent 时，**同时**在 TextContent 里
        # 放一份序列化 JSON，以兼容不认识 structuredContent 的旧客户端。
        result["structuredContent"] = structured
    return result


def _execution_error(msg: str) -> Dict[str, Any]:
    return {"resultType": "complete", "content": [_text(msg)], "isError": True}


# ==================== 请求分发 ====================

def handle_tools_list(stateless: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {"tools": TOOLS}
    if stateless:
        # resultType 是 2026-07-28 新增的**必填**字段。
        # 客户端靠它区分「正常完成」还是「需要补充输入」(input_required)。
        # 为兼容旧 server，规范规定：缺 resultType 时客户端必须当作 complete。
        result["resultType"] = "complete"
        result["_meta"] = {"io.modelcontextprotocol/serverInfo": SERVER_INFO}
        # 还支持分页与缓存（nextCursor / ttlMs / cacheScope），demo 里不需要。
    return result


def handle_tools_call(params: Dict[str, Any], stateless: bool) -> Dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}

    # 未知工具 = 协议错误（不是工具执行错误）
    if name not in TOOL_IMPL:
        raise ProtocolError(ERR_INVALID_PARAMS, f"Unknown tool: {name}")

    if not isinstance(args, dict):
        raise ProtocolError(ERR_INVALID_PARAMS, "arguments 必须是 JSON 对象")

    log(f"tools/call name={name} args_keys={sorted(args.keys())}")
    try:
        result = TOOL_IMPL[name](args)
    except Exception as e:  # noqa: BLE001
        # 工具内部炸了 = 服务端错误。包装成工具执行错误更友好，
        # 但真实生产里建议记日志 + 返回 -32603，别把栈信息喂给模型。
        log(f"工具 {name} 内部异常: {e!r}")
        return _execution_error(f"工具执行失败：{type(e).__name__}")

    if not stateless:
        result.pop("resultType", None)   # 旧版没有这个字段
    return result


class ProtocolError(Exception):
    """要变成 JSON-RPC error 响应的异常。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ==================== 有状态（legacy）模式的会话状态 ====================

class LegacySession:
    """旧版协议要求先握手再干活，这里维护那份状态。

    2026-07-28 已经**删掉**了这套机制 —— 对照着看才理解「无状态」改了什么。
    """

    def __init__(self) -> None:
        self.initialized = False

    def handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        client_info = params.get("clientInfo") or {}
        log(f"收到 initialize，客户端={client_info}，"
            f"请求版本={params.get('protocolVersion')}")
        self.initialized = True
        return {
            "protocolVersion": VERSION_LEGACY,
            "capabilities": {
                # 声明 tools 能力是**必须**的（规范用 MUST）。
                # listChanged=true 表示工具列表变化时会主动发通知。
                "tools": {"listChanged": True}
            },
            "serverInfo": SERVER_INFO,
        }


# ==================== 主循环 ====================

def validate_meta(params: Dict[str, Any]) -> None:
    """2026-07-28：每个请求都必须在 _meta 里自带协议元数据。

    这是「无状态」的落地方式：服务端不记得你是谁，
    所以每个请求都得自报家门。缺必填字段 -> -32602。
    """
    meta = params.get("_meta") or {}
    missing = [k for k in (K_VERSION, K_CLIENT_CAPS) if k not in meta]
    if missing:
        raise ProtocolError(
            ERR_INVALID_PARAMS,
            f"缺少必填的 _meta 字段: {missing}。"
            f"无状态协议要求每个请求自带 protocolVersion 与 clientCapabilities。",
        )
    version = meta.get(K_VERSION)
    if version != VERSION_STATELESS:
        log(f"⚠️ 客户端声明版本 {version} 与本服务端 {VERSION_STATELESS} 不一致"
            f"（demo 不做严格协商，继续处理）")


def main() -> None:
    parser = argparse.ArgumentParser(description="零依赖 MCP demo server")
    parser.add_argument("--legacy", action="store_true",
                        help=f"用旧版有状态协议（{VERSION_LEGACY}），演示 initialize 握手")
    args = parser.parse_args()

    stateless = not args.legacy
    version = VERSION_STATELESS if stateless else VERSION_LEGACY
    session = LegacySession()

    log(f"启动，协议版本={version}，模式={'无状态' if stateless else '有状态(legacy)'}")
    log(f"当前租户身份（来自环境变量 DEMO_TENANT_ID）= {TENANT_ID}")
    log("等待 stdin 上的 JSON-RPC 报文…")

    # ---- 按行读 stdin：这就是 stdio 传输的全部 ----
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue

        # 1) 解析 JSON
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            _send_error(None, ERR_PARSE, f"Parse error: {e}")
            continue

        req_id = msg.get("id")
        method = msg.get("method")

        # 2) 通知（notification）：没有 id，**绝不能**回响应
        if req_id is None:
            log(f"收到通知 method={method}（不回响应）")
            continue

        # 3) 请求：分发
        try:
            params = msg.get("params") or {}
            if method == "ping":
                result: Dict[str, Any] = {"resultType": "complete"} if stateless else {}

            elif stateless:
                # ── 无状态模式：没有 initialize，直接干活 ──
                validate_meta(params)
                if method == "tools/list":
                    result = handle_tools_list(stateless=True)
                elif method == "tools/call":
                    result = handle_tools_call(params, stateless=True)
                else:
                    raise ProtocolError(ERR_METHOD_NOT_FOUND,
                                        f"Method not found: {method}")
            else:
                # ── 有状态模式：必须先握手 ──
                if method == "initialize":
                    result = session.handle_initialize(params)
                elif not session.initialized:
                    raise ProtocolError(
                        ERR_INVALID_REQUEST,
                        "尚未完成 initialize 握手，拒绝处理其它请求",
                    )
                elif method == "tools/list":
                    result = handle_tools_list(stateless=False)
                elif method == "tools/call":
                    result = handle_tools_call(params, stateless=False)
                else:
                    raise ProtocolError(ERR_METHOD_NOT_FOUND,
                                        f"Method not found: {method}")

            _send_result(req_id, result)

        except ProtocolError as e:
            _send_error(req_id, e.code, e.message)
        except Exception as e:  # noqa: BLE001
            log(f"处理 {method} 时内部异常: {e!r}")
            _send_error(req_id, ERR_INTERNAL, f"Internal error: {type(e).__name__}")

    log("stdin 关闭，服务端退出")


def _send_result(req_id: Any, result: Dict[str, Any]) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _send_error(req_id: Any, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}})


def _write(obj: Dict[str, Any]) -> None:
    """唯一的出口：一行一个 JSON 报文，写 stdout。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
