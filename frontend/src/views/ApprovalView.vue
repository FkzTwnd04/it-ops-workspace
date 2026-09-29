<template>
  <div class="page">
    <div class="toolbar">
      <h3>高危操作审批台</h3>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
    </div>

    <el-tabs v-model="tab">
      <el-tab-pane :label="`待审批 ${groups.length}`" name="pending">
        <div v-loading="loading" class="list">
          <ApprovalCard v-for="g in groups" :key="g[0].interrupt_id" :items="g" show-ticket @decided="load" />
          <el-empty v-if="!loading && !groups.length" description="没有待审批的操作" />
        </div>
      </el-tab-pane>

      <el-tab-pane label="审批记录" name="history">
        <el-card shadow="never">
          <el-table :data="history" stripe size="small">
            <el-table-column label="工单" width="170">
              <template #default="{ row }">
                <RouterLink :to="`/tickets/${row.ticket_id}`" class="link">{{ row.ticket_no }}</RouterLink>
              </template>
            </el-table-column>
            <el-table-column prop="title" label="标题" min-width="160" show-overflow-tooltip />
            <el-table-column label="操作" width="120">
              <template #default="{ row }">{{ TOOL_LABELS[row.tool_name] ?? row.tool_name }}</template>
            </el-table-column>
            <el-table-column label="申请参数" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">{{ toolArgs(row.tool_args) }}</template>
            </el-table-column>
            <el-table-column label="决定" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="TYPE[row.status as keyof typeof TYPE]">{{ LABEL[row.status as keyof typeof LABEL] }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="执行参数" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ row.status === 'edited' ? toolArgs(row.final_args) : '' }}</template>
            </el-table-column>
            <el-table-column prop="reviewer_name" label="审批人" width="100" />
            <el-table-column prop="comment" label="意见" min-width="140" show-overflow-tooltip />
            <el-table-column label="等待时长" width="100">
              <template #default="{ row }">{{ wait(row) }}</template>
            </el-table-column>
            <el-table-column label="审批时间" width="150">
              <template #default="{ row }">{{ fmt(row.decided_at) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ticketsApi, TOOL_LABELS, type Approval } from '@/api/tickets'
import ApprovalCard from '@/components/ticket/ApprovalCard.vue'
import { fmt, toolArgs } from '@/utils/format'

const LABEL = { pending: '待审批', approved: '已批准', edited: '修改后批准', rejected: '已拒绝' } as const
const TYPE = { pending: 'warning', approved: 'success', edited: 'primary', rejected: 'danger' } as const

const tab = ref('pending')
const pending = ref<Approval[]>([])
const history = ref<Approval[]>([])
const loading = ref(false)

// 同一次中断的多个高危操作要一起审批
const groups = computed(() => {
  const map = new Map<string, Approval[]>()
  for (const a of pending.value) {
    const list = map.get(a.interrupt_id) ?? []
    list.push(a)
    map.set(a.interrupt_id, list)
  }
  return [...map.values()]
})

function wait(a: Approval) {
  if (!a.decided_at) return '—'
  const s = (new Date(a.decided_at).getTime() - new Date(a.requested_at).getTime()) / 1000
  return s < 60 ? `${s.toFixed(0)} 秒` : `${(s / 60).toFixed(1)} 分钟`
}

async function load() {
  loading.value = true
  try {
    const [p, h] = await Promise.all([ticketsApi.pendingApprovals(), ticketsApi.approvalHistory()])
    pending.value = p.data
    history.value = h.data.filter(a => a.status !== 'pending').reverse()
  } finally {
    loading.value = false
  }
}

watch(tab, load)
let timer: number | undefined
onMounted(() => {
  load()
  timer = window.setInterval(async () => {
    const { data } = await ticketsApi.pendingApprovals()
    if (data.length !== pending.value.length) pending.value = data
  }, 8_000)
})
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.toolbar { display: flex; justify-content: space-between; align-items: center; }
.toolbar h3 { margin: 0; }
.list { display: flex; flex-direction: column; gap: 16px; min-height: 120px; }
.link { color: #1677ff; text-decoration: none; }
</style>
