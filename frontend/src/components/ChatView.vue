<script setup lang="ts">
import { ref, nextTick, onMounted, watch, computed } from "vue";
import { useChatStore } from "../stores/chat";
import MessageBubble from "./MessageBubble.vue";

const chat = useChatStore();

const input = ref("");
const listEl = ref<HTMLElement | null>(null);

const streamingActive = computed(() =>
  chat.messages.some((m) => m.streaming && m.content.length > 0)
);


onMounted(() => {
  chat.ensureSession();
});


watch(
  () => [chat.messages.length, chat.status],
  async () => {
    await nextTick();

    if (listEl.value) {
      listEl.value.scrollTop = listEl.value.scrollHeight;
    }
  }
);


/** 真正发送消息（唯一入口） */
async function send(text: string) {
  if (!text.trim() || chat.isLoading) return;

  input.value = "";

  // 按当前通道分发：agent 模式会额外产出执行轨迹与通道徽标
  await chat.send(text);
}

/** 输入框/发送按钮的处理器：不接受参数（避免与 DOM 事件签名冲突） */
async function onSubmit() {
  await send(input.value);
}

/** 示例问题点击：带预设文本 */
async function onSuggestion(text: string) {
  await send(text);
}


function onStop() {
  chat.stopStream();
}


async function onNewSession() {
  await chat.resetSession();
}

</script>


<template>

<section class="chat-view">


  <!-- 消息区域 -->
  <div ref="listEl" class="message-list">


    <!-- 欢迎页 -->
    <div
      v-if="chat.messages.length === 0 && !chat.isLoading"
      class="welcome"
    >

      <div class="welcome-icon">
        🧠
      </div>

      <h2>
        Personal RAG Assistant
      </h2>

      <p>
        基于你的个人知识库进行智能问答
      </p>

      <!-- Step 2：单一入口。意图（文档 / 数据 / 闲聊 / 混合）由后端路由判定，
           用户不再需要选择"走哪条路"—— 执行轨迹会显示实际走了哪条。 -->
      <p class="mode-hint">
        🤖 自动判定：文档检索 · 数据统计 · 闲聊
      </p>


      <div class="suggestions">

        <!-- 这几条刻意覆盖三条不同路径：文档检索 / 数据统计 / 口径澄清 -->
        <div class="suggestion" @click="onSuggestion('路由器的质保期是多久？')">
          📚 文档：路由器的质保期是多久？
        </div>

        <div class="suggestion" @click="onSuggestion('员工表里 Sales 部门和 Engineering 部门各有多少人？两者相差多少人？')">
          📊 统计：两个部门各多少人、差多少？
        </div>

        <div class="suggestion" @click="onSuggestion('哪个部门人数最多？')">
          📊 统计：哪个部门人数最多？
        </div>

      </div>

    </div>



    <!-- 消息 -->
    <MessageBubble
      v-for="(m,i) in chat.messages"
      :key="i"
      :role="m.role"
      :content="m.content"
      :sources="m.sources"
      :hidden-count="m.hiddenCount"
      :streaming="m.streaming"
      :trace="m.trace"
      :channel="m.channel"
      :clarify="m.clarify"
      :stats="m.stats"
    />



    <!-- 思考动画 -->
    <div
      v-if="chat.isLoading && !streamingActive"
      class="thinking"
    >

      <div class="thinking-avatar">
        🤖
      </div>


      <div class="thinking-box">

        <span></span>
        <span></span>
        <span></span>

        <em>
          Agent 正在分析并选择工具...
        </em>

      </div>

    </div>


  </div>




  <!-- 错误 -->
  <div
    v-if="chat.error"
    class="error-bar"
  >
    {{ chat.error }}
  </div>




  <!-- 输入区域 -->
  <div class="composer">


    <div class="composer-tools">


      <!-- Top-K 滑杆已移除：对话入口 /agent/stream 不接受 top_k，
           检索条数由服务端统一决定（原先它只喂 /chat 的请求体）。 -->
      <button
        class="new-session"
        @click="onNewSession"
      >
        ＋ 新会话
      </button>


    </div>




    <div class="input-box">


      <textarea
        v-model="input"
        rows="2"
        placeholder="输入你的问题..."
        @keydown.enter.exact.prevent="onSubmit"
      />


      <button
        v-if="!chat.isLoading"
        class="send"
        :disabled="!input.trim()"
        @click="onSubmit"
      >
        🚀
      </button>


      <button
        v-else
        class="stop"
        @click="onStop"
      >
        ■
      </button>


    </div>


  </div>



</section>

</template>



<style scoped>

.chat-view {

height:100%;

display:flex;

flex-direction:column;

}



.message-list {

flex:1;

overflow-y:auto;

padding:32px;

display:flex;

flex-direction:column;

gap:18px;

}



/* 欢迎区域 */

.welcome {

margin:auto;

text-align:center;

color:var(--muted);

}



.welcome-icon {

font-size:52px;

margin-bottom:15px;

}



.welcome h2 {

font-size:26px;

color:var(--fg);

margin-bottom:10px;

}



.welcome p {

font-size:15px;

}



.suggestions {

display:flex;

gap:12px;

margin-top:30px;

justify-content:center;

flex-wrap:wrap;

}



/* 单一入口说明（Step 2 移除模式切换后留下的提示文字） */

.mode-hint {

margin-top:18px;

font-size:13px;

color:var(--muted);

}



.suggestion {

padding:12px 18px;

background:rgba(255,255,255,.08);

border:1px solid var(--border);

border-radius:18px;

font-size:13px;

cursor:pointer;

transition:all .15s;

}



.suggestion:hover {

border-color:#2563eb;

color:#2563eb;

transform:translateY(-1px);

}




/* AI思考 */

.thinking {

display:flex;

gap:12px;

align-items:center;

}



.thinking-avatar {

font-size:28px;

}



.thinking-box {

padding:12px 18px;

border-radius:18px;

background:var(--card);

display:flex;

align-items:center;

gap:6px;

}



.thinking-box span {

width:6px;

height:6px;

background:var(--primary);

border-radius:50%;

animation:pulse 1s infinite;

}



.thinking-box span:nth-child(2){

animation-delay:.2s;

}


.thinking-box span:nth-child(3){

animation-delay:.4s;

}


@keyframes pulse {

50%{

opacity:.2;

}

}



.thinking-box em {

font-style:normal;

font-size:13px;

color:var(--muted);

}




.error-bar {

padding:10px 20px;

background:#fee2e2;

color:#dc2626;

}




.composer {

padding:15px 25px 25px;

border-top:1px solid var(--border);

background:rgba(0,0,0,.15);

}



.composer-tools {

display:flex;

justify-content:space-between;

align-items:center;

margin-bottom:12px;

}



.new-session {

border:1px solid var(--primary);

background:var(--primary);

color:#fff;

font-size:13px;

padding:8px 15px;

border-radius:12px;

cursor:pointer;

transition:all .15s;

}



.new-session:hover {

background:var(--primary-light);

border-color:var(--primary-light);

}



.input-box {

display:flex;

gap:12px;

background:var(--card);

padding:10px;

border-radius:20px;

border:1px solid var(--border);

}



textarea {

flex:1;

resize:none;

background:transparent;

border:none;

outline:none;

font-size:15px;

}



.send,
.stop {

width:46px;

height:46px;

border-radius:50%;

border:none;

cursor:pointer;

font-size:18px;

}



.send {

background:var(--primary);

color:white;

}



.send:disabled {

opacity:.4;

}



.stop {

background:#ef4444;

color:white;

}


</style>