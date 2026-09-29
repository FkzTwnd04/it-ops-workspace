<template>
  <div class="sidebar">
    <div class="logo">
      <span>🛠️ IT 工单</span>
    </div>
    <nav class="nav-list">
      <RouterLink v-if="auth.isOps" to="/dashboard" class="nav-item" :class="{ 'nav-item--active': isActive('/dashboard') }">
        <el-icon><DataAnalysis /></el-icon>
        <span>运营看板</span>
      </RouterLink>

      <RouterLink to="/tickets" class="nav-item" :class="{ 'nav-item--active': isActive('/tickets') }">
        <el-icon><Tickets /></el-icon>
        <span>{{ auth.isOps ? '工单列表' : '我的工单' }}</span>
      </RouterLink>

      <template v-if="auth.isOps">
        <div class="nav-divider" />
        <RouterLink to="/approvals" class="nav-item" :class="{ 'nav-item--active': isActive('/approvals') }">
          <el-icon><Stamp /></el-icon>
          <span>审批台</span>
        </RouterLink>
      </template>
    </nav>
  </div>
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'
import { DataAnalysis, Tickets, Stamp } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()

function isActive(prefix: string) {
  return route.path === prefix || route.path.startsWith(prefix + '/')
}
</script>

<style scoped>
.sidebar {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.logo {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 16px;
  font-weight: 600;
  border-bottom: 1px solid #ffffff1a;
  flex-shrink: 0;
}

.nav-list {
  display: flex;
  flex-direction: column;
  padding: 4px 0;
  flex: 1;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 13px 20px;
  color: #ffffffa6;
  text-decoration: none;
  font-size: 14px;
  cursor: pointer;
  transition: background-color 0.2s, color 0.2s;
  user-select: none;
}

.nav-item:hover {
  background-color: #ffffff14;
  color: #fff;
}

.nav-item--active {
  background-color: #1677ff;
  color: #fff;
}

.nav-item .el-icon {
  font-size: 16px;
  flex-shrink: 0;
}

.nav-divider {
  border: none;
  border-top: 1px solid #ffffff1a;
  margin: 8px 0;
}
</style>
