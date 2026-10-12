// 统一的 API 客户端：封装对后端 FastAPI 的 fetch 调用与错误处理
// 开发期通过 Vite 代理，使用相对路径 /api/*（同源，免 CORS）

import type {
  AgentChatRequest,
  AgentRouteInfo,
  ChatRequest,
  ChatResponse,
  DeleteDocumentResponse,
  DocumentListResponse,
  DocumentItem,
  HealthInfo,
  IngestTaskStatus,
  QueryRequest,
  QueryResponse,
  SessionCreated,
  StreamEvent,
  UploadResponse,
} from "./types";

const BASE = "/api/v1";
const KEY_STORAGE = "rag_api_key";

// API Key 仅从运行时设置读取，不允许将共享凭据烘入前端构建产物。
export function getApiKey(): string {
  try {
    const stored = localStorage.getItem(KEY_STORAGE);
    if (stored) return stored;
  } catch {
    // localStorage 不可用时由后端拒绝未认证请求
  }
  return "";
}

/** 运行时写入/更新 API Key（空串视为清除） */
export function setApiKey(key: string): void {
  try {
    if (key) localStorage.setItem(KEY_STORAGE, key);
    else localStorage.removeItem(KEY_STORAGE);
  } catch {
    // ignore
  }
}

/** 清除已保存的 API Key */
export function clearApiKey(): void {
  setApiKey("");
}

/** 从非 2xx 响应中提取可读错误信息。
 * 后端统一错误体为 {error:{message,code,status}}；兼容旧式 {detail} 。 */
async function readErrorMessage(resp: Response): Promise<string> {
  let detail = `${resp.status} ${resp.statusText}`;
  try {
    const body = await resp.json();
    if (body && body.error && body.error.message) {
      detail = String(body.error.message);
    } else if (body && body.detail) {
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    }
  } catch {
    // 忽略解析失败，回退到状态行
  }
  return detail;
}

/** 通用请求：非 2xx 统一抛出带后端错误信息的异常 */
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((options?.headers as Record<string, string>) || {}),
  };
  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;

  const resp = await fetch(`${BASE}${path}`, {
    ...options,
    headers,
  });

  if (!resp.ok) {
    throw new Error(await readErrorMessage(resp));
  }

  return (await resp.json()) as T;
}

/** 健康检查与配置回显 */
export function getHealth(): Promise<HealthInfo> {
  return request<HealthInfo>("/health");
}

/** 单轮问答 */
export function query(payload: QueryRequest): Promise<QueryResponse> {
  return request<QueryResponse>("/query", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** 多轮对话（后端 session 存储） */
export function chat(payload: ChatRequest): Promise<ChatResponse> {
  return request<ChatResponse>("/chat", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/**
 * 多轮对话的流式版本（SSE）：逐条回调 StreamEvent，支持 AbortController 外部中断。
 * 用 fetch + ReadableStream 而非 EventSource，因为需要 POST 且能带 X-API-Key 请求头。
 */
export async function chatStream(
  payload: ChatRequest,
  onEvent: (ev: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;

  const resp = await fetch(`${BASE}/chat/stream`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
    signal,
  });

  if (!resp.ok || !resp.body) {
    throw new Error(await readErrorMessage(resp));
  }

  await consumeSse(resp.body, onEvent);
}

/**
 * Agent 多轮对话的流式版本（SSE）：与 chatStream 同款解帧逻辑，
 * 但走 /agent/stream，会额外收到 route / tool_call / tool_result / sql /
 * clarify / degraded 六类轨迹事件（旧前端不认识会忽略，不会崩）。
 */
export async function agentStream(
  payload: AgentChatRequest,
  onEvent: (ev: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;

  const resp = await fetch(`${BASE}/agent/stream`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
    signal,
  });

  if (!resp.ok || !resp.body) {
    throw new Error(await readErrorMessage(resp));
  }
  await consumeSse(resp.body, onEvent);
}

/** 规则路由调试：纯函数端点，不触发 LLM/数据库，用于排查"为什么走了这条路"。 */
export function agentRoute(message: string): Promise<AgentRouteInfo> {
  return request<AgentRouteInfo>("/agent/route", {
    method: "POST",
    body: JSON.stringify({ message }),
  });
}

/**
 * 共用 SSE 解帧：按 \n\n 切帧、取 data: 前缀、UTF-8 流式解码。
 * 抽出来供 chatStream / agentStream 复用，避免两处解帧逻辑漂移
 * （中文字节按流解码是踩过的坑：必须 TextDecoder(stream: true)）。
 */
async function consumeSse(
  body: ReadableStream<Uint8Array>,
  onEvent: (ev: StreamEvent) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const line = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      const jsonStr = line.slice("data:".length).trim();
      if (!jsonStr) continue;
      try {
        onEvent(JSON.parse(jsonStr) as StreamEvent);
      } catch {
        // 忽略单帧解析异常，不中断整条流
      }
    }
  }
}

/** 新建空白会话 */
export function createSession(): Promise<SessionCreated> {
  return request<SessionCreated>("/sessions", { method: "POST" });
}

/** 删除/清空会话 */
export function deleteSession(sessionId: string): Promise<void> {
  return request<void>(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
}

/**
 * 上传文档到知识库（multipart）：后端落盘 + 入队后立即返回 task_id（202）。
 * 不手动设 Content-Type，交由浏览器带 multipart 边界；只需带 X-API-Key。
 */
export async function uploadDocument(file: File): Promise<UploadResponse> {
  const headers: Record<string, string> = {};
  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;
  const form = new FormData();
  form.append("file", file);

  const resp = await fetch(`${BASE}/documents`, { method: "POST", headers, body: form });
  if (!resp.ok) throw new Error(await readErrorMessage(resp));
  return (await resp.json()) as UploadResponse;
}

/** 轮询入库任务状态（queued/running/done/failed + progress） */
export function getIngestStatus(taskId: string): Promise<IngestTaskStatus> {
  return request<IngestTaskStatus>(`/documents/${encodeURIComponent(taskId)}/status`);
}

/** 列出当前租户已登记文档 */
export function listDocuments(): Promise<DocumentListResponse> {
  return request<DocumentListResponse>("/documents");
}

export function activateDocumentVersion(documentId: number): Promise<DocumentItem> {
  return request<DocumentItem>(`/documents/${documentId}/activate`, { method: "POST" });
}

/** 删除文档：同时清理 Qdrant 向量 + 物理副本 + 登记记录（带租户 ACL） */
export function deleteDocument(documentId: number): Promise<DeleteDocumentResponse> {
  return request<DeleteDocumentResponse>(`/documents/${documentId}`, { method: "DELETE" });
}

/** 校验当前生效的 API Key 是否合法：用一次轻量鉴权请求（列文档）探测，401/异常返回 false。 */
export async function verifyApiKey(): Promise<boolean> {
  try {
    await listDocuments();
    return true;
  } catch {
    return false;
  }
}
