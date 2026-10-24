<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api } from '../api'
import KgForceGraph, {
  type FgCategory,
  type FgEdge,
  type FgNode,
} from '../components/KgForceGraph.vue'

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

interface StepTrace {
  id: string
  seq_no: number
  agent_name: string
  status: string
  business_summary: string
  is_backtrack: boolean
  backtrack_reason: string | null
  backtrack_resolution: string | null
  tool_calls: Array<{
    call_id: string
    tool_label: string
    result_summary: string
    duration_ms: number
  }>
  tokens_used: number
  duration_ms: number
}

interface Citation {
  citation_tag: string
  clause_id: string | null
  document_title: string
  clause_number: string
  version_no: number
  quote_original: string
}

interface Dimension {
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
  id: string
  conversation_code: string
  title: string
  status: string
  country_code: string
  latest_report_version: number
}

interface TaskDetail {
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
  step_traces: StepTrace[]
  report_versions: ReportVersion[]
  total_tokens: number
  total_duration_ms: number
  mcp_calls_count: number
  backtrack_count: number
}

interface MiniKgNode {
  id: string
  node_type: string
  name_zh: string
}
interface MiniKgEdge {
  source_node_id: string
  target_node_id: string
  relation_type: string
  review_status: string
}
interface CitationCard {
  source_integrity_alerts?: string[]
  document_title: string
  clause_number: string
  version_no: number
  source_url: string | null
  original_text: string
  translated_text: string
  review_status: string
  mini_graph: { nodes: MiniKgNode[]; edges: MiniKgEdge[] }
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
  failed: '分析失败',
}
const FG_CATEGORIES: FgCategory[] = [
  { type: 'COUNTRY', name: '国家/地区', color: '#1d4ed8' },
  { type: 'REGULATOR', name: '监管机构', color: '#0f766e' },
  { type: 'INSTRUMENT', name: '法规文件', color: '#7c3aed' },
  { type: 'OBLIGATION', name: '合规义务', color: '#b45309' },
  { type: 'DATA_CATEGORY', name: '数据类型', color: '#0369a1' },
  { type: 'PENALTY', name: '违规罚则', color: '#be123c' },
]
const RELATION_LABEL: Record<string, string> = {
  PUBLISHES: '发布',
  AMENDS: '修订',
  REPEALS: '废止',
  GOVERNS: '约束',
  PROHIBITS: '禁止',
  REQUIRES: '强制要求',
  EXEMPTS: '豁免',
  SUPERVISES: '监管',
  PENALIZES: '处罚',
  APPLIES_TO_SCENARIO: '适用场景',
  RESTRICTS_CROSS_BORDER: '数据出境限制',
  REQUIRES_ADMISSION: '准入要求',
}
// 这两类问题直接点选即发送;事实类问题的示例只填入输入框,由用户按实际情况修改后再发送
const CHOICE_SLOTS = new Set(['country_code', 'scenario_type'])

const tasks = ref<TaskSummary[]>([])
const detail = ref<TaskDetail | null>(null)
const input = ref('')
const sending = ref(false)
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
const lastAssistant = computed(() => {
  const list = detail.value?.messages.filter((m) => m.role === 'assistant') ?? []
  return list[list.length - 1]
})
const showChips = computed(
  () =>
    status.value === 'collecting_facts' &&
    !!lastAssistant.value &&
    lastAssistant.value.options.length > 0 &&
    detail.value?.messages[detail.value.messages.length - 1]?.role === 'assistant',
)
const hasReport = computed(
  () => (detail.value?.report_versions.length ?? 0) > 0 && status.value !== 'running',
)
const report = computed<ReportVersion | null>(() => {
  const list = detail.value?.report_versions ?? []
  return list.find((r) => r.version_no === reportVersionNo.value) ?? list[0] ?? null
})
const inputPlaceholder = computed(() => {
  if (status.value === 'running') return '多智能体分析进行中,请稍候…'
  if (status.value === 'completed' || status.value === 'partial_sample')
    return '报告已生成。可在上方导出,或点击左侧「新建评估」评估新的场景'
  const slot = lastAssistant.value?.slot
  if (slot === 'country_code') return '输入或点选目标国家…'
  if (slot === 'vehicle_model') return '输入车型或车队名称…'
  return '请输入您的回答,Enter 发送,Shift+Enter 换行'
})
const inputDisabled = computed(() => sending.value || status.value === 'running')

