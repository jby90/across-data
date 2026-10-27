<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'

interface Notice {
  id: string; task_id: string; task_title: string; document_title: string; country_code: string
  summary: string; dimension_codes: string[]; read_at: string | null; due_at: string | null
  created_at: string; state: string
}
const items = ref<Notice[]>([]), total = ref(0), page = ref(1), busy = ref(false), executing = ref('')
const router = useRouter()
async function load() {
  busy.value = true
  try {
    const result = await api.get<{ items: Notice[]; total: number }>(`/notifications?offset=${(page.value-1)*20}&limit=20`)
    items.value = result.items; total.value = result.total
  } catch (e) { ElMessage.error((e as Error).message) }
  finally { busy.value = false }
}
async function read(n: Notice) {
  try { await api.post(`/notifications/${n.id}/read`); await load() }
  catch (e) { ElMessage.error((e as Error).message) }
}
async function reevaluate(n: Notice) {
  if (executing.value) return
  executing.value = n.id
  try {
    const result = await api.post<{ task_id: string }>(`/notifications/${n.id}/reevaluate`)
    await router.push({ path: '/analysis', query: { task: result.task_id } })
  } catch (e) { ElMessage.error((e as Error).message) }
  finally { executing.value = '' }
}
const date = (value: string | null) => value ? new Date(value).toLocaleString('zh-CN') : '未记录'
onMounted(load)
</script>
<template>
  <div class="management-page" v-loading="busy">
    <header class="page-heading"><div><h1>更新通知</h1><p>查看原文变化及受影响的报告，重新评估后留存新版本。</p></div>
      <el-button @click="load">刷新</el-button></header>
    <el-empty v-if="!items.length && !busy" description="暂无更新通知" />
    <article v-for="n in items" :key="n.id" class="notice-card" data-testid="update-notification">
      <div class="notice-heading"><strong>{{ n.country_code }} · {{ n.document_title }}</strong>
        <el-tag :type="n.state === 'resolved' ? 'info' : 'warning'">{{ n.state === 'resolved' ? '已由新版本处理' : '来源已更新' }}</el-tag>
        <el-tag v-if="!n.read_at" size="small">未读</el-tag></div>
      <p>{{ n.summary }}</p>
      <p>报告：{{ n.task_title }} · 涉及 {{ n.dimension_codes.join('、') }}</p>
      <p class="notice-time">发现后处理时限：{{ date(n.due_at) }} · 提醒生成：{{ date(n.created_at) }}</p>
      <div class="notice-actions">
        <el-button v-if="n.state === 'open'" type="primary" :loading="executing === n.id" @click="reevaluate(n)">重新评估</el-button>
        <el-button @click="router.push({ path: '/analysis', query: { task: n.task_id } })">查看报告</el-button>
        <el-button v-if="!n.read_at" link @click="read(n)">标记已读</el-button>
      </div>
    </article>
    <el-pagination v-if="total > 20" v-model:current-page="page" :page-size="20" :total="total"
      layout="prev, pager, next" @current-change="load" />
  </div>
</template>
<style scoped>
.notice-card{margin:18px 0;padding:24px;border:1px solid var(--el-border-color-light);border-radius:14px;background:white}
.notice-heading{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.notice-heading strong{font-size:16px;flex:1;min-width:240px}
.notice-card p{line-height:1.7;margin:12px 0;color:var(--el-text-color-regular)}.notice-card .notice-time{font-size:13px;color:var(--el-text-color-secondary)}
.notice-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
</style>
