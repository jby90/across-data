<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { startProgressStream, type StreamState } from '@cyshuhe/shared'
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { useAuth } from '../auth'
import { conversationStamp, conversationSignal, elapsedLabel, receiveExport } from '../chatFeedback'
import LegalText from '../components/LegalText.vue'
import type { LegalTerm } from '@cyshuhe/shared/src/legalText'
function readableMessage(text: string) {
  return text.replace(/信息已经齐备[\s\S]*下面会实时显示每个智能体的进度。/, '信息已齐备，正在查找适用法规并生成合规报告。')
    .replace('然后由多个智能体对照目标国现行法规,', '再对照目标国法规，')
    .replace('所有回答只用于本次评估。', '')
}
function readableContent(text: string) {
  return text.replace('依据与合规义务（程序子串/数字核验通过；人工复核状态逐条标注）', '依据与合规义务')
    .replaceAll('；待核对（原文/译文尚未全部人工复核）', '')
    .replaceAll('；引用材料已复核（分析结论仍为机器草稿）', '')
}
function businessSummary(text: string) {
  return /多智能体|程序核验|tokens|MCP/.test(text)
    ? `本报告根据您的业务场景，对照适用法规形成八个维度的分析与建议。各章节可查看依据、修订内容并单独导出。` : text
}
const route = useRoute()
const router = useRouter()
const auth = useAuth()
let openRequest = 0

interface ChatMessage {
  id: string
  seq_no: number
  role: 'user' | 'assistant'
  content: string
  options: string[]
  slot: string | null
}

interface Finding {
  finding_id: string
  dimension_code: string
  business_fact: string
  clause_id: string | null
  document_title: string
  clause_number: string
  version_no: number
  compliance_obligation: string
  risk_level: string
  basis_quote: string
  basis_zh: string
  verification_status: string
}


interface Citation {
  source_review_status?: string
  translation_review_status?: string
  language?: string
  citation_tag: string
  clause_id: string | null
  document_title: string
  clause_number: string
  version_no: number
  quote_original: string
}

interface Dimension {
  findings?: Finding[]
  dimension_code: string
  dimension_title: string
  status: string
  risk_level: string
  content_markdown: string
  key_recommendations: string[]
  citations: Citation[]
  missing_gap_note: string | null
}

interface StaleImpact {
  dimension_code: string
  clause_ref: string
  amendment_summary: string
  action_required: string
}

interface ReportVersion {
  regulatory_updates?: Array<{ id: string; change_id: string; dimension_code: string; document_title: string; summary: string; state: string }>
  citation_policy: string
  citation_review_summary: { approved: number; pending: number; total: number; occurrences: number }
  source_integrity_alerts?: string[]
  version_no: number
  status: string
  is_partial_sample: boolean
  change_note: string
  executive_summary: string
  dimensions: Dimension[]
  stale_impacts: StaleImpact[]
}

interface TaskSummary {
  messages_count: number
  id: string
  conversation_code: string
  title: string
  status: string
  country_code: string
  latest_report_version: number
}

interface TaskDetail {
  activity?: {label: string; state: string; started_at: string} | null
  id: string
  conversation_code: string
  current_active_node_id: string | null
  current_active_node_title: string | null
  title: string
  country_code: string
  country_name_zh: string
  status: string
  messages: ChatMessage[]
  findings: Finding[]
  progress: Array<{label: string; state: string}>
  report_versions: ReportVersion[]
  pending_user_facts: Array<{ id: string; fact_key: string; dimension_codes: string[] }>
  fact_followup_rounds: number
}

interface CitationCard {
  clause_id: string
  section_path?: string | null
  source_review_status?: string
  translation_review_status?: string
  source_integrity_alerts?: string[]
  document_title: string
  clause_number: string
  version_no: number
  source_url: string | null
  original_text: string
  translated_text: string
  review_status: string
  terms: LegalTerm[]
  related_requirements: Array<{id: string; title: string; topic_label: string; quote: string}>
}
interface DiffPayload {
  from_version: number
  to_version: number
  dimension_diffs: Array<{
    dimension_code: string
    dimension_title: string
    changed: boolean
    from_markdown: string
    to_markdown: string
  }>
}

const RISK_LABEL: Record<string, string> = {
  HIGH: '高风险',
  MEDIUM: '中风险',
  LOW: '低风险',
  PENDING_VERIFICATION: '待核查',
}
const STATUS_LABEL: Record<string, string> = {
  completed: '已出报告',
  collecting_facts: '采集信息中',
  ready_to_run: '待启动',
  partial_sample: '部分样稿',
  running: '分析中',
  awaiting_user_facts: '等待补充业务事实',
  failed: '分析失败',
}
// 这两类问题直接点选即发送;事实类问题的示例只填入输入框,由用户按实际情况修改后再发送
const CHOICE_SLOTS = new Set(['country_code', 'scenario_type'])

const tasks = ref<TaskSummary[]>([])
const detail = ref<TaskDetail | null>(null)
const input = ref('')
const pendingSends = reactive(new Map<string, number>())
const sending = computed(() => !!detail.value && pendingSends.has(detail.value.id))
const clockNow = ref(Date.now())
const seen = ref<Record<string, string>>({})
const followStream = ref(true)
const exportJob = ref<{taskId: string; label: string; started: number; phase: 'preparing' | 'receiving'; received: number; total: number | null} | null>(null)
const currentExport = computed(() => exportJob.value?.taskId === detail.value?.id ? exportJob.value : null)
const sendElapsed = computed(() => elapsedLabel(pendingSends.get(detail.value?.id || '') || clockNow.value, clockNow.value))
const activityElapsed = computed(() => detail.value?.activity?.started_at
  ? elapsedLabel(Date.parse(detail.value.activity.started_at), clockNow.value) : '')
