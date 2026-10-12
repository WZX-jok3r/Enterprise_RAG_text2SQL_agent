<script setup lang="ts">
// 文档上传面板（抽屉）：选文件 → 异步入队（202 返回 task_id）→ 轮询任务状态/进度。
// 入库由后端 ARQ worker 在后台完成，这里只负责触发与观测。

import { ref, onBeforeUnmount, watch } from "vue";
import * as api from "../api/client";
import type { DocumentItem, IngestTaskStatus } from "../api/types";

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ (e: "close"): void }>();

interface Task extends IngestTaskStatus {
  filename: string;
}

const fileInput = ref<HTMLInputElement | null>(null);
const picked = ref<File | null>(null);
const uploading = ref(false);
const error = ref("");
const tasks = ref<Task[]>([]);
const docs = ref<DocumentItem[]>([]);
const deletingId = ref<number | null>(null);
const activatingId = ref<number | null>(null);
const deleteNotice = ref("");

let pollTimer: ReturnType<typeof setInterval> | null = null;

const ACTIVE = new Set(["queued", "running"]);

async function refreshDocs() {
  try {
    const res = await api.listDocuments();
    docs.value = res.documents;
  } catch {
    // 列表失败不阻断上传功能
  }
}

async function onDelete(d: DocumentItem) {
  if (deletingId.value !== null) return;
  const ok = window.confirm(
    `确认删除《${d.original_filename || d.source}》${d.version ? ` v${d.version}` : ""}？\n将清理此版本的向量与文件，不可恢复。删除当前版本后，请手动选择其他就绪版本。`,
  );
  if (!ok) return;
  deletingId.value = d.id;
  error.value = "";
  deleteNotice.value = "";
  try {
    const res = await api.deleteDocument(d.id);
    await refreshDocs();
    // 把后端返回的清理明细展示出来；并提醒：已删文档的历史回答仍在会话记录里，不会自动失效
    const vec = res.vectors_cleared ? "向量已清理" : "⚠ 未匹配到向量（可能本就不存在）";
    const file = res.file_removed ? "物理副本已删除" : "物理副本此前已不存在";
    deleteNotice.value =
      `《${res.source}》已删除：${vec}，${file}。` +
      "注意：当前会话的历史回答不会随之失效——如需验证删除效果，请点「＋ 新会话」后重新提问。";
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    deletingId.value = null;
  }
}

async function onActivate(d: DocumentItem) {
  if (activatingId.value !== null || deletingId.value !== null) return;
  activatingId.value = d.id;
  error.value = "";
  try {
    await api.activateDocumentVersion(d.id);
    await refreshDocs();
    deleteNotice.value = `《${d.original_filename || d.source}》已切换至 v${d.version}。请开启新会话验证当前内容。`;
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    activatingId.value = null;
  }
}

function ensurePolling() {
  if (pollTimer) return;
  pollTimer = setInterval(pollActive, 1500);
}

function stopPollingIfIdle() {
  if (pollTimer && !tasks.value.some((t) => ACTIVE.has(t.status))) {
    clearInterval(pollTimer);
    pollTimer = null;
    refreshDocs();
  }
}

async function pollActive() {
  const active = tasks.value.filter((t) => ACTIVE.has(t.status));
  for (const t of active) {
    try {
      const st = await api.getIngestStatus(t.task_id);
      t.status = st.status;
      t.progress = st.progress;
      t.error = st.error;
      t.document_id = st.document_id;
      t.version = st.version;
      t.original_filename = st.original_filename;
      t.is_current = st.is_current;
    } catch (e) {
      t.status = "failed";
      t.error = (e as Error).message;
    }
  }
  stopPollingIfIdle();
}

function onPick(ev: Event) {
  const target = ev.target as HTMLInputElement;
  picked.value = target.files && target.files[0] ? target.files[0] : null;
  error.value = "";
}

async function onUpload() {
  if (!picked.value || uploading.value) return;
  uploading.value = true;
  error.value = "";
  const file = picked.value;
  try {
    const res = await api.uploadDocument(file);
    tasks.value.unshift({
      task_id: res.task_id,
      filename: res.filename,
      document_id: null,
      status: res.status || "queued",
      progress: 0,
      error: null,
    });
    ensurePolling();
    picked.value = null;
    if (fileInput.value) fileInput.value.value = "";
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    uploading.value = false;
  }
}

function close() {
  emit("close");
}

watch(
  () => props.open,
  (v) => {
    if (v) {
      deleteNotice.value = "";
      refreshDocs();
    }
  },
);

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer);
});

function badgeClass(status: string) {
  return `badge badge-${status}`;
}
</script>

