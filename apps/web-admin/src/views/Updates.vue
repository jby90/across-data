<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'

interface Discovery { id: string; country_code: string; title: string; source_url: string; discovered_at: string; first_parsed_at: string | null; history_unknown: boolean; overdue: boolean; elapsed_hours: number | null }
interface Change { id: string; document_title: string; country_code: string; source_url: string; detected_at: string; kind: string; summary: string; changes: Array<{ kind: string; clause_number?: string; section_path?: string }>; graph_edge_ids: string[]; content_hashes: Record<string,string> }
interface Sla { pending_updates: number; overdue_72h: number; resolved_updates: number; late_notification_records: number; intake: { pending: number; completed: number; overdue_30d: number; history_unknown: number; untracked_documents: number; definition: string }; countries: string[] }
const sla = ref<Sla | null>(null), changes = ref<Change[]>([]), discoveries = ref<Discovery[]>([])
const busy = ref(false), error = ref(''), country = ref(''), page = ref(1), total = ref(0)
const changePage = ref(1), changeTotal = ref(0)
const kindLabel: Record<string,string> = { bytes_only: '字节变化，条款一致', text_change_pending_review: '条款变化待核对', source_integrity_pending: '解析完整性待核对', parsing_pending: '检测时尚未完成解析' }
const date = (value: string | null) => value ? new Date(value).toLocaleString('zh-CN') : '未记录'
async function loadDiscoveries() {
  const result = await api.get<{ items: Discovery[]; total: number }>(`/admin/updates/discoveries?offset=${(page.value-1)*25}&limit=25${country.value ? `&country=${encodeURIComponent(country.value)}` : ''}`)
  discoveries.value = result.items; total.value = result.total
}
async function load() {
  busy.value = true; error.value = ''
  try {
    const [metrics] = await Promise.all([api.get<Sla>('/admin/updates/sla'), loadChanges(), loadDiscoveries()])
    sla.value = metrics
  } catch(e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function loadChanges() {
  try {
    const records = await api.get<{items: Change[]; total: number}>(`/admin/updates/changes?offset=${(changePage.value-1)*25}&limit=25`)
    changes.value = records.items; changeTotal.value = records.total
  } catch(e) { error.value = (e as Error).message }
}
async function changeDiscoveryPage() { try { await loadDiscoveries() } catch(e) { error.value = (e as Error).message } }
async function filter() { page.value = 1; try { await loadDiscoveries() } catch(e) { error.value = (e as Error).message } }
onMounted(load)
</script>
<template>
  <div class="management-page" v-loading="busy">
    <header class="page-heading"><div><h1>更新与时效</h1><p>追踪原文变化、报告影响和发现至解析入库的耗时。</p></div><el-button @click="load">刷新状态</el-button></header>
    <el-alert v-if="error" type="error" :closable="false" :title="`更新状态加载失败：${error}`" />
    <div class="sla-cards" data-testid="update-sla-summary">
      <article><span>待处理更新</span><strong>{{ sla?.pending_updates ?? '—' }}</strong></article>
      <article><span>超过 72 小时未处理</span><strong>{{ sla?.overdue_72h ?? '—' }}</strong></article>
      <article><span>已由新报告处理</span><strong>{{ sla?.resolved_updates ?? '—' }}</strong></article>
      <article><span>入库超过 30 天</span><strong>{{ sla?.intake.overdue_30d ?? '—' }}</strong></article>
    </div>
    <el-alert type="info" :closable="false" title="检测到内容变化不等于确认法规修订。旧原文、图谱证据、报告和审批记录保留；处理通知不代表法律复核通过。" />
    <section class="updates-section"><h2>来源变化记录</h2>
      <el-empty v-if="!changes.length && !busy" description="暂无已检测的来源变化" />
      <details v-for="change in changes" :key="change.id" class="change-record" data-testid="source-change-record">
        <summary><span>{{ change.country_code }} · {{ change.document_title }}</span><el-tag :type="change.kind === 'bytes_only' ? 'info' : 'warning'">{{ kindLabel[change.kind] || '变化待核对' }}</el-tag></summary>
        <p>{{ change.summary }}</p><p>检测：{{ date(change.detected_at) }} · 关联图谱证据 {{ change.graph_edge_ids.length }} 条（记录未改写）</p>
        <a :href="change.source_url" target="_blank" rel="noopener noreferrer">查看来源</a>
        <ul><li v-for="(clause,index) in change.changes" :key="index">{{ clause.section_path || '层级待核对' }} · {{ clause.clause_number || '条款对齐待核对' }} · {{ clause.kind }}</li></ul>
        <details><summary>原文校验码与证据定位</summary><pre>{{ JSON.stringify({ hashes: change.content_hashes, clauses: change.changes, graph_edges: change.graph_edge_ids }, null, 2) }}</pre></details>
      </details>
      <el-pagination v-if="changeTotal > 25" v-model:current-page="changePage" :page-size="25" :total="changeTotal" layout="prev, pager, next" @current-change="loadChanges" />
    </section>
    <section class="updates-section"><div class="section-heading"><h2>新发现资料入库跟踪</h2><el-select v-model="country" clearable placeholder="全部国别" aria-label="跟踪国别" style="width:160px" @change="filter"><el-option v-for="code in sla?.countries || []" :key="code" :label="code" :value="code" /></el-select></div>
      <p>{{ sla?.intake.definition }}。历史入库时间未知 {{ sla?.intake.history_unknown ?? '—' }} 份；尚无发现跟踪记录的既有文件 {{ sla?.intake.untracked_documents ?? '—' }} 份。超期生成的站内提醒 {{ sla?.late_notification_records ?? '—' }} 条。</p>
      <el-table :data="discoveries" border empty-text="暂无发现记录">
        <el-table-column prop="country_code" label="国别" width="75" />
        <el-table-column prop="title" label="文件" min-width="230" />
        <el-table-column label="发现时间" min-width="170"><template #default="{ row }">{{ date(row.discovered_at) }}</template></el-table-column>
        <el-table-column label="首次成功解析" min-width="170"><template #default="{ row }">{{ row.history_unknown ? '历史时间未记录' : row.first_parsed_at ? date(row.first_parsed_at) : '尚未完成' }}</template></el-table-column>
        <el-table-column label="状态" min-width="140"><template #default="{ row }"><el-tag :type="row.overdue ? 'danger' : row.first_parsed_at ? 'info' : 'warning'">{{ row.history_unknown ? '时间待核对' : row.overdue ? '超过 30 天' : row.first_parsed_at ? '已解析入库，待复核' : '待解析入库' }}</el-tag></template></el-table-column>
      </el-table>
      <el-pagination v-if="total > 25" v-model:current-page="page" :page-size="25" :total="total" layout="prev, pager, next" @current-change="changeDiscoveryPage" />
    </section>
  </div>
</template>
<style scoped>
.sla-cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px;margin:24px 0}.sla-cards article{padding:23px;border:1px solid var(--el-border-color-light);border-radius:14px;background:white}.sla-cards span{font-size:13px;color:var(--el-text-color-secondary)}.sla-cards strong{display:block;margin-top:12px;font-size:28px;color:#17344d}
.updates-section{margin-top:32px}.updates-section h2{font-size:19px}.updates-section p{line-height:1.7;color:var(--el-text-color-secondary);font-size:13px}.change-record{background:white;border:1px solid var(--el-border-color-light);border-radius:12px;padding:20px;margin:12px 0}.change-record>summary{cursor:pointer;display:flex;gap:14px;align-items:center;flex-wrap:wrap}.change-record>summary>span{flex:1;min-width:240px;font-weight:600;font-size:15px}.change-record li{margin:8px 0;font-size:13px}.change-record pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-height:300px;overflow:auto;background:#f5f7fa;padding:16px}.section-heading{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:12px}
@media(max-width:900px){.sla-cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