const exportElapsed = computed(() => elapsedLabel(exportJob.value?.started || clockNow.value, clockNow.value))
let mounted = true
let lastSidebarRefresh = 0
let polling = false
let listRequest = 0
const streamEl = ref<HTMLDivElement | null>(null)
const inputEl = ref<{ focus: () => void } | null>(null)

const reportVersionNo = ref(1)
const openChapters = ref<string[]>([])
const openExtras = ref<string[]>([])

const drawerVisible = ref(false)
const drawerLoading = ref(false)
const card = ref<CitationCard | null>(null)
const anchorQuote = ref('')

const editVisible = ref(false)
const editNote = ref('业务人员在线修订')
const editSummary = ref('')
const editDims = reactive<Record<string, string>>({})
const savingEdit = ref(false)

const diffVisible = ref(false)
const diffData = ref<DiffPayload | null>(null)

const status = computed(() => detail.value?.status ?? '')
const progressState = ref<StreamState>('idle')
let progressStream: {close: () => void} | undefined
let progressRefresh: ReturnType<typeof setTimeout> | undefined
const lastAssistant = computed(() => {
  const list = detail.value?.messages.filter((m) => m.role === 'assistant') ?? []
  return list[list.length - 1]
})
const showChips = computed(
  () =>
    ['collecting_facts', 'awaiting_user_facts'].includes(status.value) &&
    !!lastAssistant.value &&
    lastAssistant.value.options.length > 0 &&
    detail.value?.messages[detail.value.messages.length - 1]?.role === 'assistant',
)
const hasReport = computed(
  () => (detail.value?.report_versions.length ?? 0) > 0 &&
    !['running', 'awaiting_user_facts'].includes(status.value),
)
const report = computed<ReportVersion | null>(() => {
  const list = detail.value?.report_versions ?? []
  return list.find((r) => r.version_no === reportVersionNo.value) ?? list[0] ?? null
})
const versionFindings = computed(() => report.value?.dimensions.flatMap(d => d.findings || []) || [])
const inputPlaceholder = computed(() => {
  if (status.value === 'running') return '正在分析您的业务场景，请稍候…'
  if (status.value === 'awaiting_user_facts') return '请回答分析中的业务追问；不确定可以如实说明'
  if (status.value === 'completed' || status.value === 'partial_sample')
    return '报告已生成。可在上方导出,或点击左侧「新建评估」评估新的场景'
  const slot = lastAssistant.value?.slot
  if (slot === 'country_code') return '输入或点选目标国家…'
  if (slot === 'vehicle_model') return '输入车型或车队名称…'
  return '请输入您的回答,Enter 发送,Shift+Enter 换行'
})
const inputDisabled = computed(() => sending.value || status.value === 'running')

function scrollBottom(force = false) {
  nextTick(() => {
    const el = streamEl.value
    if (!el || (!force && !followStream.value)) return
    const summary = el.querySelector<HTMLElement>('[data-testid="report-card"]')
    if (hasReport.value && summary) {
      el.scrollTop += summary.getBoundingClientRect().top - el.getBoundingClientRect().top - 20
    } else {
      el.scrollTop = el.scrollHeight
    }
  })
}

function trackScroll() {
  const el = streamEl.value
  if (el) followStream.value = el.scrollHeight - el.scrollTop - el.clientHeight < 100
}
function seenKey() { return `cyshuhe.chat-seen.v1.${auth.user?.id || 'session'}` }
function markSeen(task: TaskSummary) {
  seen.value[task.id] = conversationStamp(task)
  try { localStorage.setItem(seenKey(), JSON.stringify(seen.value)) } catch { /* Storage can be disabled. */ }
}
function isUnread(task: TaskSummary) {
  return seen.value[task.id] !== conversationStamp(task) && task.id !== detail.value?.id
}

function apply(d: TaskDetail) {
  const previousLatest = detail.value?.report_versions[0]?.version_no
  const followLatest = detail.value?.id !== d.id || reportVersionNo.value === previousLatest
  detail.value = d
  const summary: TaskSummary = {
    id: d.id, conversation_code: d.conversation_code, title: d.title, status: d.status,
    country_code: d.country_code, messages_count: d.messages.length,
    latest_report_version: d.report_versions[0]?.version_no || 0,
  }
  const index = tasks.value.findIndex(t => t.id === d.id)
  if (index >= 0) tasks.value[index] = summary
  else tasks.value.push(summary)
  if (document.visibilityState === 'visible') markSeen(summary)
  if (
    d.report_versions.length > 0 &&
    (followLatest || !d.report_versions.some((r) => r.version_no === reportVersionNo.value))
  ) {
    reportVersionNo.value = d.report_versions[0].version_no
  }
}

async function refreshTasks() {
  const request = ++listRequest
  const next = await api.get<TaskSummary[]>('/api/analysis/tasks')
  if (!mounted || request !== listRequest) return
  const previous = new Map(tasks.value.map(t => [t.id, t]))
  tasks.value = next
  lastSidebarRefresh = Date.now()
  for (const task of next) {
    const old = previous.get(task.id)
    if (old && task.id !== detail.value?.id && conversationStamp(old) !== conversationStamp(task)) {
      if (task.status === 'awaiting_user_facts') ElMessage.info(`${task.title}：有新的业务追问`)
      else if (task.latest_report_version > old.latest_report_version) ElMessage.success(`${task.title}：报告已生成`)
    }
  }
}

async function openTask(id: string) {
  const request = ++openRequest
  try {
    const next = await api.get<TaskDetail>(`/api/analysis/tasks/${id}`)
    if (request !== openRequest) return
    apply(next)
    if (route.query.task !== id) await router.push({ query: { ...route.query, task: id } })
    openChapters.value = []
    openExtras.value = []
    followStream.value = true
    scrollBottom(true)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载对话失败')
  }
}

async function newChat() {
  const request = ++openRequest
  try {
    const d = await api.post<TaskDetail>('/api/analysis/chats', {})
    if (request !== openRequest) return
    apply(d)
    await router.push({ query: { ...route.query, task: d.id } })
    await refreshTasks()
    followStream.value = true
    scrollBottom(true)
    inputEl.value?.focus()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '新建对话失败')
  }
}

