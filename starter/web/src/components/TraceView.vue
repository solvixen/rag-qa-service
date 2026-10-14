<script setup>
import { computed, ref, watch } from "vue";
import { getTrace } from "../api.js";

const props = defineProps({ traceId: { type: String, default: "" } });

const inputId = ref("");
const trace = ref(null);
const errMsg = ref("");
const loading = ref(false);

watch(
  () => props.traceId,
  (v) => {
    if (v) {
      inputId.value = v;
      load();
    }
  }
);

async function load() {
  const id = (inputId.value || "").trim();
  if (!id || loading.value) return;
  loading.value = true;
  errMsg.value = "";
  trace.value = null;
  try {
    trace.value = await getTrace(id);
  } catch (err) {
    errMsg.value = err.message;
  } finally {
    loading.value = false;
  }
}

const steps = computed(() => trace.value?.steps || []);
const errors = computed(() => trace.value?.errors || []);
const llmCalls = computed(() => trace.value?.llm_calls || []);

/* 顶部概览：一次问答的关键结论，不用展开任何 JSON 就能看完。 */
const overview = computed(() => {
  const t = trace.value;
  if (!t) return null;
  const answered = steps.value.find((s) =>
    ["answer_live", "answer_mock", "answer_live_failed"].includes(s.step)
  );
  return {
    question: t.question,
    totalMs: t.total_ms,
    stepCount: steps.value.length,
    llmCount: llmCalls.value.length,
    errorCount: errors.value.length,
    answerType: answered?.detail?.answer_type || "",
    failed: Boolean(answered && answered.step === "answer_live_failed"),
    degraded: steps.value.some((s) => s.step === "tool_loop_fallback"),
  };
});

/* 检索诊断单独分区：评审要看的就是"检索到了哪些片段、各自多少分、哪些被过滤了"。 */
const retrievalSteps = computed(() => steps.value.filter((s) => s?.detail?.retrieval));

function ms(step) {
  const v = step.took_ms ?? step.elapsed_ms ?? step.duration_ms;
  return v === undefined || v === null ? "" : Math.round(v) + " ms";
}

/* 每一步的一句话摘要——把原始 JSON 里最重要的那点先讲成人话。 */
function headline(step) {
  const d = step.detail || {};
  switch (step.step) {
    case "plan": {
      const bits = [];
      if (d.intent) bits.push("意图=" + d.intent);
      if (d.metric) bits.push("指标=" + d.metric);
      if (d.store_id) bits.push("门店=" + d.store_id);
      if (d.product_id) bits.push("商品=" + d.product_id);
      if (d.window) bits.push("区间=" + d.window.join(" ~ "));
      return bits.join(" · ");
    }
    case "tool": {
      const r = d.retrieval;
      if (r) {
        const top = (r.hits || [])[0];
        const filtered = (r.filtered || []).length;
        return (
          "检索「" + (r.query || "") + "」· 命中 " + (r.hits || []).length + " 段" +
          (top ? " · 最高 " + top.score + " 分" : "") +
          (filtered ? " · 过滤掉 " + filtered + " 篇" : "")
        );
      }
      return "调用工具 " + (d.tool || "?");
    }
    case "tool_arguments_invalid":
      return "工具参数不是合法 JSON：" + (d.tool || "");
    case "number_check_failed":
      return "有 " + ((d.unmatched || []).length) + " 个数字在工具结果里找不到，已回退模板作答";
    case "tool_loop_fallback":
      return (
        "模型轮数用尽仍未收敛，改用模板作答（取数 " + (d.evidence_calls ?? 0) +
        " 次 / 检索 " + (d.retrieved_queries ?? 0) + " 次）"
      );
    case "answer_live":
      return "模型作答 · answer_type=" + (d.answer_type || "?");
    case "answer_mock":
      return "规则引擎作答 · answer_type=" + (d.answer_type || "?");
    case "answer_live_failed":
      return "模型调用失败 · kind=" + (d.kind || "?");
    case "response":
      return "已返回 · answer_type=" + (d.answer_type || "?");
    default:
      return "";
  }
}

