<script setup lang="ts">
// AgentTrace.vue - Agent 执行轨迹可视化
//
// 为什么需要它：SQL 通道的价值不只在"答得对"，还在**可解释**——
// 用户能看到「为什么走了 SQL」「执行了哪条语句」「返回几行」。
// 这正是本项目区别于普通 RAG 项目的地方，也是演示时最该展示的部分。

import { ref } from "vue";
import type { TraceStep } from "../api/types";

const props = defineProps<{
  steps: TraceStep[];
  collapsed?: boolean;
}>();

const open = ref(!props.collapsed);

const iconOf = (kind: TraceStep["kind"]) => {
  switch (kind) {
    case "route":
      return "🧭";
    case "tool":
      return "🔧";
    case "sql":
      return "📊";
    case "clarify":
      return "❓";
    case "degraded":
      return "⚠️";
    case "rewrite":
      return "✏️";
    default:
      return "•";
  }
};
</script>

<template>
  <div v-if="steps.length" class="trace">
    <button class="trace-head" @click="open = !open">
      <span class="trace-caret">{{ open ? "▾" : "▸" }}</span>
      <span class="trace-title">执行轨迹</span>
      <span class="trace-count">{{ steps.length }} 步</span>
    </button>

    <ol v-show="open" class="trace-body">
      <li v-for="(s, i) in steps" :key="i" class="trace-step" :class="`kind-${s.kind}`">
        <span class="ts-icon">{{ iconOf(s.kind) }}</span>
        <div class="ts-main">
          <div class="ts-label">
            {{ s.label }}
            <span v-if="s.ok === false" class="ts-bad">失败</span>
          </div>
          <div v-if="s.detail" class="ts-detail">{{ s.detail }}</div>

          <!-- SQL 步骤展示语句与结果规模：可解释性的核心 -->
          <div v-if="s.kind === 'sql' && s.sql" class="ts-sql">
            <pre class="ts-sql-code">{{ s.sql }}</pre>
            <div class="ts-sql-meta">
              <span>{{ s.rowCount }} 行</span>
              <span>{{ s.elapsedMs }}ms</span>
              <span v-if="s.truncated" class="ts-truncated">⚠️ 已截断，非全部数据</span>
            </div>
          </div>
        </div>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.trace {
  margin-bottom: 8px;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
  background: var(--bg-subtle, #f9fafb);
  font-size: 12px;
  overflow: hidden;
}
.trace-head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 7px 10px;
  background: none;
  border: none;
  cursor: pointer;
  color: var(--text-muted, #6b7280);
  font-size: 12px;
  text-align: left;
}
.trace-head:hover {
  background: var(--bg-hover, #f3f4f6);
}
.trace-caret {
  width: 10px;
}
.trace-title {
  font-weight: 600;
}
.trace-count {
  margin-left: auto;
  opacity: 0.75;
}
.trace-body {
  margin: 0;
  padding: 4px 10px 10px 10px;
  list-style: none;
}
.trace-step {
  display: flex;
  gap: 8px;
  padding: 4px 0;
  border-top: 1px dashed var(--border, #e5e7eb);
}
.trace-step:first-child {
  border-top: none;
}
.ts-icon {
  flex: 0 0 16px;
}
.ts-main {
  flex: 1;
  min-width: 0;
}
.ts-label {
  color: var(--text, #111827);
  font-weight: 500;
}
.ts-bad {
  margin-left: 6px;
  color: #dc2626;
  font-weight: 600;
}
.ts-detail {
  margin-top: 2px;
  color: var(--text-muted, #6b7280);
  word-break: break-word;
}
.ts-sql {
  margin-top: 6px;
}
.ts-sql-code {
  margin: 0;
  padding: 8px;
  background: #0f172a;
  color: #e2e8f0;
  border-radius: 6px;
  font-size: 11.5px;
  line-height: 1.5;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
}
.ts-sql-meta {
  display: flex;
  gap: 12px;
  margin-top: 4px;
  color: var(--text-muted, #6b7280);
}
.ts-truncated {
  color: #b45309;
  font-weight: 600;
}
</style>