async function send(text?: string) {
  const content = (text ?? input.value).trim()
  if (!content || !detail.value || inputDisabled.value) return
  const id = detail.value.id
  const request = openRequest
  const questionId = status.value === 'awaiting_user_facts' ? detail.value.pending_user_facts?.[0]?.id : undefined
  pendingSends.set(id, Date.now())
  try {
    input.value = ''
    // 先把用户的话显示出来,再等待助手回复
    detail.value.messages.push({
      id: `tmp-${Date.now()}`,
      seq_no: 9999,
      role: 'user',
      content,
      options: [],
      slot: null,
    })
    followStream.value = true
    scrollBottom(true)
    const next = await api.post<TaskDetail>(`/api/analysis/tasks/${id}/messages`, {
        content,
        question_id: questionId,
      })
    if (!mounted) return
    if (detail.value?.id === id && request === openRequest) apply(next)
    // The answer is saved and the next question is available. Refreshing the
    // sidebar must not keep the composer locked while another request is slow.
    void refreshTasks().catch(() => { /* A later refresh can update the sidebar. */ })
    scrollBottom()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '发送失败')
    if (mounted && detail.value?.id === id && request === openRequest) await openTask(id)
  } finally {
    pendingSends.delete(id)
  }
}

function clickChip(opt: string) {
  const slot = lastAssistant.value?.slot ?? ''
  if (CHOICE_SLOTS.has(slot)) {
    send(opt)
  } else {
    input.value = opt
    inputEl.value?.focus()
  }
}

function onEnter(ev: KeyboardEvent) {
  if (ev.shiftKey) return
  ev.preventDefault()
  send()
}

let timer: number | undefined
let clockTimer: number | undefined
async function poll() {
  if (polling || document.visibilityState !== 'visible') return
  polling = true
  try {
    if (progressState.value !== 'live') await refreshProgress()
    // A background conversation can finish while a different one is open.
    if (Date.now() - lastSidebarRefresh > 10000) {
      await refreshTasks()
      const summary = tasks.value.find(t => t.id === detail.value?.id)
      if (summary && detail.value && !sending.value &&
          (summary.status !== detail.value.status || summary.messages_count !== detail.value.messages.length ||
           summary.latest_report_version !== (detail.value.report_versions[0]?.version_no || 0))) {
        const id = detail.value.id
        const next = await api.get<TaskDetail>(`/api/analysis/tasks/${id}`)
        if (mounted && detail.value?.id === id) apply(next)
      }
    }
  } catch { /* A later refresh retries; preserve the conversation. */ }
  finally { polling = false }
}
async function refreshProgress() {
  if (detail.value?.status !== 'running') return
  const id = detail.value.id
  try {
    const next = await api.get<TaskDetail>(`/api/analysis/tasks/${id}`)
    if (!mounted || detail.value?.id !== id) return
    apply(next)
    scrollBottom()
  } catch {
    /* 下一轮重试 */
  }
}

watch(() => [detail.value?.id, detail.value?.status], () => {
  progressStream?.close()
  clearTimeout(progressRefresh)
  progressRefresh = undefined
  progressState.value = 'idle'
  if (!detail.value || detail.value.status !== 'running') return
  progressStream = startProgressStream({
    url: `/api/analysis/tasks/${detail.value.id}/events`, token: api.token,
    onState: state => { progressState.value = state },
    onUnauthorized: () => { api.setToken(null); window.location.assign(import.meta.env.BASE_URL + 'login') },
    onEvent: event => {
      if (event.type === 'connected' || progressRefresh !== undefined) return
      progressRefresh = setTimeout(() => {
        progressRefresh = undefined
        void refreshProgress()
      }, 80)
    },
  })
})

watch(
  () => [detail.value?.messages.length, detail.value?.progress.length, detail.value?.status],
  () => scrollBottom(),
)

async function openCitation(clauseId: string | null, quote = '') {
  if (!clauseId) {
    ElMessage.warning('该条目没有可定位的入库条款')
    return
  }
  anchorQuote.value = quote
  drawerVisible.value = true
  drawerLoading.value = true
  try {
    card.value = await api.get<CitationCard>(`/api/analysis/clauses/${clauseId}/citation-card`)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载条款失败')
  } finally {
    drawerLoading.value = false
  }
}

function openEdit() {
  if (!report.value) return
  editSummary.value = report.value.executive_summary
  for (const d of report.value.dimensions) editDims[d.dimension_code] = d.content_markdown
  editVisible.value = true
}

async function saveEdit() {
  if (!detail.value) return
  savingEdit.value = true
  try {
    const rv = await api.post<ReportVersion>(
      `/api/analysis/tasks/${detail.value.id}/reports/edit`,
      {
        change_note: editNote.value,
        executive_summary: editSummary.value,
        dimension_updates: { ...editDims },
      },
    )
    editVisible.value = false
    await openTask(detail.value.id)
    reportVersionNo.value = rv.version_no
    ElMessage.success(`已保存为新版本 v${rv.version_no}`)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '保存失败')
  } finally {
    savingEdit.value = false
  }
}

async function openDiff() {
  const vers = (detail.value?.report_versions ?? []).map((r) => r.version_no).sort((a, b) => a - b)
  if (!detail.value || vers.length < 2) {
    ElMessage.info('需要至少两个报告版本才能对比,请先「在线修订」保存新版本')
    return
  }
  try {
    diffData.value = await api.get<DiffPayload>(
      `/api/analysis/tasks/${detail.value.id}/reports/diff?from_version=${vers[0]}&to_version=${vers[vers.length - 1]}`,
    )
    diffVisible.value = true
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载对比失败')
  }
}

