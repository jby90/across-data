<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'

const unread = ref<number | null>(null)
let timer: number | undefined
async function refresh() {
  if (document.hidden) return
  try {
    const data = await api.get<{ unread: number }>('/notifications?limit=1')
    if (unread.value !== data.unread) window.dispatchEvent(new Event('regulatory-notifications-updated'))
    unread.value = data.unread
  } catch { unread.value = null }
}
onMounted(() => {
  refresh()
  timer = window.setInterval(refresh, 15000)
  document.addEventListener('visibilitychange', refresh)
})
onUnmounted(() => {
  window.clearInterval(timer)
  document.removeEventListener('visibilitychange', refresh)
})
</script>
<template>
  <RouterLink to="/notifications" class="notification-bell" aria-label="更新通知"
    :title="unread === null ? '通知状态暂不可用，点击查看' : `${unread} 条未读更新通知`">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">
      <path d="M18 8a6 6 0 0 0-12 0v6l-2 3h16l-2-3V8 M10 21h4" />
    </svg>
    <span v-if="unread" class="notification-count">{{ unread > 99 ? '99+' : unread }}</span>
  </RouterLink>
</template>
<style scoped>
.notification-bell{position:relative;display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;color:var(--el-text-color-regular);border-radius:9px}
.notification-bell:hover{background:var(--el-fill-color-light)}
svg{width:21px;height:21px}.notification-count{position:absolute;right:-5px;top:-2px;min-width:17px;padding:1px 4px;border-radius:10px;background:#b45309;color:white;font-size:10px;text-align:center}
</style>
