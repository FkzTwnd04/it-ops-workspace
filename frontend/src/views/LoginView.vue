<template>
  <div class="login-page">
    <el-card class="login-card">
      <div class="login-header">
        <div class="login-logo">🛠️</div>
        <h2>IT 智能工单系统</h2>
        <p>Deep Agents 驱动的服务台</p>
      </div>
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="handleLogin"
      >
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="请输入用户名" size="large" :prefix-icon="User" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            size="large"
            :prefix-icon="Lock"
            show-password
            @keyup.enter="handleLogin"
          />
        </el-form-item>
        <el-button type="primary" size="large" :loading="loading" style="width: 100%; margin-top: 8px" @click="handleLogin">
          登录
        </el-button>
      </el-form>
      <div class="demo-accounts">
        演示账号（密码 Passw0rd!）：提单人 zhangsan · 运维 ops_wang · 分公司运维 ops_chen
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { User, Lock } from '@element-plus/icons-vue'
import { authApi } from '@/api/auth'
import { useAuthStore, type Role } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function handleLogin() {
  await formRef.value?.validate()
  loading.value = true
  try {
    const { data } = await authApi.login({ username: form.username, password: form.password })
    auth.login(data.access_token, {
      userId: data.user_id,
      role: data.role as Role,
      tenantId: data.tenant_id,
      username: form.username,
      displayName: data.display_name || form.username,
    })
    router.push(auth.isOps ? '/dashboard' : '/tickets')
  } catch {
    ElMessage.error('用户名或密码错误')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
}
.login-card {
  width: 400px;
  border-radius: 12px;
}
.login-header {
  text-align: center;
  margin-bottom: 24px;
}
.login-logo {
  font-size: 48px;
  margin-bottom: 8px;
}
.login-header h2 {
  margin: 0 0 4px;
  font-size: 24px;
  color: #1a1a1a;
}
.login-header p {
  margin: 0;
  color: #8c8c8c;
  font-size: 14px;
}
.demo-accounts {
  margin-top: 16px;
  font-size: 12px;
  color: #8c8c8c;
  line-height: 1.6;
}
</style>