<template>
  <div v-if="open" class="overlay" @click.self="close">
    <aside class="panel">
      <header class="panel-head">
        <h2>📄 文档入库</h2>
        <button class="close" @click="close">✕</button>
      </header>

      <section class="upload-box">
        <input
          ref="fileInput"
          type="file"
          accept=".pdf,.docx,.xlsx,.md,.txt"
          @change="onPick"
        />
        <button class="upload" :disabled="!picked || uploading" @click="onUpload">
          {{ uploading ? "上传中…" : "上传并入库" }}
        </button>
        <p class="hint">支持 pdf / docx / xlsx / md / txt。同名文件形成版本链，新版本入库成功后生效；重复内容复用已有版本。表格版本用于知识检索，暂不发布到 SQL 查询。</p>
        <p v-if="error" class="err">{{ error }}</p>
      </section>

      <section class="tasks">
        <h3>本次入库任务</h3>
        <p v-if="tasks.length === 0" class="empty">暂无任务</p>
        <ul>
          <li v-for="t in tasks" :key="t.task_id" class="task">
            <div class="task-top">
              <span class="fname">{{ t.original_filename || t.filename }} <span v-if="t.version">v{{ t.version }}</span></span>
              <span :class="badgeClass(t.status)">{{ t.status }}</span>
            </div>
            <div class="progress">
              <div class="bar" :style="{ width: t.progress + '%' }"></div>
            </div>
            <div class="task-meta">
              <span>{{ t.progress }}%</span>
              <span v-if="t.error" class="err">· {{ t.error }}</span>
            </div>
          </li>
        </ul>
      </section>

      <section class="docs">
        <h3>知识库已登记文档</h3>
        <p v-if="deleteNotice" class="notice">{{ deleteNotice }}</p>
        <p v-if="docs.length === 0" class="empty">暂无文档</p>
        <ul>
          <li v-for="d in docs" :key="d.id" class="doc">
            <div class="doc-main">
              <span class="fname">{{ d.original_filename || d.source }} <span v-if="d.version">v{{ d.version }} · {{ d.is_current ? "当前" : "历史" }}</span></span>
              <span class="dim">{{ d.format }} · {{ d.chunk_count }} 块 · {{ d.status }}</span>
            </div>
            <button v-if="d.document_key && d.status === 'ready' && !d.is_current" class="activate"
              :disabled="activatingId !== null || deletingId !== null" @click="onActivate(d)">
              {{ activatingId === d.id ? "切换中…" : "设为当前" }}
            </button>
            <button class="del" :disabled="deletingId !== null || activatingId !== null" @click="onDelete(d)">
              {{ deletingId === d.id ? "删除中…" : "删除" }}
            </button>
          </li>
        </ul>
      </section>
    </aside>
  </div>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(2, 6, 23, 0.55);
  backdrop-filter: blur(3px);
  display: flex;
  justify-content: flex-end;
  z-index: 50;
}

.panel {
  width: 420px;
  max-width: 92vw;
  height: 100%;
  overflow-y: auto;
  background: rgba(15, 23, 42, 0.92);
  border-left: 1px solid var(--border);
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.panel-head h2 {
  margin: 0;
  font-size: 18px;
}

.close {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--muted);
  border-radius: 10px;
  width: 32px;
  height: 32px;
  cursor: pointer;
}

.upload-box {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  background: rgba(30, 41, 59, 0.5);
}

.upload-box input[type="file"] {
  color: var(--muted);
  font-size: 13px;
}

.upload {
  background: linear-gradient(135deg, #6366f1, #06b6d4);
  color: #fff;
  border: none;
  border-radius: 12px;
  padding: 10px 14px;
  cursor: pointer;
  font-size: 14px;
}

.upload:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.hint {
  font-size: 12px;
  color: var(--muted);
  margin: 0;
}

.err {
  color: var(--danger);
  font-size: 12px;
  margin: 0;
}

.tasks h3,
.docs h3 {
  font-size: 14px;
  margin: 0 0 10px;
  color: var(--fg);
}

.empty {
  color: var(--muted);
  font-size: 13px;
}

ul {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.task {
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--card);
}

.task-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.fname {
  font-size: 13px;
  word-break: break-all;
}

.badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 9999px;
  border: 1px solid var(--border);
  white-space: nowrap;
}

.badge-queued {
  color: #fbbf24;
}
.badge-running {
  color: #38bdf8;
}
.badge-done {
  color: #22c55e;
}
.badge-failed {
  color: var(--danger);
}

.progress {
  height: 6px;
  border-radius: 9999px;
  background: rgba(148, 163, 184, 0.2);
  overflow: hidden;
}

.bar {
  height: 100%;
  background: linear-gradient(90deg, #6366f1, #06b6d4);
  transition: width 0.4s ease;
}

.task-meta {
  display: flex;
  gap: 6px;
  font-size: 12px;
  color: var(--muted);
  margin-top: 6px;
}

.doc {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 10px;
  background: rgba(30, 41, 59, 0.4);
}

.doc-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.del {
  flex-shrink: 0;
  border: 1px solid var(--border);
  background: rgba(239, 68, 68, 0.12);
  color: var(--danger);
  border-radius: 8px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
}

.activate {
  flex-shrink: 0;
  border: 1px solid var(--border);
  background: rgba(99, 102, 241, 0.12);
  color: var(--fg);
  border-radius: 8px;
  padding: 4px 8px;
  font-size: 12px;
  cursor: pointer;
}

.del:hover:not(:disabled) {
  background: rgba(239, 68, 68, 0.24);
}

.del:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.dim {
  font-size: 12px;
  color: var(--muted);
}

.notice {
  font-size: 12px;
  line-height: 1.5;
  color: #fbbf24;
  background: rgba(251, 191, 36, 0.08);
  border: 1px solid rgba(251, 191, 36, 0.25);
  border-radius: 10px;
  padding: 8px 10px;
  margin: 0 0 10px;
}
</style>
