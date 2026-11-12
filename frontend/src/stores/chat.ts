// 对话状态管理（Pinia）：维护 session_id、渲染用消息列表、加载/错误状态
// 后端是历史的权威存储，前端 messages 仅用于渲染

import { defineStore } from "pinia";
import * as api from "../api/client";
import type { AgentStats, SourceItem, TraceStep } from "../api/types";

export type ChatRole = "user" | "assistant";
/** 对话通道：legacy=既有纯 RAG 路径；agent=带工具编排与执行轨迹的新路径 */
export type ChatMode = "legacy" | "agent";

export interface ChatMessage {
  role: ChatRole;
  content: string;
  sources?: SourceItem[];
  hiddenCount?: number;
  streaming?: boolean;
  /** Agent 执行轨迹（仅 agent 模式下有内容） */
  trace?: TraceStep[];
  /** 回答依据的数据通道，用于在气泡上打标 */
  channel?: "rag" | "sql" | "multi";
  /** Agent 运行统计（步数/LLM 调用数/降级） */
  stats?: AgentStats;
  /** 需要用户澄清口径时的问句 */
  clarify?: string;
}

const SESSION_KEY = "rag_session_id";
// 会话归属租户的指纹（与 session 一同存储）：Key 变更后据此丢弃旧会话，避免跨租户串会话
const SESSION_TENANT_KEY = "rag_session_tenant";

/** 当前 API Key 的轻量非可逆指纹（djb2）；空 Key 返回空串 */
function keyFingerprint(): string {
  const k = api.getApiKey();
  if (!k) return "";
  let h = 5381;
  for (let i = 0; i < k.length; i++) h = ((h << 5) + h + k.charCodeAt(i)) >>> 0;
  return h.toString(16);
}

// 当前进行中的流式请求控制器（不入 state，避免被响应式代理包裹）
let currentAbort: AbortController | null = null;

