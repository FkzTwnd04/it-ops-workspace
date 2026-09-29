import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'
import { TOKEN_KEY, useAuthStore } from '@/stores/auth'

export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '') + '/api/v1'

const client = axios.create({
  baseURL: API_BASE,
  timeout: 30_000,
})

// 请求拦截器：注入 JWT
client.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// 响应拦截器：统一错误处理
client.interceptors.response.use(
  res => res,
  (error: AxiosError<{ detail: string }>) => {
    const status = error.response?.status
    const detail = error.response?.data?.detail

    if (status === 401) {
      // 同时清除 Pinia store（响应式 token）和 localStorage，
      // 否则 auth.isLoggedIn 仍为 true，router.push('/login') 会被导航守卫重定向回首页
      useAuthStore().logout()
      router.push('/login').catch(() => {})
      return Promise.reject(error)
    }

    if (status === 403) {
      ElMessage.error('权限不足')
    } else if (status === 429) {
      ElMessage.warning('请求过于频繁，请稍后再试')
    } else if (status && status >= 500) {
      ElMessage.error(detail ?? '服务异常，请稍后重试')
    } else if (detail) {
      ElMessage.error(detail)
    }

    return Promise.reject(error)
  },
)

export default client