const citationNodes = computed<FgNode[]>(() => {
  const mg = card.value?.mini_graph
  if (!mg) return []
  const deg = new Map<string, number>()
  for (const e of mg.edges) {
    deg.set(e.source_node_id, (deg.get(e.source_node_id) || 0) + 1)
    deg.set(e.target_node_id, (deg.get(e.target_node_id) || 0) + 1)
  }
  return mg.nodes.map((n) => ({
    id: n.id,
    name: n.name_zh,
    type: n.node_type,
    degree: deg.get(n.id) || 1,
  }))
})
const citationEdges = computed<FgEdge[]>(() =>
  (card.value?.mini_graph.edges ?? []).map((e) => ({
    source: e.source_node_id,
    target: e.target_node_id,
    label: RELATION_LABEL[e.relation_type] || e.relation_type,
    approved: e.review_status === 'approved',
  })),
)

function scrollBottom() {
  nextTick(() => {
    const el = streamEl.value
    if (!el) return
    const summary = el.querySelector<HTMLElement>('[data-testid="report-card"]')
    if (hasReport.value && summary) {
      el.scrollTop += summary.getBoundingClientRect().top - el.getBoundingClientRect().top - 20
    } else {
      el.scrollTop = el.scrollHeight
    }
  })
}

function apply(d: TaskDetail) {
  detail.value = d
  if (
    d.report_versions.length > 0 &&
    !d.report_versions.some((r) => r.version_no === reportVersionNo.value)
  ) {
    reportVersionNo.value = d.report_versions[0].version_no
  }
}

async function refreshTasks() {
  tasks.value = await api.get<TaskSummary[]>('/api/analysis/tasks')
}

async function openTask(id: string) {
  try {
    apply(await api.get<TaskDetail>(`/api/analysis/tasks/${id}`))
    openChapters.value = []
    openExtras.value = []
    scrollBottom()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载对话失败')
  }
}

async function newChat() {
  try {
    const d = await api.post<TaskDetail>('/api/analysis/chats', {})
    apply(d)
    await refreshTasks()
    scrollBottom()
    inputEl.value?.focus()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '新建对话失败')
  }
}

