<template>
  <el-card shadow="never">
    <template #header>
      <div class="head">
        <span>Agent 处理过程</span>
        <span class="meta">
          <el-tag v-if="streaming && !finished" size="small" type="primary" effect="light">
            <el-icon class="spin"><Loading /></el-icon> 处理中 {{ elapsed }}
          </el-tag>
          <el-tag v-else-if="items.length" size="small" type="info">本次运行耗时 {{ elapsed }}</el-tag>
        </span>
      </div>
    </template>

    <div v-if="todos.length" class="todos">
      <div class="todos-title">处理计划（write_todos）</div>
      <div v-for="(t, i) in todos" :key="i" class="todo" :class="t.status">
        <el-icon v-if="t.status === 'completed'"><CircleCheckFilled /></el-icon>
        <el-icon v-else-if="t.status === 'in_progress'" class="spin"><Loading /></el-icon>
        <el-icon v-else><Clock /></el-icon>
        <span>{{ t.content }}</span>
      </div>
    </div>

    <el-empty v-if="!items.length" :description="streaming ? '等待 Agent 开始处理…' : '暂无本次运行的处理记录'" :image-size="60" />

    <el-timeline v-else class="timeline">
      <el-timeline-item
        v-for="(it, i) in items"
        :key="i"
        :timestamp="it.at"
        :type="it.color"
        :hollow="it.hollow"
        placement="top"
      >
        <div class="item">
          <el-tag v-if="it.agent" size="small" :type="it.agent === '主 Agent' ? 'primary' : 'success'" effect="plain">
            {{ it.agent }}
          </el-tag>
          <span class="title">{{ it.title }}</span>
        </div>
        <div v-if="it.detail" class="detail" :class="{ collapsed: !expanded[i] && it.detail.length > 160 }"
             @click="expanded[i] = !expanded[i]">
          {{ it.detail }}
        </div>
      </el-timeline-item>
    </el-timeline>
  </el-card>
</template>

<script setup lang="ts">
import { computed, reactive, ref, onBeforeUnmount } from 'vue'
import { Loading, CircleCheckFilled, Clock } from '@element-plus/icons-vue'
import { AGENT_LABELS, STATUS_META, TOOL_LABELS, type TicketStatus } from '@/api/tickets'
import type { ProgressEvent } from '@/composables/useTicketStream'
import { toolArgs } from '@/utils/format'

defineProps<{ streaming: boolean }>()

interface Item {
  at: string
  agent?: string
  title: string
  detail?: string
  color?: 'primary' | 'success' | 'warning' | 'danger' | 'info'
  hollow?: boolean
}

const STAGES: Record<string, string> = {
  start_run: '外层状态机：开始一次处理',
  run_agent: '外层状态机：Deep Agent 诊断完成',
  finalize: '外层状态机：按规则判定是否自动结案',
  await_confirm: '外层状态机：处理用户确认结果',
  await_human: '外层状态机：处理人工结论',
}
const AWAITING: Record<string, string> = {
  approval: '暂停：高危操作等待运维审批',
  user_confirm: '暂停：等待提单人确认是否解决',
  human_resolve: '暂停：已转人工，等待运维处理',
}

const items = ref<Item[]>([])
const todos = ref<{ content: string; status: string }[]>([])
const expanded = reactive<Record<number, boolean>>({})
const startTs = ref<number | null>(null)
const lastTs = ref<number | null>(null)
const finished = ref(false)
const now = ref(Date.now() / 1000)
const timer = window.setInterval(() => { now.value = Date.now() / 1000 }, 500)
onBeforeUnmount(() => window.clearInterval(timer))

const elapsed = computed(() => {
  if (startTs.value == null) return '0.0s'
  const end = finished.value ? (lastTs.value ?? startTs.value) : now.value
  return `${Math.max(0, end - startTs.value).toFixed(1)}s`
})

function agentLabel(name?: string) {
  return name ? AGENT_LABELS[name] ?? name : undefined
}

function rel(ts?: number) {
  if (ts == null || startTs.value == null) return ''
  return `+${(ts - startTs.value).toFixed(1)}s`
}

function reset() {
  items.value = []
  todos.value = []
  startTs.value = null
  lastTs.value = null
  finished.value = false
}

function push(ev: ProgressEvent) {
  if (ev.ts != null) {
    if (startTs.value == null) startTs.value = ev.ts
    lastTs.value = ev.ts
  }
  const at = rel(ev.ts)
  const add = (it: Omit<Item, 'at'>) => items.value.push({ at, ...it })
  switch (ev.type) {
    case 'status': {
      const last = [...items.value].reverse().find(i => i.title.startsWith('工单状态'))
      const label = STATUS_META[ev.status as TicketStatus]?.label ?? ev.status
      if (!last || !last.title.endsWith(label)) add({ title: `工单状态 → ${label}`, color: 'primary', hollow: true })
      break
    }
    case 'stage':
      if (STAGES[ev.stage]) add({ title: STAGES[ev.stage], color: 'info', hollow: true })
      break
    case 'todos':
      todos.value = ev.todos ?? []
      break
    case 'subagent_start':
      add({ agent: '主 Agent', title: `委派 ${agentLabel(ev.subagent)}`, detail: ev.description, color: 'success' })
      break
    case 'subagent_result':
      add({ agent: '主 Agent', title: '收到子 Agent 结果', detail: ev.content, color: 'success', hollow: true })
      break
    case 'tool_call':
      add({ agent: agentLabel(ev.agent), title: `调用 ${TOOL_LABELS[ev.tool] ?? ev.tool}`, detail: toolArgs(ev.args) })
      break
    case 'tool_result':
      add({ agent: agentLabel(ev.agent), title: `${TOOL_LABELS[ev.tool] ?? ev.tool} 返回`, detail: ev.content, hollow: true })
      break
    case 'agent_message':
      add({ agent: agentLabel(ev.agent), title: '回复', detail: ev.content })
      break
    case 'awaiting':
      add({
        title: AWAITING[ev.kind] ?? '暂停',
        detail: ev.actions?.map((a: any) => `${TOOL_LABELS[a.name] ?? a.name}（${toolArgs(a.args)}）`).join('；'),
        color: 'warning',
      })
      break
    case 'error':
      add({ title: '出错', detail: ev.message, color: 'danger' })
      break
    case 'done':
      finished.value = true
      break
  }
}

defineExpose({ push, reset })
</script>

<style scoped>
.head { display: flex; justify-content: space-between; align-items: center; }
.todos { background: #fafafa; border-radius: 6px; padding: 10px 12px; margin-bottom: 16px; }
.todos-title { font-size: 12px; color: #8c8c8c; margin-bottom: 6px; }
.todo { display: flex; align-items: center; gap: 6px; font-size: 13px; padding: 2px 0; color: #595959; }
.todo.completed { color: #52c41a; }
.todo.in_progress { color: #1677ff; }
.timeline { max-height: 640px; overflow-y: auto; padding-right: 8px; }
.item { display: flex; align-items: center; gap: 8px; }
.title { font-size: 13px; font-weight: 500; }
.detail {
  margin-top: 4px; font-size: 12px; color: #595959; background: #f7f8fa; border-radius: 4px;
  padding: 6px 8px; white-space: pre-wrap; word-break: break-all; cursor: pointer;
}
.detail.collapsed { max-height: 60px; overflow: hidden; }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
