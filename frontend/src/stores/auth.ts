import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export type Role = 'requester' | 'ops' | 'admin'

interface UserInfo {
  userId: string
  role: Role
  tenantId: string
  username: string
  displayName: string
}

export const TOKEN_KEY = 'it-ticket-token'
const USER_KEY = 'it-ticket-user'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
  const user = ref<UserInfo | null>((() => {
    try {
      return JSON.parse(localStorage.getItem(USER_KEY) ?? 'null')
    } catch {
      return null
    }
  })())

  const isLoggedIn = computed(() => !!token.value)
  const isOps = computed(() => user.value?.role === 'ops' || user.value?.role === 'admin')

  function login(accessToken: string, userInfo: UserInfo) {
    token.value = accessToken
    user.value = userInfo
    localStorage.setItem(TOKEN_KEY, accessToken)
    localStorage.setItem(USER_KEY, JSON.stringify(userInfo))
  }

  function logout() {
    token.value = null
    user.value = null
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  }

  return { token, user, isLoggedIn, isOps, login, logout }
})
