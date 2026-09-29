<template>
  <el-card shadow="never" class="approval-card">
    <template #header>
      <div class="head">
        <div>
          <RouterLink v-if="showTicket" :to="`/tickets/${items[0].ticket_id}`" class="ticket-link">
            {{ items[0].ticket_no }} · {{ items[0].title }}
          </RouterLink>
          <span v-else>待审批的高危操作</span>
          <el-tag v-if="showTicket" size="small" :type="priorityType(items[0].priority)" effect="plain" style="margin-left: 8px">
            {{ items[0].priority }}
          </el-tag>
        </div>
        <span class="time">申请于 {{ fmt(items[0].requested_at) }}</span>
      </div>
    </template>

    <div v-for="a in items" :key="a.id" class="action">
      <div class="action-head">
        <el-tag type="danger" effect="dark" size="small">高危</el-tag>
        <b>{{ TOOL_LABELS[a.tool_name] ?? a.tool_name }}</b>
        <code>{{ a.tool_name }}</code>
      </div>
      <div class="args">参数：{{ toolArgs(a.tool_args) }}</div>
      <div v-if="a.risk_note" class="risk">{{ a.risk_note }}</div>

      <el-radio-group v-model="state[a.id].type" size="small" class="decision">
        <el-radio-button value="approve">批准</el-radio-button>
        <el-radio-button value="edit">修改参数后批准</el-radio-button>
        <el-radio-button value="reject">拒绝</el-radio-button>
      </el-radio-group>

      <el-input
        v-if="state[a.id].type === 'edit'"
        v-model="state[a.id].argsText"
        type="textarea"
        :rows="4"
        class="mono"
        placeholder="修改后的参数（JSON）"
      />
      <el-input v-model="state[a.id].comment" placeholder="审批意见（拒绝时必填）" size="small" class="comment" />
    </div>

    <div class="footer">
      <el-button type="primary" :loading="submitting" @click="submit">提交审批决定</el-button>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ticketsApi, TOOL_LABELS, type Approval, type Decision } from '@/api/tickets'
import { fmt, priorityType, toolArgs } from '@/utils/format'

const props = defineProps<{ items: Approval[]; showTicket?: boolean }>()
const emit = defineEmits<{ decided: [] }>()

type Row = { type: Decision['type']; argsText: string; comment: string }
const state = reactive<Record<string, Row>>({})
const submitting = ref(false)

watch(() => props.items, items => {
  for (const a of items) {
    if (!state[a.id]) state[a.id] = { type: 'approve', argsText: JSON.stringify(a.tool_args, null, 2), comment: '' }
  }
}, { immediate: true })

async function submit() {
  const decisions: Decision[] = []
  for (const a of props.items) {
    const s = state[a.id]
    if (s.type === 'reject' && !s.comment.trim()) {
      ElMessage.warning('拒绝时请填写审批意见')
      return
    }
    const d: Decision = { approval_id: a.id, type: s.type, comment: s.comment || undefined }
    if (s.type === 'edit') {
      try {
        d.args = JSON.parse(s.argsText)
      } catch {
        ElMessage.warning(`${TOOL_LABELS[a.tool_name] ?? a.tool_name} 的参数不是合法 JSON`)
        return
      }
    }
    decisions.push(d)
  }
  submitting.value = true
  try {
    await ticketsApi.decide(props.items[0].ticket_id, decisions)
    ElMessage.success('审批已提交，Agent 继续处理')
    emit('decided')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.approval-card { border-left: 3px solid #fa8c16; }
.head { display: flex; justify-content: space-between; align-items: center; }
.ticket-link { color: #1677ff; text-decoration: none; font-weight: 600; }
.time { font-size: 12px; color: #8c8c8c; }
.action { padding: 10px 0; border-bottom: 1px dashed #f0f0f0; display: flex; flex-direction: column; gap: 8px; }
.action:last-of-type { border-bottom: none; }
.action-head { display: flex; align-items: center; gap: 8px; }
.action-head code { color: #8c8c8c; font-size: 12px; }
.args { font-size: 13px; color: #262626; word-break: break-all; }
.risk { font-size: 12px; color: #d4380d; background: #fff2e8; padding: 6px 10px; border-radius: 4px; }
.mono :deep(textarea) { font-family: Consolas, monospace; font-size: 12px; }
.footer { display: flex; justify-content: flex-end; margin-top: 8px; }
</style>
