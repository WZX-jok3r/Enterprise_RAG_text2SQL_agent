// 与后端 FastAPI (src/api.py) 的 Pydantic 模型一一对应的 TS 类型定义

/** 单条参考资料 */
export interface SourceItem {
  source: string;
  format: string;
  score: number;
  text_preview?: string | null;
  document_version_id?: string | null;
  document_version?: number | null;
}

/** POST /api/query 请求体 */
export interface QueryRequest {
  query: string;
  top_k?: number;
  filter_dict?: Record<string, unknown> | null;
}

/** POST /api/query 响应 */
export interface QueryResponse {
  query: string;
  answer: string;
  sources: SourceItem[];
  retrieved_count: number;
  hidden_count?: number;
}

/** POST /api/chat 请求体 */
export interface ChatRequest {
  message: string;
  session_id?: string | null;
  top_k?: number;
}

/** POST /api/chat 响应 */
export interface ChatResponse {
  session_id: string;
  answer: string;
  sources: SourceItem[];
  retrieved_count: number;
  history_length: number;
  hidden_count?: number;
}

/** POST /api/sessions 响应 */
export interface SessionCreated {
  session_id: string;
}

/** POST /api/chat/stream 的 SSE 事件（逐条 data: <json>）
 *
 * ⚠️ 契约说明（后端 app/agent/events.py）：这 4 类既有事件的**字段集合已冻结**，
 *    前端 stores/chat.ts 按 type 分支处理，其中 meta 承担来源列表与
 *    「已隐藏 N 条」徽标的渲染。新增能力必须**扩帧而非改帧**。
 */
export type StreamEvent =
  | { type: "meta"; sources: SourceItem[]; retrieved_count: number; hidden_count: number }
  | { type: "delta"; text: string }
  | { type: "done"; answer: string; session_id?: string; history_length?: number; agent_stats?: AgentStats }
  | { type: "error"; message: string }
  // ---- 以下为 Agent 路径新增的 6 类事件（旧前端不认识则走 default 忽略，不会崩）----
  | { type: "route"; target: "rag" | "sql" | "auto" | "multi"; reason: string }
  | { type: "tool_call"; id: string; name: string; args: Record<string, unknown>; purpose?: string }
  | { type: "tool_result"; id: string; ok: boolean; summary: string; sources?: SourceItem[]; retrieved_count?: number; hidden_count?: number }
  | { type: "sql"; sql: string; row_count: number; elapsed_ms: number; retries?: number; truncated: boolean }
  | { type: "clarify"; question: string }
  | { type: "degraded"; reason: string; message?: string };

/** Agent 运行统计（done 事件附带，用于成本/步数观测） */
export interface AgentStats {
  steps: number;
  llm_calls: number;
  tool_calls: number;
  degraded: boolean;
  degraded_reason: string;
  elapsed_ms: number;
}

/** 执行轨迹中的一步（前端渲染用） */
export interface TraceStep {
  kind: "route" | "tool" | "sql" | "clarify" | "degraded";
  label: string;
  detail?: string;
  ok?: boolean;
  /** SQL 事件附带的可展示语句与结果规模 */
  sql?: string;
  rowCount?: number;
  elapsedMs?: number;
  truncated?: boolean;
}

/** POST /api/agent/stream 请求体 */
export interface AgentChatRequest {
  message: string;
  session_id?: string | null;
  force_route?: "rag" | "sql" | null;
}

/** POST /api/agent/route 响应（规则路由调试） */
export interface AgentRouteInfo {
  route: "rag" | "sql" | "llm_decide";
  reason: string;
  matched_aggregation: string[];
  matched_column: string[];
  matched_doc_word: string[];
}

/** GET /api/health 响应 */
export interface HealthInfo {
  status: string;
  embedding_model: string;
  llm_model: string;
  collection: string;
  retrieval_mode: string;
  rerank_enabled: boolean;
  rerank_model: string | null;
  rerank_candidates: number | null;
  default_top_k: number;
}

/** POST /api/documents 上传响应（202 已接收、后台入库中） */
export interface UploadResponse {
  task_id: string;
  filename: string;
  status: string;
  message?: string;
}

/** GET /api/documents/{task_id}/status 入库任务状态 */
export interface IngestTaskStatus {
  task_id: string;
  document_id: number | null;
  status: string; // queued | running | done | failed
  progress: number; // 0~100
  error?: string | null;
  document_key?: string | null;
  version?: number | null;
  original_filename?: string | null;
  is_current?: boolean;
}

/** 单条已登记文档 */
export interface DocumentItem {
  id: number;
  source: string;
  format: string;
  status: string;
  chunk_count: number;
  document_key?: string | null;
  version?: number | null;
  original_filename?: string | null;
  is_current?: boolean;
}

/** GET /api/documents 列表响应 */
export interface DocumentListResponse {
  documents: DocumentItem[];
  total: number;
}

/** DELETE /api/documents/{id} 删除响应 */
export interface DeleteDocumentResponse {
  deleted_id: number;
  source: string;
  vectors_cleared: boolean;
  file_removed: boolean;
  message?: string;
}