async function download(ext: 'docx' | 'pdf' | 'zip', dimension?: string) {
  if (!detail.value || !report.value || exportJob.value) return
  if (report.value.source_integrity_alerts?.length) {
    ElMessage.error('报告引用的原文来源异常，已停止导出，请核实来源后重新分析')
    return
  }
  const id = detail.value.id
  const country = detail.value.country_code
  const ver = report.value.version_no
  const label = `${dimension ? `${dimension} 章节` : '完整报告'} · ${ext.toUpperCase()} · v${ver}`
  exportJob.value = {taskId: id, label, started: Date.now(), phase: 'preparing', received: 0, total: null}
  try {
    const res = await fetch(`/api/analysis/tasks/${id}/reports/${ver}/export.${ext}${dimension ? '?dimension=' + encodeURIComponent(dimension) : ''}`, {
      headers: { Authorization: `Bearer ${api.token()}` },
    })
    const blob = await receiveExport(res, (received, total) => {
      if (exportJob.value) Object.assign(exportJob.value, {phase: 'receiving', received, total})
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `cyshuhe_${country}_${dimension || '8dim'}_report_v${ver}.${ext}`
    document.body.appendChild(a)
    a.click()
    a.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 30000)
    if (mounted) ElMessage.success(`${label} 已交给浏览器下载，请查看下载列表`)
  } catch (e: unknown) {
    if (mounted) ElMessage.error((e as Error).message || '导出失败，可重试')
  } finally {
    exportJob.value = null
  }
}

async function copyConversationId() {
  if (!detail.value) return
  const code = detail.value.conversation_code || detail.value.id
  try {
    await navigator.clipboard.writeText(code)
    ElMessage.success(`已复制对话 ID: ${code}`)
  } catch {
    ElMessage.info(`对话 ID: ${code}`)
  }
}

async function refreshRegulatoryUpdates() {
  const id = detail.value?.id
  if (!id || detail.value?.status === 'running') return
  try {
    const next = await api.get<TaskDetail>(`/api/analysis/tasks/${id}`)
    if (detail.value?.id === id) apply(next)
  } catch { /* The notification page exposes retryable loading errors. */ }
}
watch(() => route.query.task, (id) => {
  if (typeof id === 'string' && id !== detail.value?.id) void openTask(id)
})
onMounted(async () => {
  try {
    const saved = JSON.parse(localStorage.getItem(seenKey()) || '{}')
    if (saved && typeof saved === 'object' && !Array.isArray(saved)) seen.value = saved
  } catch { /* Unavailable or old browser state does not block chat. */ }
  clockTimer = window.setInterval(() => { clockNow.value = Date.now() }, 1000)
  window.addEventListener('regulatory-notifications-updated', refreshRegulatoryUpdates)
  try {
    await refreshTasks()
    if (typeof route.query.task === 'string') {
      await openTask(route.query.task)
    } else if (tasks.value.length === 0) {
      await newChat()
    } else {
      await openTask(tasks.value[tasks.value.length - 1].id)
    }
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载失败')
  }
  timer = window.setInterval(poll, 2000)
})
onUnmounted(() => {
  mounted = false
  window.removeEventListener('regulatory-notifications-updated', refreshRegulatoryUpdates)
  progressStream?.close()
  clearTimeout(progressRefresh)
  if (timer !== undefined) window.clearInterval(timer)
  if (clockTimer !== undefined) window.clearInterval(clockTimer)
})
</script>

<template>
  <div class="chat-page">
    <aside class="sessions">
      <div class="sessions-top">
        <strong>评估对话</strong>
        <el-button type="primary" size="small" data-testid="btn-new-chat" @click="newChat">
          + 新建评估
        </el-button>
      </div>
      <div class="session-list">
        <div
          v-for="t in [...tasks].reverse()"
          :key="t.id"
          class="session-item"
          :class="{ active: detail?.id === t.id }"
          data-testid="analysis-task-item"
          role="button"
          tabindex="0"
          :aria-current="detail?.id === t.id ? 'true' : undefined"
          @click="openTask(t.id)"
          @keydown.enter="openTask(t.id)"
          @keydown.space.prevent="openTask(t.id)"
        >
          <div class="s-title"><span class="session-dot" :class="conversationSignal(t).tone" aria-hidden="true" />{{ t.title }}</div>
          <div class="s-code" data-testid="session-conversation-code">
            {{ t.conversation_code || t.id.slice(0, 8) }}
          </div>
          <div class="session-state" :class="conversationSignal(t).tone">
            {{ conversationSignal(t).label }}<span v-if="isUnread(t)" class="unread-label">新动态</span>
          </div>
        </div>
      </div>
    </aside>

    <section class="chat-main">
      <header class="chat-head">
        <span class="chat-title">{{ detail?.title || '新对话' }}</span>
        <el-tag v-if="detail" size="small" :type="status === 'running' ? 'primary' : 'info'">
          {{ STATUS_LABEL[status] || status }}
        </el-tag>
        <div v-if="detail" class="conv-id-bar">
          <el-tag
            size="small"
            effect="plain"
            type="info"
            data-testid="chat-conversation-id"
            :title="`完整对话 UUID: ${detail.id}`"
          >
            {{ detail.conversation_code || detail.id }}
          </el-tag>
          <el-button
            size="small"
            link
            type="primary"
            data-testid="btn-copy-conversation-id"
            @click="copyConversationId"
          >
            复制编号
          </el-button>
        </div>
      </header>

      <div v-if="exportJob" class="export-feedback" data-testid="export-feedback" role="status" aria-live="polite">
        <span class="activity-spinner" aria-hidden="true" />
        <div><strong>{{ exportJob.phase === 'preparing' ? '正在生成文件' : '正在接收文件' }}</strong><span>{{ exportJob.label }} · {{ exportElapsed }}</span>
          <small v-if="exportJob.phase === 'preparing'">{{ exportJob.label.includes('PDF') ? '正在完成分页和字体排版，完成后自动下载。' : '文件准备好后自动下载。' }}</small>
          <small v-else>已接收 {{ Math.ceil(exportJob.received / 1024) }} KB<span v-if="exportJob.total"> / {{ Math.ceil(exportJob.total / 1024) }} KB</span></small>
        </div>
      </div>

      <div ref="streamEl" class="stream" data-testid="chat-stream" @scroll.passive="trackScroll">
        <template v-if="detail">
          <div
            v-for="m in detail.messages"
            :key="m.id"
            class="row"
            :class="m.role"
            :data-testid="`chat-msg-${m.role}`"
          >
            <div class="avatar">{{ m.role === 'assistant' ? '助' : '我' }}</div>
            <div class="bubble">{{ m.role === 'assistant' ? readableMessage(m.content) : m.content }}</div>
          </div>

          <div v-if="sending" class="row assistant" data-testid="chat-waiting" role="status" aria-live="polite">
            <div class="avatar">助</div>
            <div class="bubble waiting-bubble">
              <div class="activity-title"><span class="activity-spinner" aria-hidden="true" />正在整理您的回答</div>
              <p>正在更新业务信息并准备下一步。</p>
              <small>已等待 {{ sendElapsed }}<span v-if="pendingSends.get(detail.id) && clockNow - pendingSends.get(detail.id)! > 20000"> · 仍在处理，您可以先查看其他对话</span></small>
            </div>
          </div>

          <div v-if="showChips && !sending" class="chips" data-testid="chat-options">
            <span v-if="!CHOICE_SLOTS.has(lastAssistant?.slot ?? '')" class="chip-hint">
              以下只是示例回答,点击会填入输入框,请按您的实际情况修改后再发送:
            </span>
            <el-button
              v-for="opt in lastAssistant?.options"
              :key="opt"
              size="small"
              round
              data-testid="chat-option-chip"
              @click="clickChip(opt)"
            >
              {{ opt }}
            </el-button>
          </div>

          <el-alert v-if="status === 'awaiting_user_facts' && !sending" type="info" :closable="false" data-testid="analysis-user-fact-pause" title="请补充业务信息" description="回答上方问题后继续生成报告。" />
          <div v-if="status === 'running'" class="card progress" data-testid="progress-card" role="status" aria-live="polite">
            <div class="card-title"><span class="activity-title" data-testid="task-running-tag"><span class="activity-spinner" aria-hidden="true" />{{ detail.activity?.label || '正在评估您的业务场景' }}</span><small v-if="activityElapsed">本环节 {{ activityElapsed }}</small></div>
            <div class="progress-stages"><span v-for="(s,i) in detail.progress" :key="i" class="progress-stage" :class="s.state"><span aria-hidden="true">{{ s.state === 'complete' ? '✓' : s.state === 'failed' ? '!' : '·' }}</span> {{s.label}}</span></div>
          </div>
          <el-alert v-if="status === 'failed'" type="error" :closable="false" title="本次评估暂未完成" description="可输入“重试”重新分析，或将对话编号提供给管理员。" />

          <!-- 报告卡片 -->
          <div v-if="hasReport && report" class="card report" data-testid="report-card">
            <div class="card-title">
              <span>八维度合规评估报告 v{{ report.version_no }}</span>
              <el-tag
                :type="
                  report.status === 'analysis_failed' || report.source_integrity_alerts?.length
                    ? 'danger'
                    : report.is_partial_sample
                      ? 'warning'
                      : 'success'
                "
              >
                {{
                  report.status === 'analysis_failed'
                    ? '暂未完成'
                    : report.source_integrity_alerts?.length
                      ? '来源异常 · 暂停导出'
                      : report.is_partial_sample
                        ? '部分维度待补充'
                        : '报告草稿'
                }}
              </el-tag>
            </div>
            <p class="summary">{{ businessSummary(report.executive_summary) }}</p>
            <el-alert v-if="report.regulatory_updates?.length" type="warning" :closable="false"
                :title="report.regulatory_updates.some(u => u.state === 'open') ? '引用来源已更新，请查看更新通知并重新评估。' : '已有后续报告版本，可切换查看。'"
              data-testid="report-regulatory-update" style="margin-bottom:12px" />
            <el-alert
              v-for="alert in report.source_integrity_alerts"
              :key="alert"
              type="error"
              :closable="false"
              show-icon
              :title="alert"
              style="margin-bottom: 10px"
            />

            <el-alert
              v-for="(imp, i) in report.stale_impacts"
              :key="i"
              type="warning"
              :closable="false"
              show-icon
              :title="`${imp.dimension_code}: ${imp.amendment_summary}。${imp.action_required}`"
              data-testid="report-stale-impact-alert"
              style="margin-bottom: 6px"
            />

            <div class="actions">
              <el-select
                v-model="reportVersionNo"
                size="small"
                style="width: 110px"
                data-testid="report-version-select"
              >
                <el-option
                  v-for="r in detail.report_versions"
                  :key="r.version_no"
                  :label="`版本 v${r.version_no}`"
                  :value="r.version_no"
                />
              </el-select>
              <el-dropdown trigger="click"
                ><el-button type="primary" size="small" :loading="!!currentExport" :disabled="!!exportJob || report.status === 'analysis_failed'">{{ currentExport ? '正在导出' : '导出报告' }}</el-button
                ><template #dropdown
                  ><el-dropdown-menu
                    ><el-dropdown-item data-testid="btn-export-docx" @click="download('docx')"
                      >Word 文档</el-dropdown-item
                    ><el-dropdown-item data-testid="btn-export-pdf" @click="download('pdf')"
                      >PDF 文档</el-dropdown-item
                    ><el-dropdown-item data-testid="btn-export-zip" @click="download('zip')"
                      >整份与八章 Word 压缩包</el-dropdown-item
                    ></el-dropdown-menu
                  ></template
                ></el-dropdown
              >
              <el-button size="small" data-testid="btn-edit-report" @click="openEdit"
                >在线修订</el-button
              >
              <el-button size="small" data-testid="btn-diff-report" @click="openDiff"
                >版本对比</el-button
              >
            </div>

            <div class="sub-title">八个维度(点开查看,引用可点击溯源)</div>
            <el-collapse v-model="openChapters">
              <el-collapse-item
                v-for="d in report.dimensions"
                :key="d.dimension_code"
                :name="d.dimension_code"
                :data-testid="`report-chapter-${d.dimension_code}`"
              >
                <template #title>
                  <b>{{ d.dimension_code }}</b
                  >&nbsp;{{ d.dimension_title }}&nbsp;
                  <el-tag size="small" :type="d.status === 'COMPLETE' ? 'success' : 'warning'">
                    {{ d.status === 'COMPLETE' ? '分析完成' : '有缺口' }}
                  </el-tag>
                  &nbsp;
                  <el-tag size="small" :type="d.risk_level === 'HIGH' ? 'danger' : 'info'">
                    {{ RISK_LABEL[d.risk_level] || d.risk_level }}
                  </el-tag>
                </template>
                <div style="display:flex;gap:8px;margin:8px 0 12px">
                  <el-button size="small" :disabled="!!exportJob" :data-testid="`export-${d.dimension_code}-docx`" @click="download('docx', d.dimension_code)">导出本章 Word</el-button>
                  <el-button size="small" :disabled="!!exportJob" :data-testid="`export-${d.dimension_code}-pdf`" @click="download('pdf', d.dimension_code)">导出本章 PDF</el-button>
                </div>
                <el-alert
                  v-if="d.missing_gap_note"
                  type="warning"
                  :closable="false"
                  show-icon
                  :title="d.missing_gap_note"
                  style="margin-bottom: 8px"
                />
                <div class="md">{{ readableContent(d.content_markdown) }}</div>
                <ul v-if="d.key_recommendations.length" class="recs">
                  <li v-for="(r, i) in d.key_recommendations" :key="i">
                    {{ r }}
                  </li>
                </ul>
                <div class="cites">
                  <el-button
                    v-for="c in d.citations"
                    :key="c.citation_tag"
                    size="small"
                    type="primary"
                    plain
                    data-testid="report-citation-badge"
                    @click="openCitation(c.clause_id, c.quote_original)"
                  >
                    {{ c.citation_tag }} {{ c.document_title.slice(0, 22) }} ·
                    {{ c.clause_number }}
                  </el-button>
                </div>
              </el-collapse-item>
            </el-collapse>

            <el-collapse v-model="openExtras" style="margin-top: 10px">
              <el-collapse-item
                name="matrix"
                title="合规判定矩阵(业务事实 | 适用条款 | 合规义务 | 风险等级 | 判断依据)"
              >
                <el-table
                  :data="versionFindings"
                  border
                  size="small"
                  data-testid="compliance-findings-table"
                >
                  <template #empty>该版本未保存判定矩阵快照，请查阅各章引用。</template>
                  <el-table-column prop="business_fact" label="业务事实" min-width="140" />
                  <el-table-column label="适用条款" min-width="170">
                    <template #default="{ row }">
                      <el-button
                        link
                        type="primary"
                        data-testid="finding-clause-link"
                        @click="openCitation(row.clause_id, row.basis_quote)"
                      >
                        {{ row.document_title.slice(0, 24) }} ·
                        {{ row.clause_number }}
                      </el-button>
                    </template>
                  </el-table-column>
                  <el-table-column prop="compliance_obligation" label="合规义务" min-width="200" />
                  <el-table-column label="风险" width="80">
                    <template #default="{ row }">{{
                      RISK_LABEL[row.risk_level] || row.risk_level
                    }}</template>
                  </el-table-column>
                  <el-table-column label="判断依据(原文)" min-width="200">
                    <template #default="{ row }">
                      <i>"{{ row.basis_quote }}"</i>
                      <div class="muted">中文对照：{{ row.basis_zh }}</div>
                    </template>
                  </el-table-column>
                </el-table>
              </el-collapse-item>

            </el-collapse>
          </div>
        </template>
      </div>

      <footer class="composer">
        <el-input
          ref="inputEl"
          v-model="input"
          type="textarea"
          :rows="2"
          resize="none"
          :disabled="inputDisabled"
          :placeholder="inputPlaceholder"
          data-testid="chat-input"
          @keydown.enter="onEnter"
        />
        <el-button
          type="primary"
          :loading="sending"
          :disabled="inputDisabled || !input.trim()"
          data-testid="chat-send"
          @click="send()"
        >
          发送
        </el-button>
      </footer>
    </section>

    <el-drawer v-model="drawerVisible" title="引用来源与术语释义" size="60%">
      <div v-loading="drawerLoading" data-testid="citation-detail-drawer">
        <el-alert v-for="alert in card?.source_integrity_alerts" :key="alert"
          :title="alert" type="warning" :closable="false" show-icon />
        <template v-if="card">
          <h4>
            {{ card.document_title }} · {{ card.clause_number }}
            <el-tag size="small">v{{ card.version_no }}</el-tag>
            <el-tag
              size="small"
              :type="
                card.review_status === 'approved'
                  ? 'success'
                  : card.translated_text
                    ? 'warning'
                    : 'info'
              "
            >
              {{
                card.review_status === 'approved'
                  ? '已复核'
                  : card.translated_text
                    ? '中文对照'
                    : '未翻译'
              }}
            </el-tag>
          </h4>
          <details class="muted"><summary>资料信息</summary><p>原文：{{ card.source_review_status === 'approved' ? '已复核' : '未人工复核' }}；中文：{{ card.translation_review_status === 'approved' ? '已复核' : '机器翻译' }}</p></details>
          <div v-if="card.source_url" class="muted">
            官方来源:<a :href="card.source_url" target="_blank" rel="noopener">{{
              card.source_url
            }}</a>
          </div>
          <div class="dual">
            <div>
              <div class="col-label">官方原文</div>
              <LegalText :text="card.original_text" :terms="card.terms" :clause-id="card.clause_id" :section-path="card.section_path" />
              <div v-if="anchorQuote" class="anchor">
                报告锚定的原文片段:<mark>{{ anchorQuote }}</mark>
              </div>
            </div>
            <div>
              <div class="col-label">中文对照</div>
              <LegalText :text="card.translated_text || '暂无中文对照'" :terms="card.terms" :clause-id="card.clause_id" :section-path="card.section_path" />
            </div>
          </div>
          <div v-if="card.related_requirements?.length" class="col-label" style="margin-top:24px">相关合规要求</div>
          <article v-for="r in card.related_requirements" :key="r.id" class="step"><span class="muted">{{r.topic_label}}</span><h4>{{r.title}}</h4><p>{{r.quote}}</p></article>
        </template>
      </div>
    </el-drawer>

    <el-dialog v-model="editVisible" title="在线修订并保存为新版本" width="720px">
      <el-input v-model="editNote" placeholder="修订说明" style="margin-bottom: 8px" />
      <el-input v-model="editSummary" type="textarea" :rows="3" style="margin-bottom: 8px" />
      <div v-if="report" style="max-height: 340px; overflow-y: auto">
        <div v-for="d in report.dimensions" :key="d.dimension_code" style="margin-bottom: 8px">
          <b>{{ d.dimension_code }} {{ d.dimension_title }}</b>
          <el-input v-model="editDims[d.dimension_code]" type="textarea" :rows="3" />
        </div>
      </div>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="savingEdit"
          data-testid="btn-confirm-save-report-edit"
          @click="saveEdit"
        >
          保存为新版本
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="diffVisible" title="报告版本对比" width="780px">
      <div v-if="diffData" data-testid="report-diff-content">
        <div
          v-for="x in diffData.dimension_diffs"
          :key="x.dimension_code"
          class="diff-row"
          :class="{ changed: x.changed }"
        >
          <b>{{ x.dimension_code }} {{ x.dimension_title }}</b>
          <el-tag size="small" :type="x.changed ? 'warning' : 'info'">{{
            x.changed ? '有修订' : '一致'
          }}</el-tag>
          <div v-if="x.changed" class="dual">
            <pre>{{ x.from_markdown }}</pre>
            <pre>{{ x.to_markdown }}</pre>
          </div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.chat-page {
  display: flex;
  height: calc(100vh - 100px);
  min-height: 640px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  overflow: hidden;
}
.sessions {
  width: 240px;
  background: #fff;
  border-right: 1px solid #e2e8f0;
  display: flex;
  flex-direction: column;
}
.sessions-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px;
  border-bottom: 1px solid #f1f5f9;
}
.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}
.session-item {
  padding: 8px 10px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  margin-bottom: 8px;
  cursor: pointer;
}
.session-item.active {
  border-color: #2563eb;
  background: #eff6ff;
}
.s-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 2px;
  word-break: break-all;
}
.s-code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 11px;
  color: #64748b;
  margin-bottom: 4px;
}
.conv-id-bar {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.chat-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.chat-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 18px;
  background: #fff;
  border-bottom: 1px solid #e2e8f0;
}
.chat-title {
  font-weight: 700;
}
.stream {
  flex: 1;
  overflow-y: auto;
  padding: 18px;
}
.row {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
}
.row.user {
  flex-direction: row-reverse;
}
.avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: #1d4ed8;
  color: #fff;
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.row.user .avatar {
  background: #0f766e;
}
.bubble {
  max-width: 70%;
  padding: 10px 14px;
  border-radius: 12px;
  background: #fff;
  border: 1px solid #e2e8f0;
  white-space: pre-line;
  line-height: 1.65;
  font-size: 14px;
}
.row.user .bubble {
  background: #dbeafe;
  border-color: #bfdbfe;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0 0 14px 42px;
  align-items: center;
}
.chip-hint {
  width: 100%;
  font-size: 12px;
  color: #92400e;
}
.card {
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 14px 16px;
  margin: 0 0 14px 42px;
}
.card.progress {
  border-color: #93c5fd;
}
.card-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 700;
  margin-bottom: 8px;
}
.step {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 8px 10px;
  margin-bottom: 8px;
  font-size: 13px;
  background: #f8fafc;
}
.step.back {
  border-color: #f97316;
  background: #fff7ed;
}
.step-head {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 4px;
}
.back-reason {
  margin-top: 4px;
  color: #9a3412;
  white-space: pre-line;
}
.muted {
  color: #64748b;
  font-size: 12px;
}
.summary {
  white-space: pre-line;
  line-height: 1.7;
  margin: 0 0 10px;
}
.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin: 8px 0 12px;
}
.sub-title {
  font-weight: 600;
  margin-bottom: 6px;
}
.md {
  white-space: pre-line;
  line-height: 1.7;
  font-size: 13.5px;
}
.recs {
  margin: 8px 0;
  padding-left: 20px;
  font-size: 13px;
}
.cites {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.composer {
  display: flex;
  gap: 10px;
  padding: 12px 18px;
  background: #fff;
  border-top: 1px solid #e2e8f0;
  align-items: flex-end;
}
.dual {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 8px;
}
.col-label {
  font-weight: 700;
  font-size: 12.5px;
  color: #475569;
  margin-bottom: 4px;
}
.anchor {
  margin-top: 8px;
  padding: 6px 8px;
  background: #fef9c3;
  border-radius: 6px;
  font-size: 12px;
}
.diff-row {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 8px 10px;
  margin-bottom: 8px;
}
.diff-row.changed {
  border-color: #f59e0b;
  background: #fffbeb;
}
.diff-row pre {
  white-space: pre-wrap;
  font-size: 12px;
  margin: 0;
}

.chat-page {
  height: calc(100dvh - 145px);
  min-height: 650px;
  border-color: var(--cy-line);
  border-radius: 14px;
  background: #f7f9fc;
}
.sessions {
  width: 240px;
  flex-shrink: 0;
  background: #fbfcfe;
  border-color: var(--cy-line);
}
.sessions-top {
  padding: 20px 16px;
  gap: 12px;
}
.session-list {
  padding: 12px;
}
.session-item {
  padding: 14px;
  border-color: transparent;
  background: transparent;
  margin-bottom: 8px;
  transition: background 0.15s;
}
.session-item:hover {
  background: #f0f4fa;
}
.session-item.active {
  background: #eaf0fc;
  border-color: #d4dff6;
}
.s-title {
  font-size: 14px;
  line-height: 1.6;
  margin-bottom: 6px;
}
.s-code {
  font-size: 11px;
  color: #98a5b2;
}
.chat-head {
  padding: 18px 24px;
  flex-wrap: wrap;
  gap: 10px;
  min-height: 76px;
}
.chat-title {
  font-size: 16px;
  font-weight: 600;
}
.conv-id-bar {
  font-size: 12px;
}
.stream {
  padding: 30px 28px;
}
.row {
  gap: 12px;
  margin-bottom: 24px;
}
.avatar {
  border-radius: 11px;
  width: 34px;
  height: 34px;
  font-size: 13px;
  background: #4767a5;
}
.bubble {
  font-size: 15px;
  line-height: 1.9;
  padding: 16px 20px;
  border-color: #e8edf3;
  max-width: 85%;
}
.row.user .bubble {
  background: #eaf0ff;
  border-color: #dce5f8;
}
.row.user .avatar {
  background: #5c8d84;
}
.card {
  padding: 22px 24px;
  margin-left: 46px;
  border-color: #e4eaf1;
}
.card-title {
  flex-wrap: wrap;
  gap: 12px;
  font-size: 16px;
}
.summary {
  font-size: 15px;
  line-height: 1.9;
}
.composer {
  padding: 18px 24px;
  gap: 12px;
}
.composer .el-button {
  min-height: 44px;
}
.composer .el-textarea__inner {
  min-height: 48px;
}
.md {
  font-size: 15px;
  line-height: 1.9;
}
.actions {
  gap: 10px;
  margin: 20px 0;
}
.sub-title {
  font-size: 14px;
  color: #738492;
  margin-top: 26px;
}
.step {
  font-size: 14px;
  padding: 14px;
}
.muted {
  font-size: 12px;
}
.recs {
  font-size: 14px;
  line-height: 1.9;
}
.el-collapse-item {
  margin-top: 4px;
}
.session-dot { display: inline-block; width: 8px; height: 8px; margin: 0 8px 1px 0; border-radius: 50%; background: #a2afbf; flex-shrink: 0; }
.session-dot.green { background: #159b71; }
.session-dot.amber { background: #d99a23; }
.session-dot.blue { background: #4168e8; }
.session-dot.red { background: #df6666; }
.session-state { margin-top: 10px; font-size: 12px; line-height: 1.6; color: #7b899a; }
.session-state.green { color: #168563; }
.session-state.amber { color: #9d711a; }
.session-state.blue { color: #4163c8; }
.session-state.red { color: #bb4c4c; }
.unread-label { display: inline-block; margin-left: 7px; padding: 0 5px; border-radius: 4px; background: #e8efff; color: #365bc7; font-size: 11px; }
.activity-title { display: flex; align-items: center; gap: 10px; }
.activity-spinner { display: inline-block; width: 15px; height: 15px; flex-shrink: 0; border: 2px solid #dce5f9; border-top-color: #5275cf; border-radius: 50%; animation: activity-spin 1s linear infinite; }
.waiting-bubble { min-width: 230px; }
.waiting-bubble p { margin: 7px 0; color: #78889a; font-size: 13px; }
.waiting-bubble small, .progress small { font-size: 12px; color: #8794a4; font-weight: 400; }
.progress-stages { display: flex; flex-wrap: wrap; gap: 10px 18px; margin-top: 16px; }
.progress-stage { font-size: 13px; color: #708197; }
.progress-stage.complete > span { color: #1c9576; }
.progress-stage.warning, .progress-stage.waiting { color: #a17b37; }
.progress-stage.failed { color: #b45959; }
.export-feedback { display: flex; align-items: center; gap: 12px; margin: 12px 24px 0; padding: 12px 16px; border-radius: 10px; background: #eef3ff; color: #395ca7; font-size: 13px; }
.export-feedback strong { margin-right: 12px; font-weight: 600; }
.export-feedback small { display: block; margin-top: 5px; color: #71829e; font-size: 12px; }
@keyframes activity-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .activity-spinner { animation: none; border-top-color: #5275cf; } }
@media (max-width: 1100px) {
  .sessions {
    width: 205px;
  }
  .stream {
    padding: 24px 18px;
  }
  .card {
    margin-left: 0;
  }
  .conv-id-bar {
    margin-left: 0;
    width: 100%;
  }
}
@media (max-width: 800px) {
  .chat-page {
    flex-direction: column;
    height: calc(100dvh - 115px);
    min-height: 680px;
  }
  .sessions {
    width: 100%;
    max-height: 170px;
    border-right: 0;
    border-bottom: 1px solid var(--cy-line);
  }
  .sessions-top {
    padding: 12px 16px;
  }
  .session-list {
    display: flex;
    overflow-x: auto;
    gap: 10px;
    flex: 1;
    padding: 8px 12px;
  }
  .session-item {
    min-width: 200px;
    margin-bottom: 0;
  }
  .s-code {
    display: none;
  }
  .session-item .el-tag {
    font-size: 11px;
  }
  .chat-main {
    min-height: 0;
  }
  .chat-head {
    padding: 14px 16px;
  }
  .stream {
    padding: 20px 14px;
  }
  .bubble {
    max-width: 90%;
  }
  .card {
    padding: 18px 16px;
  }
  .composer {
    padding: 12px;
  }
  .dual {
    grid-template-columns: 1fr;
  }
}
</style>
