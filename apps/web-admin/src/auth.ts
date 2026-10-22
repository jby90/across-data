import type { UserOut } from '@cyshuhe/shared'
import { defineStore } from 'pinia'
import { api } from './api'

export const useAuth = defineStore('auth', {
  state: () => ({ user: null as UserOut | null }),
  actions: {
    async login(email: string, password: string) {
      const res = await api.login(email, password)
      api.setToken(res.access_token)
      this.user = await api.me()
    },
    async load() {
      if (!this.user && api.token()) this.user = await api.me()
    },
    logout() {
      api.setToken(null)
      this.user = null
    },
  },
})