function pretty(obj) {
  if (obj === null || obj === undefined) return "";
  if (typeof obj === "string") return obj;
  return JSON.stringify(obj, null, 2);
}
</script>

<template>
  <div>
    <div class="card" style="display: flex; gap: 12px; align-items: center">
      <input
        type="text"
        v-model="inputId"
        placeholder="输入 trace_id（从问答页的“调试追踪”链接自动带入）"
        style="flex: 1"
        @keyup.enter="load"
      />
      <button class="primary" :disabled="loading || !inputId.trim()" @click="load">
        {{ loading ? "加载中…" : "查看追踪" }}
      </button>
    </div>

    <div class="err-box" v-if="errMsg">{{ errMsg }}</div>

    <template v-if="trace">
      <!-- 概览 -->
      <div class="card">
        <h3>一眼看全</h3>
        <div class="kv-grid">
          <div><span class="k">问题</span><span class="v">{{ overview.question || "（空）" }}</span></div>
          <div><span class="k">总耗时</span><span class="v">{{ Math.round(overview.totalMs || 0) }} ms</span></div>
          <div>
            <span class="k">步骤 / 模型调用</span>
            <span class="v">{{ overview.stepCount }} 步 · {{ overview.llmCount }} 次</span>
          </div>
          <div><span class="k">作答类型</span><span class="v">{{ overview.answerType || "—" }}</span></div>
          <div>
            <span class="k">错误</span>
            <span class="v" :class="{ bad: overview.errorCount > 0 }">{{ overview.errorCount }}</span>
          </div>
          <div v-if="overview.failed" class="full">
            <span class="k">结果</span><span class="v bad">模型调用失败，已按降级返回</span>
          </div>
          <div v-if="overview.degraded" class="full">
            <span class="k">降级</span><span class="v bad">模型未收敛，已改用模板作答</span>
          </div>
        </div>
      </div>

      <!-- 错误 -->
      <div class="card" v-if="errors.length">
        <h3 class="bad">错误（{{ errors.length }}）</h3>
        <div v-for="(e, i) in errors" :key="i" class="err-item">
          <div class="err-line">
            <b>{{ e.where }}</b> · {{ e.type }}：{{ e.message }}
          </div>
          <details>
            <summary>堆栈</summary>
            <pre class="evidence-pre">{{ e.traceback }}</pre>
          </details>
        </div>
      </div>

      <!-- 检索诊断：分数与过滤原因 -->
      <div class="card" v-for="(step, i) in retrievalSteps" :key="'r' + i">
        <h3>检索诊断 · 第 {{ i + 1 }} 次</h3>
        <div class="kv-grid">
          <div><span class="k">查询</span><span class="v">{{ step.detail.retrieval.query || "（空）" }}</span></div>
          <div><span class="k">覆盖率</span><span class="v">{{ step.detail.retrieval.coverage ?? "—" }}</span></div>
          <div>
            <span class="k">词扩展</span>
            <span class="v">{{ (step.detail.retrieval.expansions || []).join("、") || "无" }}</span>
          </div>
          <div><span class="k">耗时</span><span class="v">{{ ms(step) || "—" }}</span></div>
        </div>

        <table class="diag">
          <thead>
            <tr><th>片段</th><th>分数</th><th>标记</th></tr>
          </thead>
          <tbody>
            <tr v-for="h in step.detail.retrieval.hits || []" :key="h.chunk_id">
              <td class="mono">{{ h.chunk_id }}</td>
              <td class="mono">{{ h.score }}</td>
              <td>
                <span v-if="h.padded" class="tag warn">凑数补齐（不参与作答）</span>
                <span v-if="(h.dropped_instructions || []).length" class="tag warn">已丢弃文档内指令</span>
                <span v-if="!h.padded && !(h.dropped_instructions || []).length" class="tag ok">正常命中</span>
              </td>
            </tr>
            <tr v-if="!(step.detail.retrieval.hits || []).length">
              <td colspan="3" class="muted">这一段没有命中任何片段</td>
            </tr>
          </tbody>
        </table>

        <div v-if="(step.detail.retrieval.filtered || []).length" class="filtered">
          <h4>被过滤掉的文档（{{ step.detail.retrieval.filtered.length }}）</h4>
          <ul>
            <li v-for="(f, j) in step.detail.retrieval.filtered" :key="j">
              <span class="mono">{{ f.doc_id }}</span>：{{ f.reason }}
            </li>
          </ul>
        </div>
        <div v-else class="muted filtered-none">没有文档被元数据过滤</div>
      </div>

      <!-- 时间线 -->
      <div class="card">
        <h3>处理步骤（{{ steps.length }} 步）</h3>
        <div class="trace-step" v-for="(step, i) in steps" :key="i">
          <div class="head">
            <span class="name">{{ i + 1 }}. {{ step.step }}</span>
            <span class="ms" v-if="ms(step)">{{ ms(step) }}</span>
          </div>
          <div class="line" v-if="headline(step)">{{ headline(step) }}</div>
          <details v-if="step.detail !== undefined && step.detail !== null">
            <summary>原始 JSON</summary>
            <pre class="evidence-pre">{{ pretty(step.detail) }}</pre>
          </details>
        </div>
      </div>

      <!-- 模型调用 -->
      <div class="card" v-if="llmCalls.length">
        <h3>大模型调用（{{ llmCalls.length }} 次）</h3>
        <div class="trace-step" v-for="(call, i) in llmCalls" :key="i">
          <div class="head">
            <span class="name">调用 {{ i + 1 }}<template v-if="call.model"> · {{ call.model }}</template></span>
            <span class="ms" v-if="call.took_ms">{{ Math.round(call.took_ms) }} ms</span>
          </div>
          <div class="line">
            finish={{ call.finish_reason || "—" }} · 工具={{ (call.tool_calls || []).join("、") || "无" }}
            · 正文 {{ call.content_chars ?? 0 }} 字
            <template v-if="call.error"> · <span class="bad">错误 {{ call.error }}</span></template>
          </div>
          <details>
            <summary>请求 / 响应（含完整提示词与原始输出）</summary>
            <pre class="evidence-pre">{{ pretty(call) }}</pre>
          </details>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.kv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 4px 18px;
  margin-top: 6px;
}
.kv-grid .full { grid-column: 1 / -1; }
.k { color: #6b7280; font-size: 12px; margin-right: 8px; }
.v { font-weight: 600; }
.bad { color: var(--red, #c0392b); }

table.diag {
  width: 100%;
  border-collapse: collapse;
  margin-top: 10px;
  font-size: 13px;
}
table.diag th,
table.diag td {
  text-align: left;
  padding: 4px 8px;
  border-bottom: 1px solid #eceff3;
}
table.diag th { color: #6b7280; font-weight: 500; background: #fafbfc; }

.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.muted { color: #9aa3ad; font-size: 12px; }

.tag {
  display: inline-block;
  padding: 0 6px;
  border-radius: 8px;
  font-size: 11px;
  line-height: 18px;
  margin-right: 4px;
}
.tag.ok { background: #e8f5ee; color: #1e7a4a; }
.tag.warn { background: #fdf3e3; color: #a06414; }

.filtered { margin-top: 10px; }
.filtered h4 { margin: 0 0 4px; font-size: 13px; color: #a06414; }
.filtered ul { margin: 0; padding-left: 18px; font-size: 13px; }
.filtered-none { margin-top: 8px; }

.line { font-size: 13px; color: #374151; margin-top: 2px; }
.err-item { margin-top: 6px; }
.err-line { font-size: 13px; }
</style>
