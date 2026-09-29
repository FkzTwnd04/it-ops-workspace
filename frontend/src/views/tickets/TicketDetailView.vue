<template>
  <div v-if="detail" class="page">
    <el-card shadow="never">
      <div class="title-row">
        <div>
          <el-button text :icon="ArrowLeft" @click="router.push('/tickets')">返回</el-button>
          <span class="no">{{ t.ticket_no }}</span>
          <span class="title">{{ t.title }}</span>
        </div>
        <div class="tags">
          <el-tag :type="STATUS_META[t.status].type" effect="dark">{{ STATUS_META[t.status].label }}</el-tag>
          <el-tag :type="priorityType(t.priority)" effect="plain">{{ t.priority }}</el-tag>
          <el-tag v-if="t.category" type="info">{{ CATEGORY_LABELS[t.category] ?? t.category }}</el-tag>
          <el-tag v-if="t.fallback_used" type="danger" effect="plain">已降级</el-tag>
          <el-tag v-if="t.reopen_count" type="warning" effect="plain">重新处理 {{ t.reopen_count }} 次</el-tag>
        </div>
      </div>

      <el-steps :active="stepIndex" finish-status="success" :process-status="t.status === 'ESCALATED' ? 'error' : 'process'"
                align-center class="steps">
        <el-step v-for="s in steps" :key="s.key" :title="s.label" />
      </el-steps>

      <el-descriptions :column="3" size="small" border>
        <el-descriptions-item label="提单人">{{ t.requester_name ?? '—' }}</el-descriptions-item>
        <el-descriptions-item label="相关系统">{{ t.host || '—' }}</el-descriptions-item>
        <el-descriptions-item label="SLA 截止">
          <span :class="{ overdue: overdue }">{{ fmt(t.sla_due_at) }}{{ overdue ? '（已超时）' : '' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="提交时间">{{ fmt(t.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="检索置信度">
          {{ t.retrieval_confidence == null ? '—' : t.retrieval_confidence.toFixed(2) }}
        </el-descriptions-item>
        <el-descriptions-item label="租户">{{ t.tenant_id }}</el-descriptions-item>
        <el-descriptions-item label="问题描述" :span="3">
          <span class="pre">{{ t.description }}</span>
        </el-descriptions-item>
        <el-descriptions-item v-if="t.escalation_reason" label="转人工原因" :span="3">
          <span class="warn">{{ t.escalation_reason }}</span>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-row :gutter="16">
      <el-col :span="14">
        <AgentProgress ref="progressRef" :streaming="streaming" />
      </el-col>

      <el-col :span="10" class="side">
        <!-- 未处理 -->
        <el-card v-if="canRun" shadow="never">
          <p class="hint">工单还没有交给 Agent 处理。</p>
          <el-button type="primary" :loading="acting" @click="run">交给 Agent 处理</el-button>
        </el-card>

        <!-- 待审批 -->
        <template v-if="detail.pending === 'approval'">
          <ApprovalCard v-if="auth.isOps && pendingApprovals.length" :items="pendingApprovals" @decided="afterAction" />
          <el-alert v-else type="warning" :closable="false" show-icon
                    title="Agent 申请执行高危操作，正在等待运维审批" />
        </template>

        <!-- 待用户确认 -->
        <el-card v-if="detail.pending === 'user_confirm' && (isRequester || auth.isOps)" shadow="never" class="confirm">
          <template #header>问题解决了吗？</template>
          <p class="hint">Agent 已给出处理方案，请按方案操作后确认。确认"没解决"会让 Agent 重新诊断一次，再不行转人工。</p>
          <el-input v-model="feedback" type="textarea" :rows="2" placeholder="没解决的话，说说现在的情况（可选）" />
          <div class="btns">
            <el-button type="success" :loading="acting" @click="confirm(true)">已解决，关闭工单</el-button>
            <el-button type="warning" plain :loading="acting" @click="confirm(false)">没解决</el-button>
          </div>
        </el-card>

        <!-- 待人工处理 -->
        <el-card v-if="detail.pending === 'human_resolve'" shadow="never" class="human">
          <template #header>人工处理</template>
          <template v-if="auth.isOps">
            <el-input v-model="humanSummary" type="textarea" :rows="3" placeholder="处理结果（结案时必填）" />
            <el-input v-model="humanComment" placeholder="退回 Agent 时的补充说明（可选）" style="margin-top: 8px" />
            <div class="btns">
              <el-button type="primary" :loading="acting" @click="humanResolve('resolve')">处理完成，结案</el-button>
              <el-button :loading="acting" @click="humanResolve('retry')">补充信息，退回 Agent</el-button>
            </div>
          </template>
          <p v-else class="hint">工单已转给运维人员处理，请耐心等待。</p>
        </el-card>

        <!-- 结论 -->
        <el-card v-if="r" shadow="never">
          <template #header>
            <div class="head">
              <span>处理结论</span>
              <span>
                <el-tag v-if="r.resolved_by" size="small" type="warning">人工：{{ r.resolved_by }}</el-tag>
                <el-tag v-else-if="r.resolved" size="small" type="success">Agent 判定可解决</el-tag>
                <el-tag v-else size="small" type="danger">Agent 判定需人工</el-tag>
                <el-tag v-if="r.confidence != null" size="small" type="info" style="margin-left: 4px">
                  把握 {{ r.confidence.toFixed(2) }}
                </el-tag>
              </span>
            </div>
          </template>
          <div v-if="r.human_summary" class="block"><b>人工处理：</b>{{ r.human_summary }}</div>
          <div v-if="r.root_cause" class="block"><b>根因：</b>{{ r.root_cause }}</div>
          <div v-if="r.summary" class="block"><b>说明：</b>{{ r.summary }}</div>
          <div v-if="r.steps_taken?.length" class="block">
            <b>已执行：</b>
            <ol><li v-for="(s, i) in r.steps_taken" :key="i">{{ s }}</li></ol>
          </div>
          <div v-if="r.user_actions?.length" class="block">
            <b>需要您操作：</b>
            <ol><li v-for="(s, i) in r.user_actions" :key="i">{{ s }}</li></ol>
          </div>
          <div v-if="r.references?.length" class="block">
            <b>知识库参考方案（降级）：</b>
            <div v-for="(ref, i) in r.references" :key="i" class="ref">
              <div class="ref-src">{{ ref.source }} · {{ ref.score.toFixed(2) }}</div>
              <div class="pre">{{ ref.content }}</div>
            </div>
          </div>
        </el-card>

        <el-card shadow="never">
          <el-tabs v-model="tab">
            <el-tab-pane :label="`状态流转 ${detail.events.length}`" name="events">
              <el-timeline>
                <el-timeline-item v-for="e in detail.events" :key="e.id" :timestamp="fmt(e.created_at)" placement="top">
                  <b>{{ e.from_status ? STATUS_META[e.from_status].label + ' → ' : '' }}{{ STATUS_META[e.to_status].label }}</b>
                  <span class="actor">{{ e.actor }}</span>
                  <div v-if="e.reason" class="reason">{{ e.reason }}</div>
                </el-timeline-item>
              </el-timeline>
            </el-tab-pane>
            <el-tab-pane :label="`检索 ${detail.retrievals.length}`" name="retrievals">
              <div v-for="(rt, i) in detail.retrievals" :key="i" class="retrieval">
                <div class="rt-head">
                  <el-tag size="small">{{ rt.doc_type === 'ticket' ? '历史工单' : '运维手册' }}</el-tag>
                  <el-tag size="small" type="info">{{ rt.strategy }}</el-tag>
                  <span :class="rt.confidence >= 0.6 ? 'ok' : 'warn'">置信度 {{ rt.confidence.toFixed(2) }}</span>
                </div>
                <div class="rt-q">查询：{{ rt.query }}</div>
                <div v-for="(d, j) in rt.docs.slice(0, 3)" :key="j" class="rt-doc">
                  {{ d.source_name }} · {{ d.score?.toFixed(2) }}
                </div>
              </div>
              <el-empty v-if="!detail.retrievals.length" :image-size="50" />
            </el-tab-pane>
            <el-tab-pane :label="`运维操作 ${detail.ops_actions.length}`" name="ops">
              <div v-for="(o, i) in detail.ops_actions" :key="i" class="op">
                <b>{{ TOOL_LABELS[o.tool_name] ?? o.tool_name }}</b>
                <span class="actor">{{ fmt(o.executed_at) }}</span>
                <div class="reason">{{ toolArgs(o.args) }}</div>
                <div class="op-result">{{ o.result }}</div>
              </div>
              <el-empty v-if="!detail.ops_actions.length" :image-size="50" />
            </el-tab-pane>
            <el-tab-pane :label="`审批 ${detail.approvals.length}`" name="approvals">
              <div v-for="a in detail.approvals" :key="a.id" class="op">
                <b>{{ TOOL_LABELS[a.tool_name] ?? a.tool_name }}</b>
                <el-tag size="small" :type="APPROVAL_TYPE[a.status]" style="margin-left: 6px">{{ APPROVAL_LABEL[a.status] }}</el-tag>
                <div class="reason">申请参数：{{ toolArgs(a.tool_args) }}</div>
                <div v-if="a.final_args && a.status === 'edited'" class="reason">执行参数：{{ toolArgs(a.final_args) }}</div>
                <div v-if="a.reviewer_name" class="reason">
                  审批人 {{ a.reviewer_name }} · {{ fmt(a.decided_at) }}{{ a.comment ? ' · ' + a.comment : '' }}
                </div>
              </div>
              <el-empty v-if="!detail.approvals.length" :image-size="50" />
            </el-tab-pane>
          </el-tabs>
        </el-card>
      </el-col>
    </el-row>
  </div>
  <div v-else v-loading="true" style="height: 300px" />
</template>

<script setup lang="ts">
import { computed, ref, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft } from '@element-plus/icons-vue'
import {
  ticketsApi, STATUS_META, CATEGORY_LABELS, TOOL_LABELS,
  type TicketDetail, type TicketStatus,
} from '@/api/tickets'
import { useAuthStore } from '@/stores/auth'
import { useTicketStream, type ProgressEvent } from '@/composables/useTicketStream'
import AgentProgress from '@/components/ticket/AgentProgress.vue'
import ApprovalCard from '@/components/ticket/ApprovalCard.vue'
import { fmt, priorityType, toolArgs } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const detail = ref<TicketDetail | null>(null)
const progressRef = ref<InstanceType<typeof AgentProgress>>()
const acting = ref(false)
const feedback = ref('')
const humanSummary = ref('')
const humanComment = ref('')
const tab = ref('events')

const APPROVAL_LABEL = { pending: '待审批', approved: '已批准', edited: '修改后批准', rejected: '已拒绝' } as const
const APPROVAL_TYPE = { pending: 'warning', approved: 'success', edited: 'primary', rejected: 'danger' } as const

const id = computed(() => route.params.id as string)
const t = computed(() => detail.value!.ticket)
const r = computed(() => detail.value?.ticket.resolution ?? null)
const isRequester = computed(() => t.value.requester_id === auth.user?.userId)
const overdue = computed(() => !!t.value.sla_due_at && !['RESOLVED', 'CLOSED'].includes(t.value.status)
  && new Date(t.value.sla_due_at) < new Date())
const canRun = computed(() => t.value.status === 'NEW' && !detail.value?.running && !detail.value?.pending
  && !detail.value?.events.some(e => e.to_status !== 'NEW'))
const pendingApprovals = computed(() => detail.value?.approvals.filter(a => a.status === 'pending') ?? [])

const steps = computed(() => {
  const base: { key: TicketStatus; label: string }[] = [
    { key: 'NEW', label: '新建' },
    { key: 'TRIAGED', label: '已分类' },
    { key: 'IN_PROGRESS', label: 'Agent 处理' },
  ]
  const visited = new Set(detail.value?.events.map(e => e.to_status))
  if (visited.has('PENDING_APPROVAL')) base.push({ key: 'PENDING_APPROVAL', label: '高危审批' })
  if (visited.has('ESCALATED')) base.push({ key: 'ESCALATED', label: '转人工' })
  else base.push({ key: 'PENDING_CONFIRM', label: '用户确认' })
  base.push({ key: 'RESOLVED', label: '已解决' }, { key: 'CLOSED', label: '已关闭' })
  return base
})
const stepIndex = computed(() => {
  const i = steps.value.findIndex(s => s.key === t.value.status)
  if (t.value.status === 'CLOSED') return steps.value.length
  return i < 0 ? 0 : i
})

const { streaming, connect } = useTicketStream(onEvent)

function onEvent(ev: ProgressEvent) {
  progressRef.value?.push(ev)
  if (ev.type === 'status' && detail.value && ev.status !== detail.value.ticket.status) {
    detail.value.ticket.status = ev.status
    load()
  }
  if (ev.type === 'done') load()
}

async function load() {
  const { data } = await ticketsApi.detail(id.value)
  detail.value = data
}

function follow() {
  progressRef.value?.reset()
  connect(id.value)
}

async function act(fn: () => Promise<unknown>, msg: string) {
  acting.value = true
  try {
    await fn()
    ElMessage.success(msg)
    await afterAction()
  } finally {
    acting.value = false
  }
}

async function afterAction() {
  await load()
  follow()
}

const run = () => act(() => ticketsApi.run(id.value), 'Agent 开始处理')

function confirm(resolved: boolean) {
  return act(() => ticketsApi.confirm(id.value, resolved, feedback.value || undefined),
    resolved ? '感谢确认，工单已关闭' : '已反馈，Agent 将重新处理')
}

function humanResolve(action: 'resolve' | 'retry') {
  if (action === 'resolve' && !humanSummary.value.trim()) {
    ElMessage.warning('请填写处理结果')
    return
  }
  return act(() => ticketsApi.resolve(id.value, action, humanSummary.value || undefined, humanComment.value || undefined),
    action === 'resolve' ? '工单已结案' : '已退回 Agent 重新处理')
}

onMounted(async () => {
  await load()
  follow()
})
watch(id, async () => {
  detail.value = null
  await load()
  follow()
})
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: 16px; }
.title-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.no { color: #8c8c8c; margin: 0 10px 0 4px; }
.title { font-size: 18px; font-weight: 600; }
.tags { display: flex; gap: 6px; }
.steps { margin-bottom: 20px; }
.overdue, .warn { color: #f5222d; }
.ok { color: #52c41a; }
.pre { white-space: pre-wrap; }
.side { display: flex; flex-direction: column; gap: 16px; }
.hint { color: #595959; font-size: 13px; margin: 0 0 10px; }
.btns { display: flex; gap: 8px; margin-top: 12px; }
.confirm { border-left: 3px solid #52c41a; }
.human { border-left: 3px solid #fa8c16; }
.head { display: flex; justify-content: space-between; align-items: center; }
.block { font-size: 13px; margin-bottom: 8px; line-height: 1.7; }
.block ol { margin: 4px 0 0; padding-left: 20px; }
.ref { background: #fafafa; border-radius: 4px; padding: 6px 8px; margin-top: 6px; font-size: 12px; }
.ref-src { color: #1677ff; margin-bottom: 2px; }
.actor { color: #8c8c8c; font-size: 12px; margin-left: 8px; }
.reason { color: #595959; font-size: 12px; margin-top: 2px; word-break: break-all; }
.retrieval, .op { padding: 8px 0; border-bottom: 1px dashed #f0f0f0; font-size: 13px; }
.rt-head { display: flex; gap: 6px; align-items: center; }
.rt-q { color: #595959; font-size: 12px; margin-top: 4px; }
.rt-doc { color: #8c8c8c; font-size: 12px; }
.op-result { font-size: 12px; background: #f6ffed; padding: 4px 8px; border-radius: 4px; margin-top: 4px; }
</style>
