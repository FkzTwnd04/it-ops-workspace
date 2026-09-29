<template>
  <div class="page">
    <div class="toolbar">
      <h3>运营看板 <span class="sub">{{ auth.user?.tenantId }} · 全部指标由落库数据实时计算</span></h3>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-row v-if="s" :gutter="16">
      <el-col v-for="c in cards" :key="c.label" :span="6">
        <el-card shadow="never" class="stat">
          <div class="label">{{ c.label }}</div>
          <div class="value" :style="{ color: c.color }">{{ c.value }}</div>
          <div class="desc">{{ c.desc }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card v-if="s" shadow="never">
      <template #header>工单状态分布</template>
      <div class="bars">
        <div v-for="(m, k) in STATUS_META" :key="k" class="bar-row">
          <span class="bar-label">{{ m.label }}</span>
          <el-progress :percentage="share(k)" :stroke-width="14" :show-text="false" class="bar" />
          <span class="bar-n">{{ s.by_status[k] ?? 0 }}</span>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ticketsApi, STATUS_META, type Stats } from '@/api/tickets'
import { useAuthStore } from '@/stores/auth'
import { pct, seconds } from '@/utils/format'

const auth = useAuthStore()
const s = ref<Stats | null>(null)
const loading = ref(false)

const total = computed(() => Object.values(s.value?.by_status ?? {}).reduce((a, b) => a + b, 0))
const share = (k: string) => (total.value ? Math.round(((s.value?.by_status[k] ?? 0) / total.value) * 100) : 0)

const cards = computed(() => {
  const v = s.value!
  return [
    { label: '工单总数', value: String(total.value), desc: `已结案 ${v.finished}，Agent 运行 ${v.runs} 次`, color: '#262626' },
    { label: '自动解决率', value: pct(v.auto_resolve_rate), desc: '已结案工单中由 Agent 自动解决的比例', color: '#52c41a' },
    { label: '转人工率', value: pct(v.escalation_rate), desc: '处理过的工单中升级到人工的比例', color: '#fa8c16' },
    { label: '高危审批率', value: pct(v.approval_rate), desc: '处理过的工单中触发高危审批的比例', color: '#1677ff' },
    { label: '降级率', value: pct(v.fallback_rate), desc: '运行中触发二、三级降级的比例', color: '#f5222d' },
    { label: '处理耗时 P50', value: seconds(v.latency_p50_ms), desc: '单次运行，已扣除等待审批时间', color: '#262626' },
    { label: '处理耗时 P95', value: seconds(v.latency_p95_ms), desc: '单次运行，已扣除等待审批时间', color: '#262626' },
    { label: '已处理工单', value: String(v.processed), desc: '至少运行过一次 Agent 的工单', color: '#262626' },
  ]
})

async function load() {
  loading.value = true
  try {
    s.value = (await ticketsApi.stats()).data
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: 16px; }
.toolbar { display: flex; justify-content: space-between; align-items: center; }
.toolbar h3 { margin: 0; }
.sub { font-size: 12px; color: #8c8c8c; font-weight: normal; margin-left: 8px; }
.stat { margin-bottom: 16px; }
.label { font-size: 13px; color: #8c8c8c; }
.value { font-size: 28px; font-weight: 600; margin: 6px 0; }
.desc { font-size: 12px; color: #bfbfbf; }
.bars { display: flex; flex-direction: column; gap: 10px; }
.bar-row { display: flex; align-items: center; gap: 12px; }
.bar-label { width: 90px; font-size: 13px; color: #595959; }
.bar { flex: 1; }
.bar-n { width: 40px; text-align: right; font-size: 13px; }
</style>
