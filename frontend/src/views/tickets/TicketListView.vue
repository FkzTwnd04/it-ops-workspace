<template>
  <div class="page">
    <div class="toolbar">
      <h3>{{ auth.isOps ? '工单列表' : '我的工单' }}</h3>
      <div class="toolbar-right">
        <el-select v-model="statusFilter" placeholder="全部状态" clearable style="width: 150px" @change="load">
          <el-option v-for="(m, k) in STATUS_META" :key="k" :label="m.label" :value="k" />
        </el-select>
        <el-checkbox v-if="auth.isOps" v-model="mine" @change="load">只看我提交的</el-checkbox>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="dialogVisible = true">提交工单</el-button>
      </div>
    </div>

    <el-card shadow="never">
      <el-table v-loading="loading" :data="tickets" stripe style="width: 100%" @row-click="open">
        <el-table-column prop="ticket_no" label="工单号" width="170" />
        <el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip />
        <el-table-column label="分类" width="130">
          <template #default="{ row }">{{ row.category ? CATEGORY_LABELS[row.category] ?? row.category : '—' }}</template>
        </el-table-column>
        <el-table-column label="优先级" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="priorityType(row.priority)" effect="plain">{{ row.priority }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="STATUS_META[row.status as TicketStatus].type">
              {{ STATUS_META[row.status as TicketStatus].label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="处理方式" width="110">
          <template #default="{ row }">
            <span v-if="row.auto_resolved" class="auto">Agent 自动</span>
            <span v-else-if="row.status === 'ESCALATED' || row.escalation_reason" class="human">人工</span>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column v-if="auth.isOps" prop="requester_name" label="提单人" width="100" />
        <el-table-column label="SLA 截止" width="170">
          <template #default="{ row }">
            <span :class="{ overdue: isOverdue(row) }">{{ fmt(row.sla_due_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="提交时间" width="170">
          <template #default="{ row }">{{ fmt(row.created_at) }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && tickets.length === 0" description="暂无工单" />
    </el-card>

    <el-dialog v-model="dialogVisible" title="提交 IT 工单" width="560px">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
        <el-form-item label="标题" prop="title">
          <el-input v-model="form.title" placeholder="一句话描述问题，例如：VPN 连不上" maxlength="200" />
        </el-form-item>
        <el-form-item label="问题描述" prop="description">
          <el-input v-model="form.description" type="textarea" :rows="5" maxlength="4000" show-word-limit
                    placeholder="报错信息、出现时间、影响范围，越具体处理越快" />
        </el-form-item>
        <el-form-item label="相关系统">
          <el-input v-model="form.host" placeholder="可不填，例如 vpn-gw-01、mail-01" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">提交并交给 Agent 处理</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { ticketsApi, STATUS_META, CATEGORY_LABELS, type Ticket, type TicketStatus } from '@/api/tickets'
import { useAuthStore } from '@/stores/auth'
import { fmt, priorityType } from '@/utils/format'

const auth = useAuthStore()
const router = useRouter()

const tickets = ref<Ticket[]>([])
const loading = ref(false)
const statusFilter = ref<string>('')
const mine = ref(false)

const dialogVisible = ref(false)
const submitting = ref(false)
const formRef = ref<FormInstance>()
const form = reactive({ title: '', description: '', host: '' })
const rules: FormRules = {
  title: [{ required: true, min: 2, message: '请填写标题', trigger: 'blur' }],
  description: [{ required: true, min: 2, message: '请描述问题', trigger: 'blur' }],
}

async function load(silent = false) {
  if (silent !== true) loading.value = true
  try {
    const { data } = await ticketsApi.list({ status: statusFilter.value || undefined, mine: mine.value, limit: 200 })
    tickets.value = data
  } finally {
    loading.value = false
  }
}

async function submit() {
  await formRef.value?.validate()
  submitting.value = true
  try {
    const { data } = await ticketsApi.create({
      title: form.title, description: form.description, host: form.host || undefined, auto_run: true,
    })
    ElMessage.success(`工单 ${data.ticket_no} 已提交，Agent 开始处理`)
    dialogVisible.value = false
    Object.assign(form, { title: '', description: '', host: '' })
    router.push(`/tickets/${data.id}`)
  } finally {
    submitting.value = false
  }
}

function open(row: Ticket) {
  router.push(`/tickets/${row.id}`)
}

function isOverdue(t: Ticket) {
  return !!t.sla_due_at && !['RESOLVED', 'CLOSED'].includes(t.status) && new Date(t.sla_due_at) < new Date()
}

let timer: number | undefined
onMounted(() => {
  load()
  timer = window.setInterval(() => load(true), 10_000)
})
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: 16px; }
.toolbar { display: flex; justify-content: space-between; align-items: center; }
.toolbar h3 { margin: 0; }
.toolbar-right { display: flex; gap: 12px; align-items: center; }
.auto { color: #52c41a; }
.human { color: #fa8c16; }
.overdue { color: #f5222d; font-weight: 600; }
:deep(.el-table__row) { cursor: pointer; }
</style>