export const useChatStore = defineStore("chat", {
  state: () => ({
    sessionId: null as string | null,
    messages: [] as ChatMessage[],
    status: "idle" as "idle" | "loading" | "error",
    error: "" as string,
    topK: 5,
    initialized: false,
    /** 当前通道；可在界面上切换（agent 模式才有执行轨迹） */
    mode: "agent" as ChatMode,
  }),

  getters: {
    isLoading: (s) => s.status === "loading",
  },

  actions: {
    /** 进入页面：仅当本地 session 与当前租户指纹一致才复用，否则新建（防跨租户串会话） */
    async ensureSession() {
      if (this.initialized) return;
      const saved = localStorage.getItem(SESSION_KEY);
      const savedTenant = localStorage.getItem(SESSION_TENANT_KEY);
      if (saved && savedTenant === keyFingerprint()) {
        this.sessionId = saved;
      } else {
        // 无会话或已切换租户：清掉残留旧 id，避免沿用别的租户的会话
        localStorage.removeItem(SESSION_KEY);
        localStorage.removeItem(SESSION_TENANT_KEY);
        await this.createNewSession();
      }
      this.initialized = true;
    },

    /** 新建后端会话并写入 localStorage（连同当前租户指纹） */
    async createNewSession() {
      try {
        const res = await api.createSession();
        this.sessionId = res.session_id;
        localStorage.setItem(SESSION_KEY, res.session_id);
        localStorage.setItem(SESSION_TENANT_KEY, keyFingerprint());
        this.messages = [];
        this.error = "";
        this.status = "idle";
      } catch (e) {
        // 新建失败（如 Key 无效 401）：清掉可能残留的旧会话，修正 Key 后可干净重建
        localStorage.removeItem(SESSION_KEY);
        localStorage.removeItem(SESSION_TENANT_KEY);
        this.sessionId = null;
        this.status = "error";
        this.error = `新建会话失败: ${(e as Error).message}`;
      }
    },

    /** 发送一条消息，走多轮 /api/chat */
    async sendMessage(text: string) {
      const message = text.trim();
      if (!message || this.isLoading) return;

      await this.ensureSession();
      this.status = "loading";
      this.error = "";
      // 先本地追加用户消息，给出即时反馈
      this.messages.push({ role: "user", content: message });

      try {
        const res = await api.chat({
          message,
          session_id: this.sessionId,
          top_k: this.topK,
        });
        // 后端可能自动新建 session，回传最新的写回本地
        if (res.session_id && res.session_id !== this.sessionId) {
          this.sessionId = res.session_id;
          localStorage.setItem(SESSION_KEY, res.session_id);
        }
        this.messages.push({
          role: "assistant",
          content: res.answer,
          sources: res.sources,
          hiddenCount: res.hidden_count ?? 0,
        });
        this.status = "idle";
      } catch (e) {
        this.status = "error";
        this.error = `请求失败: ${(e as Error).message}`;
        // 撤回未成功的用户消息，保持与后端历史一致
        this.messages.pop();
      }
    },

    /** 发送一条消息（流式 SSE）：逐段增量渲染 assistant 消息 */
    async sendMessageStream(text: string) {
      const message = text.trim();
      if (!message || this.isLoading) return;

      await this.ensureSession();
      this.status = "loading";
      this.error = "";
      this.messages.push({ role: "user", content: message });
      // 先放一条空的 assistant 占位消息，后续就地累加
      this.messages.push({
        role: "assistant",
        content: "",
        sources: [],
        hiddenCount: 0,
        streaming: true,
      });
      const idx = this.messages.length - 1;

      currentAbort?.abort();
      const controller = new AbortController();
      currentAbort = controller;
      let hadError = false;

      try {
        await api.chatStream(
          { message, session_id: this.sessionId, top_k: this.topK },
          (ev) => {
            const msg = this.messages[idx];
            if (ev.type === "meta") {
              msg.sources = ev.sources;
              msg.hiddenCount = ev.hidden_count;
            } else if (ev.type === "delta") {
              msg.content += ev.text;
            } else if (ev.type === "done") {
              if (ev.answer) msg.content = ev.answer;
              msg.streaming = false;
              if (ev.session_id && ev.session_id !== this.sessionId) {
                this.sessionId = ev.session_id;
                localStorage.setItem(SESSION_KEY, ev.session_id);
              }
            } else if (ev.type === "error") {
              msg.streaming = false;
              hadError = true;
              this.status = "error";
              this.error = ev.message;
            }
          },
          controller.signal,
        );
        if (!hadError) this.status = "idle";
        const m = this.messages[idx];
        if (m) m.streaming = false;
      } catch (e) {
        const m = this.messages[idx];
        if (m) m.streaming = false;
        if ((e as Error).name === "AbortError") {
          // 用户主动中断：保留已流出内容，不算错误
          this.status = "idle";
        } else {
          this.status = "error";
          this.error = `请求失败: ${(e as Error).message}`;
        }
      } finally {
        if (currentAbort === controller) currentAbort = null;
      }
    },

    /**
     * Agent 模式发送消息（SSE）：除逐字渲染外，还把轨迹事件累积成执行轨迹。
     *
     * 事件处理原则：**未知 type 一律忽略**（不 throw、不落错误），
     * 这样后端将来扩帧时旧前端不会崩 —— 与后端"扩帧不改帧"的策略配套。
     */
    async sendMessageAgent(text: string) {
      const message = text.trim();
      if (!message || this.isLoading) return;

      await this.ensureSession();
      this.status = "loading";
      this.error = "";
      this.messages.push({ role: "user", content: message });
      this.messages.push({
        role: "assistant",
        content: "",
        sources: [],
        hiddenCount: 0,
        trace: [],
        streaming: true,
      });
      const idx = this.messages.length - 1;

      currentAbort?.abort();
      const controller = new AbortController();
      currentAbort = controller;
      let hadError = false;

      const pushTrace = (step: TraceStep) => {
        const m = this.messages[idx];
        if (m && m.trace) m.trace.push(step);
      };

      try {
        await api.agentStream(
          { message, session_id: this.sessionId },
          (ev) => {
            const msg = this.messages[idx];
            if (!msg) return;

            switch (ev.type) {
              // ---- 既有 4 类事件：语义与纯 RAG 路径完全一致 ----
              case "meta":
                msg.sources = ev.sources;
                msg.hiddenCount = ev.hidden_count;
                break;
              case "delta":
                msg.content += ev.text;
                break;
              case "done":
                if (ev.answer) msg.content = ev.answer;
                msg.streaming = false;
                if (ev.agent_stats) msg.stats = ev.agent_stats;
                if (ev.session_id && ev.session_id !== this.sessionId) {
                  this.sessionId = ev.session_id;
                  localStorage.setItem(SESSION_KEY, ev.session_id);
                }
                break;
              case "error":
                msg.streaming = false;
                hadError = true;
                this.status = "error";
                this.error = ev.message;
                break;

              // ---- 新增轨迹事件 ----
              case "route": {
                const label =
                  ev.target === "sql"
                    ? "判定为统计问题 → 走数据查询"
                    : ev.target === "rag"
                      ? "判定为文档问题 → 走知识库检索"
                      : ev.target === "meta"
                        ? "询问对话自身 → 直接依据历史作答"
                        : "交由模型选择工具";
                // meta 不检索，不归任何检索通道（否则会显示“知识库”徽标）
                if (ev.target !== "meta") {
                  msg.channel = ev.target === "sql" ? "sql" : "rag";
                }
                pushTrace({ kind: "route", label, detail: ev.reason });
                break;
              }
              case "tool_call": {
                const nameMap: Record<string, string> = {
                  kb_search: "检索知识库",
                  sql_query: "查询数据表",
                  list_data_tables: "读取数据表清单",
                };
                pushTrace({
                  kind: "tool",
                  label: nameMap[ev.name] || `调用工具 ${ev.name}`,
                  detail: ev.purpose || "",
                });
                break;
              }
              case "tool_result": {
                const m2 = this.messages[idx];
                if (m2 && m2.trace && m2.trace.length) {
                  const last = m2.trace[m2.trace.length - 1];
                  if (last.kind === "tool") {
                    last.ok = ev.ok;
                    last.label += ` · ${ev.summary}`;
                  }
                }
                break;
              }
              case "sql":
                pushTrace({
                  kind: "sql",
                  label: "执行 SQL",
                  sql: ev.sql,
                  rowCount: ev.row_count,
                  elapsedMs: ev.elapsed_ms,
                  truncated: ev.truncated,
                  ok: true,
                });
                msg.channel = "sql";
                break;
              case "clarify":
                msg.clarify = ev.question;
                pushTrace({ kind: "clarify", label: "需要澄清统计口径", detail: ev.question });
                break;
              case "degraded":
                pushTrace({
                  kind: "degraded",
                  label: "已降级为文档检索",
                  detail: ev.message || ev.reason,
                });
                break;
              default:
                // 未知事件类型：静默忽略（后端扩帧时旧前端不崩）
                break;
            }
          },
          controller.signal,
        );
        if (!hadError) this.status = "idle";
        const m = this.messages[idx];
        if (m) m.streaming = false;
      } catch (e) {
        const m = this.messages[idx];
        if (m) m.streaming = false;
        if ((e as Error).name === "AbortError") {
          this.status = "idle";
        } else {
          this.status = "error";
          this.error = `请求失败: ${(e as Error).message}`;
        }
      } finally {
        if (currentAbort === controller) currentAbort = null;
      }
    },

    /** 按当前 mode 分发（ChatView 只调这一个方法） */
    async send(text: string) {
      if (this.mode === "agent") return this.sendMessageAgent(text);
      return this.sendMessageStream(text);
    },

    /** 中断当前进行中的流式输出 */
    stopStream() {
      currentAbort?.abort();
      currentAbort = null;
    },

    /** 新会话：删除旧 session -> 清空 -> 新建 */
    async resetSession() {
      // 若有进行中的流式请求，先中断
      currentAbort?.abort();
      currentAbort = null;
      if (this.sessionId) {
        try {
          await api.deleteSession(this.sessionId);
        } catch {
          // 删除失败（可能已过期）不阻断本地重置
        }
      }
      localStorage.removeItem(SESSION_KEY);
      localStorage.removeItem(SESSION_TENANT_KEY);
      this.sessionId = null;
      this.messages = [];
      this.error = "";
      this.status = "idle";
      await this.createNewSession();
    },
  },
});