async function send(text?: string) {
  const content = (text ?? input.value).trim()
  if (!content || !detail.value || inputDisabled.value) return
  sending.value = true
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
    scrollBottom()
    apply(
      await api.post<TaskDetail>(`/api/analysis/tasks/${detail.value.id}/messages`, { content }),
    )
    await refreshTasks()
    scrollBottom()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '发送失败')
    if (detail.value) await openTask(detail.value.id)
  } finally {
    sending.value = false
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
async function poll() {
  if (detail.value?.status !== 'running') return
  try {
    apply(await api.get<TaskDetail>(`/api/analysis/tasks/${detail.value.id}`))
    await refreshTasks()
    scrollBottom()
  } catch {
    /* 下一轮重试 */
  }
}

watch(
  () => [detail.value?.messages.length, detail.value?.step_traces.length, detail.value?.status],
  scrollBottom,
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

async function download(ext: 'docx' | 'pdf') {
  if (!detail.value || !report.value) return
  if (report.value.source_integrity_alerts?.length) {
    ElMessage.error('报告引用的原文来源异常，已停止导出，请核实来源后重新分析')
    return
  }
  try {
    const ver = report.value.version_no
    const res = await fetch(`/api/analysis/tasks/${detail.value.id}/reports/${ver}/export.${ext}`, {
      headers: { Authorization: `Bearer ${api.token()}` },
    })
    if (!res.ok) throw new Error(`导出失败 (${res.status})`)
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `cyshuhe_${detail.value.country_code}_8dim_report_v${ver}.${ext}`
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success(`已导出 ${ext.toUpperCase()} 报告 (v${ver})`)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '导出失败')
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

onMounted(async () => {
  try {
    await refreshTasks()
    if (tasks.value.length === 0) {
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
  if (timer !== undefined) window.clearInterval(timer)
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
          @click="openTask(t.id)"
        >
          <div class="s-title">{{ t.title }}</div>
          <div class="s-code" data-testid="session-conversation-code">
            {{ t.conversation_code || t.id.slice(0, 8) }}
          </div>
          <el-tag
            size="small"
            :type="
              t.status === 'completed'
                ? 'success'
                : t.status === 'running'
                  ? 'primary'
                  : t.status === 'failed'
                    ? 'danger'
                    : 'info'
            "
          >
            {{ STATUS_LABEL[t.status] || t.status }}
          </el-tag>
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

      <div ref="streamEl" class="stream" data-testid="chat-stream">
        <template v-if="detail">
          <div
            v-for="m in detail.messages"
            :key="m.id"
            class="row"
            :class="m.role"
            :data-testid="`chat-msg-${m.role}`"
          >
            <div class="avatar">{{ m.role === 'assistant' ? '助' : '我' }}</div>
            <div class="bubble">{{ m.content }}</div>
          </div>

          <div v-if="showChips" class="chips" data-testid="chat-options">
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

          <!-- 分析进度(实时) -->
          <div v-if="status === 'running'" class="card progress" data-testid="progress-card">
            <div class="card-title">
              <span data-testid="task-running-tag">多智能体分析进行中…</span>
              <el-tag size="small" type="primary">已完成 {{ detail.step_traces.length }} 步</el-tag>
            </div>
            <div
              v-for="s in detail.step_traces"
              :key="s.id"
              class="step"
              :class="{ back: s.is_backtrack }"
            >
              <div class="step-head">
                <b>{{ s.agent_name }}</b>
                <el-tag v-if="s.is_backtrack" size="small" type="danger">退回补证</el-tag>
                <span class="muted">{{ s.duration_ms }} ms · {{ s.tokens_used }} tokens</span>
              </div>
              <div>{{ s.business_summary }}</div>
              <div v-if="s.backtrack_reason" class="back-reason">
                退回原因:{{ s.backtrack_reason }}
              </div>
            </div>
            <div class="muted" style="margin-top: 6px">正在进行下一步,请稍候…</div>
          </div>

          <div v-if="status === 'failed'" class="card">
            <el-alert type="error" :closable="false" show-icon title="分析失败">
              <template #default>
                {{ detail.step_traces[detail.step_traces.length - 1]?.business_summary }}。
                在下方输入任意内容(如「重试」)即可重新分析。
              </template>
            </el-alert>
          </div>

          <!-- 报告卡片 -->
          <div v-if="hasReport && report" class="card report" data-testid="report-card">
            <div class="card-title">
              <span>八维度合规评估报告 v{{ report.version_no }}</span>
              <el-tag
                :type="
                  report.source_integrity_alerts?.length
                    ? 'danger'
                    : report.is_partial_sample
                      ? 'warning'
                      : 'success'
                "
              >
                {{
                  report.source_integrity_alerts?.length
                    ? '来源异常 · 暂停导出'
                    : report.is_partial_sample
                      ? '部分样稿 · 有维度缺依据'
                      : '全部维度均有核验依据'
                }}
              </el-tag>
            </div>
            <p class="summary">{{ report.executive_summary }}</p>
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
                ><el-button type="primary" size="small">导出报告</el-button
                ><template #dropdown
                  ><el-dropdown-menu
                    ><el-dropdown-item data-testid="btn-export-docx" @click="download('docx')"
                      >Word 文档</el-dropdown-item
                    ><el-dropdown-item data-testid="btn-export-pdf" @click="download('pdf')"
                      >PDF 文档</el-dropdown-item
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
                    {{ d.status === 'COMPLETE' ? '依据已核验' : '有缺口' }}
                  </el-tag>
                  &nbsp;
                  <el-tag size="small" :type="d.risk_level === 'HIGH' ? 'danger' : 'info'">
                    {{ RISK_LABEL[d.risk_level] || d.risk_level }}
                  </el-tag>
                </template>
                <el-alert
                  v-if="d.missing_gap_note"
                  type="warning"
                  :closable="false"
                  show-icon
                  :title="d.missing_gap_note"
                  style="margin-bottom: 8px"
                />
                <div class="md">{{ d.content_markdown }}</div>
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
                  :data="detail.findings"
                  border
                  size="small"
                  data-testid="compliance-findings-table"
                >
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
                      <div class="muted">中文为机器翻译:{{ row.basis_zh }}</div>
                    </template>
                  </el-table-column>
                </el-table>
              </el-collapse-item>
              <el-collapse-item
                name="process"
                :title="`协同过程(${detail.step_traces.length} 步 · ${detail.mcp_calls_count} 次工具调用 · ${detail.total_tokens} tokens · 退回补证 ${detail.backtrack_count} 次)`"
              >
                <div
                  v-for="s in detail.step_traces"
                  :key="s.id"
                  class="step"
                  :class="{ back: s.is_backtrack }"
                  :data-testid="s.is_backtrack ? 'backtrack-step-card' : 'normal-step-card'"
                >
                  <div class="step-head">
                    <b>#{{ s.seq_no }} {{ s.agent_name }}</b>
                    <el-tag v-if="s.is_backtrack" size="small" type="danger">退回补证</el-tag>
                    <span class="muted">{{ s.duration_ms }} ms · {{ s.tokens_used }} tokens</span>
                  </div>
                  <div>{{ s.business_summary }}</div>
                  <div v-if="s.backtrack_reason" class="back-reason">
                    退回原因:{{ s.backtrack_reason }}
                    <div v-if="s.backtrack_resolution">补证结果:{{ s.backtrack_resolution }}</div>
                  </div>
                  <div v-for="c in s.tool_calls" :key="c.call_id" class="muted">
                    · {{ c.tool_label }}({{ c.duration_ms }} ms):{{ c.result_summary }}
                  </div>
                </div>
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

    <el-drawer v-model="drawerVisible" title="引用溯源 · 官方原文与关联关系" size="60%">
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
                    ? '中文译文（机器翻译，待核对）'
                    : '未翻译'
              }}
            </el-tag>
          </h4>
          <div v-if="card.source_url" class="muted">
            官方来源:<a :href="card.source_url" target="_blank" rel="noopener">{{
              card.source_url
            }}</a>
          </div>
          <div class="dual">
            <div>
              <div class="col-label">官方原文</div>
              <div class="md">{{ card.original_text }}</div>
              <div v-if="anchorQuote" class="anchor">
                报告锚定的原文片段:<mark>{{ anchorQuote }}</mark>
              </div>
            </div>
            <div>
              <div class="col-label">中文译文（机器翻译，待核对）</div>
              <div class="md">{{ card.translated_text || '暂无中文译文' }}</div>
            </div>
          </div>
          <div class="col-label" style="margin-top: 12px">该条款关联关系</div>
          <KgForceGraph
            v-if="drawerVisible && citationNodes.length > 0"
            :nodes="citationNodes"
            :edges="citationEdges"
            :categories="FG_CATEGORIES"
            height="340px"
          />
          <el-empty v-else description="该条款暂无图谱关联" :image-size="60" />
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
