<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api } from '../api'
import KgForceGraph, {
  type FgCategory,
  type FgEdge,
  type FgNode,
} from '../components/KgForceGraph.vue'

interface FollowUpQuestion {
  fact_key: string
  dimension_code: string
  question_zh: string
  why_needed: string
  suggested_options: string[]
  current_value: string | null
  is_fulfilled: boolean
}

interface FindingItem {
  finding_id: string
  dimension_code: string
  business_fact: string
  clause_id: string | null
  document_id: string | null
  document_title: string
  clause_number: string
  version_no: number
  source_url: string | null
  compliance_obligation: string
  risk_level: string
  basis_quote: string
  basis_zh: string
  verification_status: string
}

interface StepTraceItem {
  id: string
  seq_no: number
  agent_node_id: string
  agent_name: string
  phase: string
  status: string
  business_summary: string
  is_backtrack: boolean
  backtrack_reason: string | null
  backtrack_resolution: string | null
  tool_calls: Array<{
    call_id: string
    tool_name: string
    tool_label: string
    arguments: Record<string, unknown>
    result_summary: string
    duration_ms: number
    verified: boolean
  }>
  tokens_used: number
  duration_ms: number
}

interface CitationItem {
  citation_tag: string
  clause_id: string | null
  document_id: string | null
  document_title: string
  clause_number: string
  version_no: number
  source_url: string | null
  quote_original: string
  quote_zh: string
  review_status: string
}

interface ReportDimension {
  dimension_code: string
  dimension_title: string
  status: string
  risk_level: string
  content_markdown: string
  key_recommendations: string[]
  citations: CitationItem[]
  missing_gap_note: string | null
}

interface StaleImpactAlert {
  dimension_code: string
  clause_ref: string
  amendment_summary: string
  action_required: string
  detected_at: string
}

interface ReportVersion {
  id: string
  task_id: string
  version_no: number
  status: string
  is_partial_sample: boolean
  change_note: string
  executive_summary: string
  dimensions: ReportDimension[]
  stale_impacts: StaleImpactAlert[]
  created_at: string
}

interface TaskSummary {
  id: string
  title: string
  country_code: string
  country_name_zh: string
  vehicle_model: string
  scenario_type: string
  scenario_label: string
  status: string
  sufficiency_score: number
  missing_facts_count: number
  findings_count: number
  latest_report_version: number
  is_partial_sample: boolean
  total_tokens: number
  total_duration_ms: number
  mcp_calls_count: number
  backtrack_count: number
}

interface TaskDetail {
  id: string
  title: string
  country_code: string
  country_name_zh: string
  vehicle_model: string
  scenario_type: string
  scenario_label: string
  status: string
  sufficiency_score: number
  facts: Record<string, string>
  missing_facts: FollowUpQuestion[]
  findings: FindingItem[]
  step_traces: StepTraceItem[]
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
  id: string
  source_node_id: string
  target_node_id: string
  relation_type: string
  review_status: string
}

interface CitationCardPayload {
  clause_id: string
  document_id: string
  document_title: string
  country_code: string
  clause_number: string
  heading: string | null
  version_no: number
  source_url: string | null
  original_text: string
  translated_text: string
  review_status: string
  mini_graph: {
    nodes: MiniKgNode[]
    edges: MiniKgEdge[]
  }
}

const FG_CATEGORIES: FgCategory[] = [
  { type: 'COUNTRY', name: '国家/地区', color: '#1d4ed8' },
  { type: 'REGULATOR', name: '监管机构', color: '#0f766e' },
  { type: 'INSTRUMENT', name: '法规文件', color: '#7c3aed' },
  { type: 'OBLIGATION', name: '合规义务', color: '#b45309' },
  { type: 'DATA_CATEGORY', name: '数据类型', color: '#0369a1' },
  { type: 'PENALTY', name: '违规罚则', color: '#be123c' },
]

const RELATION_LABEL_MAP: Record<string, string> = {
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

interface ReportDiffPayload {
  task_id: string
  from_version: number
  to_version: number
  summary_changed: boolean
  from_summary: string
  to_summary: string
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
  completed: '已完成八维度报告',
  collecting_facts: '待补充追问事实',
  ready_to_run: '待启动分析',
  partial_sample: '部分样稿 · 待人工核查',
  running: '多智能体分析进行中',
  failed: '分析失败',
}

const loading = ref(false)
const tasks = ref<TaskSummary[]>([])
const selectedTaskId = ref<string>('')
const taskDetail = ref<TaskDetail | null>(null)

const activeSubPage = ref<'page4' | 'page5' | 'page6'>('page4')

// 追问补充表单状态
const followUpDraft = reactive<Record<string, string>>({})
const submittingFacts = ref(false)

// 页面 5：国别报告状态
const selectedReportVersionNo = ref<number>(1)
const reportSplitMode = ref<'full' | 'split'>('full')
const activeDimensionCode = ref<string>('D1')

// 在线编辑报告弹窗
const editDialogVisible = ref(false)
const editChangeNote = ref('业务合规专员在线修订 D3 与执行摘要')
const editExecSummary = ref('')
const editDimensionMap = reactive<Record<string, string>>({})
const savingReportEdit = ref(false)

// 版本差异对比弹窗
const diffDialogVisible = ref(false)
const diffFromVer = ref(1)
const diffToVer = ref(2)
const diffPayload = ref<ReportDiffPayload | null>(null)
const loadingDiff = ref(false)

// 法规引用角标抽屉（含 1 跳知识图谱复用组件）
const citationDrawerVisible = ref(false)
const loadingCitationCard = ref(false)
const citationCard = ref<CitationCardPayload | null>(null)
const activeCitationQuote = ref<string>('')

// 新建合规分析任务弹窗
const createDialogVisible = ref(false)
const creatingTask = ref(false)
const newTaskForm = reactive({
  title: '新加坡 L4 自动驾驶车队路测与云端遥测出境合规评估',
  country_code: 'SG',
  vehicle_model: 'CYS-RoboTaxi L4',
  scenario_type: 'autonomous_trial',
  preset_mode: 'complete' as 'complete' | 'incomplete',
})

const currentReport = computed<ReportVersion | null>(() => {
  const list = taskDetail.value?.report_versions || []
  if (list.length === 0) return null
  return (
    list.find((r) => r.version_no === selectedReportVersionNo.value) || list[0]
  )
})

const visibleDimensions = computed<ReportDimension[]>(() => {
  if (!currentReport.value) return []
  if (reportSplitMode.value === 'full') {
    return currentReport.value.dimensions
  }
  return currentReport.value.dimensions.filter(
    (d) => d.dimension_code === activeDimensionCode.value,
  )
})

const citationFgNodes = computed<FgNode[]>(() => {
  const mg = citationCard.value?.mini_graph
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

const citationFgEdges = computed<FgEdge[]>(() => {
  const mg = citationCard.value?.mini_graph
  if (!mg) return []
  return mg.edges.map((e) => ({
    source: e.source_node_id,
    target: e.target_node_id,
    label: RELATION_LABEL_MAP[e.relation_type] || e.relation_type,
    approved: e.review_status === 'approved',
  }))
})

async function loadTasks(selectId?: string) {
  loading.value = true
  try {
    const list = await api.get<TaskSummary[]>('/api/analysis/tasks')
    tasks.value = list
    const targetId =
      selectId ||
      selectedTaskId.value ||
      list.find((t) => t.country_code === 'SG' && t.status === 'completed')?.id ||
      list[0]?.id
    if (targetId) {
      await selectTask(targetId)
    }
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载合规分析任务列表失败')
  } finally {
    loading.value = false
  }
}

async function selectTask(taskId: string) {
  selectedTaskId.value = taskId
  try {
    const detail = await api.get<TaskDetail>(`/api/analysis/tasks/${taskId}`)
    taskDetail.value = detail
    for (const q of detail.missing_facts) {
      followUpDraft[q.fact_key] = q.current_value || ''
    }
    if (detail.report_versions.length > 0) {
      selectedReportVersionNo.value = detail.report_versions[0].version_no
    }
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载任务详情失败')
  }
}

async function submitFollowUpFacts() {
  if (!taskDetail.value) return
  submittingFacts.value = true
  try {
    const updated = await api.post<TaskDetail>(
      `/api/analysis/tasks/${taskDetail.value.id}/facts`,
      {
        facts: { ...followUpDraft },
        auto_run_if_sufficient: true,
      },
    )
    taskDetail.value = updated
    if (updated.report_versions.length > 0) {
      selectedReportVersionNo.value = updated.report_versions[0].version_no
    }
    tasks.value = await api.get<TaskSummary[]>('/api/analysis/tasks')
    ElMessage.info(
      updated.status === 'running'
        ? '事实已齐备,多智能体开始分析,可在「页面6」实时查看进度'
        : `已保存补充事实(当前完备度 ${updated.sufficiency_score}%)`,
    )
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '提交补充事实失败')
  } finally {
    submittingFacts.value = false
  }
}

async function openCitationCard(clauseId: string | null, quote = '') {
  if (!clauseId) {
    ElMessage.warning('该条目当前属于待核查状态，尚无已绑定的入库条款 ID')
    return
  }
  activeCitationQuote.value = quote
  citationDrawerVisible.value = true
  loadingCitationCard.value = true
  try {
    citationCard.value = await api.get<CitationCardPayload>(
      `/api/analysis/clauses/${clauseId}/citation-card`,
    )
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载条款原文与关联图谱失败')
  } finally {
    loadingCitationCard.value = false
  }
}

function openEditReportModal() {
  if (!currentReport.value) return
  editExecSummary.value = currentReport.value.executive_summary
  for (const d of currentReport.value.dimensions) {
    editDimensionMap[d.dimension_code] = d.content_markdown
  }
  editDialogVisible.value = true
}

async function saveEditedReport() {
  if (!taskDetail.value) return
  savingReportEdit.value = true
  try {
    const newRv = await api.post<ReportVersion>(
      `/api/analysis/tasks/${taskDetail.value.id}/reports/edit`,
      {
        change_note: editChangeNote.value,
        executive_summary: editExecSummary.value,
        dimension_updates: { ...editDimensionMap },
      },
    )
    editDialogVisible.value = false
    await selectTask(taskDetail.value.id)
    selectedReportVersionNo.value = newRv.version_no
    tasks.value = await api.get<TaskSummary[]>('/api/analysis/tasks')
    ElMessage.success(`已保存新报告版本 v${newRv.version_no}`)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '保存报告版本失败')
  } finally {
    savingReportEdit.value = false
  }
}

async function openDiffModal() {
  if (!taskDetail.value || taskDetail.value.report_versions.length < 2) {
    ElMessage.info('请先点击「在线编辑保存新版本」生成至少 2 个版本后再对比差异')
    return
  }
  const vers = taskDetail.value.report_versions.map((r) => r.version_no).sort((a, b) => a - b)
  diffFromVer.value = vers[0]
  diffToVer.value = vers[vers.length - 1]
  diffDialogVisible.value = true
  await loadVersionDiff()
}

async function loadVersionDiff() {
  if (!taskDetail.value) return
  loadingDiff.value = true
  try {
    diffPayload.value = await api.get<ReportDiffPayload>(
      `/api/analysis/tasks/${taskDetail.value.id}/reports/diff?from_version=${diffFromVer.value}&to_version=${diffToVer.value}`,
    )
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载版本对比失败')
  } finally {
    loadingDiff.value = false
  }
}

async function downloadReportFile(ext: 'docx' | 'pdf') {
  if (!taskDetail.value || !currentReport.value) return
  try {
    const ver = currentReport.value.version_no
    const res = await fetch(
      `/api/analysis/tasks/${taskDetail.value.id}/reports/${ver}/export.${ext}`,
      {
        headers: {
          Authorization: `Bearer ${api.token()}`,
        },
      },
    )
    if (!res.ok) {
      throw new Error(`导出失败 (${res.status})`)
    }
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `cyshuhe_${taskDetail.value.country_code}_8dim_report_v${ver}.${ext}`
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success(
      `已导出 ${ext.toUpperCase()} 报告文件 (v${ver} · ${(blob.size / 1024).toFixed(1)} KB)`,
    )
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '导出报告文件失败')
  }
}

async function handleCreateTask() {
  creatingTask.value = true
  try {
    const facts =
      newTaskForm.preset_mode === 'complete'
        ? {
            exterior_video_and_gnss:
              '采集 8 路环视视频与厘米级 GNSS，车端对人脸/车牌实时模糊处理后回传研发云',
            cross_border_mechanism:
              '传输至中国重庆/上海研发中心，已签署对标 PDPA 第三附表保护标准的跨境数据处理协议 (DPA) 并完成 TIA',
            consent_and_notice:
              '车机首次激活弹窗明示同意 + 车身张贴路测视觉采集告示 + 已任命新加坡本地 DPO 并公示邮箱',
            retention_and_blackbox:
              'LTA 自动驾驶黑匣子安全与接管日志在新加坡境内加密留存 3 年；研发诊断个人数据满 180 天自动销毁',
            local_admission_and_safety_operator:
              '已准备 LTA 自动驾驶路测申请包、配备经培训的随车安全员 (Safety Operator) 并投保第三方责任险',
          }
        : {
            exterior_video_and_gnss:
              '采集 8 路环视视频与厘米级 GNSS，车端对人脸/车牌实时模糊处理后回传研发云',
          }
    const created = await api.post<TaskDetail>('/api/analysis/tasks', {
      title: newTaskForm.title,
      country_code: newTaskForm.country_code,
      vehicle_model: newTaskForm.vehicle_model,
      scenario_type: newTaskForm.scenario_type,
      facts,
      auto_run_if_sufficient: true,
    })
    createDialogVisible.value = false
    await loadTasks(created.id)
    ElMessage.info(
      created.status === 'collecting_facts'
        ? '任务已创建,检测到必填事实缺失,请在追问卡片中补充'
        : '任务已创建,多智能体分析已开始,可在「页面6」实时查看进度',
    )
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '创建分析任务失败')
  } finally {
    creatingTask.value = false
  }
}

// 分析在后台运行:每 2 秒刷新一次进度,结束后自动停止
let pollTimer: number | undefined
async function pollOnce() {
  if (taskDetail.value?.status !== 'running') {
    return
  }
  try {
    const detail = await api.get<TaskDetail>(`/api/analysis/tasks/${taskDetail.value.id}`)
    taskDetail.value = detail
    tasks.value = await api.get<TaskSummary[]>('/api/analysis/tasks')
    if (detail.status !== 'running' && detail.report_versions.length > 0) {
      selectedReportVersionNo.value = detail.report_versions[0].version_no
    }
  } catch {
    /* 下一轮重试 */
  }
}

onMounted(() => {
  loadTasks()
  pollTimer = window.setInterval(pollOnce, 2000)
})

onUnmounted(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="compliance-workbench" v-loading="loading">
    <!-- 左侧：企业的合规分析任务列表 -->
    <div class="task-list-sidebar">
      <div class="sidebar-top">
        <div class="sidebar-title">多智能体合规评估任务</div>
        <el-button
          type="primary"
          size="small"
          data-testid="btn-new-analysis-task"
          @click="createDialogVisible = true"
        >
          + 新建评估
        </el-button>
      </div>

      <div class="task-cards-scroll">
        <div
          v-for="t in tasks"
          :key="t.id"
          class="task-card-item"
          :class="{ active: selectedTaskId === t.id }"
          data-testid="analysis-task-item"
          @click="selectTask(t.id)"
        >
          <div class="tc-header">
            <el-tag size="small" effect="dark" type="primary">
              {{ t.country_code }} · {{ t.country_name_zh }}
            </el-tag>
            <el-tag
              size="small"
              :type="
                t.status === 'completed'
                  ? 'success'
                  : t.status === 'collecting_facts'
                    ? 'warning'
                    : 'info'
              "
            >
              {{ STATUS_LABEL[t.status] || t.status }}
            </el-tag>
          </div>
          <div class="tc-title">{{ t.title }}</div>
          <div class="tc-meta">
            <span>车型: {{ t.vehicle_model }}</span>
            <span>完备度: {{ t.sufficiency_score }}%</span>
          </div>
          <div class="tc-stats" v-if="t.latest_report_version > 0">
            <span>报告 v{{ t.latest_report_version }}</span>
            <span>MCP调用 {{ t.mcp_calls_count }}次</span>
            <span v-if="t.backtrack_count > 0" class="backtrack-pill">
              含退回补证 {{ t.backtrack_count }}次
            </span>
          </div>
        </div>
      </div>
    </div>

    <!-- 右侧：页面 4（合规分析工作台）、页面 5（八维度国别报告）、页面 6（多智能体任务过程） -->
    <div v-if="taskDetail" class="workbench-main">
      <div class="task-banner">
        <div>
          <div class="banner-title-row">
            <h2 class="banner-title">{{ taskDetail.title }}</h2>
            <el-tag
              v-if="taskDetail.status === 'partial_sample'"
              type="warning"
              effect="dark"
            >
              部分样稿 · 待律所人工核查
            </el-tag>
            <el-tag v-else-if="taskDetail.status === 'completed'" type="success" effect="dark">
              八维度报告(引用均经程序核验)
            </el-tag>
            <el-tag
              v-else-if="taskDetail.status === 'running'"
              type="primary"
              effect="dark"
              data-testid="task-running-tag"
            >
              多智能体分析进行中…(已完成 {{ taskDetail.step_traces.length }} 步)
            </el-tag>
            <el-tag v-else-if="taskDetail.status === 'failed'" type="danger" effect="dark">
              分析失败,请查看页面6的失败原因
            </el-tag>
            <el-tag v-else type="warning" effect="dark">
              信息采集追问中 ({{ taskDetail.sufficiency_score }}%)
            </el-tag>
          </div>
          <div class="banner-sub">
            目标国家：<strong>{{ taskDetail.country_name_zh }} ({{ taskDetail.country_code }})</strong>
            ｜ 受评车型：<strong>{{ taskDetail.vehicle_model }}</strong>
            ｜ 业务场景：<strong>{{ taskDetail.scenario_label }}</strong>
          </div>
        </div>

        <el-radio-group v-model="activeSubPage" size="default" data-testid="analysis-subpage-tabs">
          <el-radio-button value="page4">
            页面4 · 合规分析工作台
          </el-radio-button>
          <el-radio-button value="page5">
            页面5 · 八维度国别报告
          </el-radio-button>
          <el-radio-button value="page6">
            页面6 · 多智能体协同过程 ({{ taskDetail.step_traces.length }})
          </el-radio-button>
        </el-radio-group>
      </div>

      <!-- ==================== 页面 4：合规分析工作台（信息采集追问 + 五列判定矩阵） ==================== -->
      <div v-if="activeSubPage === 'page4'" class="subpage-body">
        <!-- 事实完备度与追问补充面板 -->
        <div class="fact-sufficiency-card" data-testid="followup-questions-panel">
          <div class="fs-header">
            <div>
              <strong>智能体信息采集规范与主动追问机制</strong>
              <span class="fs-hint">
                （必填事实缺失时提示补充，完备度达 100% 后启动多智能体对标分析）
              </span>
            </div>
            <el-tag :type="taskDetail.sufficiency_score >= 100 ? 'success' : 'warning'">
              当前信息完备度：{{ taskDetail.sufficiency_score }}%
            </el-tag>
          </div>

          <div class="questions-grid">
            <div
              v-for="q in taskDetail.missing_facts"
              :key="q.fact_key"
              class="question-item"
              :class="{ fulfilled: q.is_fulfilled }"
            >
              <div class="q-top">
                <el-tag size="small" :type="q.is_fulfilled ? 'success' : 'warning'">
                  {{ q.dimension_code }} · {{ q.is_fulfilled ? '已采集' : '待补充追问' }}
                </el-tag>
                <span class="q-title">{{ q.question_zh }}</span>
              </div>
              <div class="q-why">法规依据原因：{{ q.why_needed }}</div>
              <div class="q-options">
                <span class="opt-label">快捷填充：</span>
                <el-tag
                  v-for="(opt, idx) in q.suggested_options"
                  :key="idx"
                  size="small"
                  effect="plain"
                  class="opt-chip"
                  @click="followUpDraft[q.fact_key] = opt"
                >
                  {{ opt }}
                </el-tag>
              </div>
              <el-input
                v-model="followUpDraft[q.fact_key]"
                size="small"
                placeholder="请填写或点击上方快捷选项补充该项业务事实"
              />
            </div>
          </div>

          <div class="fs-actions">
            <el-button
              type="primary"
              :loading="submittingFacts"
              data-testid="btn-submit-followup-facts"
              @click="submitFollowUpFacts"
            >
              {{
                taskDetail.sufficiency_score < 100
                  ? '提交补充事实并启动多智能体分析'
                  : '更新业务事实并重新运行多智能体分析'
              }}
            </el-button>
          </div>
        </div>

        <!-- 五列合规分析结果矩阵表：业务事实｜适用条款｜合规义务｜风险等级｜判断依据 -->
        <div class="findings-matrix-card">
          <div class="card-section-title">
            <span>合规分析结果矩阵（业务事实｜适用条款｜合规义务｜风险等级｜判断依据）</span>
            <el-tag size="small" type="info">
              点击任一适用条款可查看官方原文、版本与关联图谱
            </el-tag>
          </div>

          <el-empty
            v-if="taskDetail.findings.length === 0"
            description="当前任务尚未完成必填事实采集，请先在上方补充追问事实后启动分析"
          />

          <el-table
            v-else
            :data="taskDetail.findings"
            border
            stripe
            data-testid="compliance-findings-table"
          >
            <el-table-column
              prop="business_fact"
              label="业务事实"
              min-width="190"
            />
            <el-table-column label="适用条款" min-width="180">
              <template #default="{ row }">
                <el-button
                  link
                  type="primary"
                  data-testid="finding-clause-link"
                  @click="openCitationCard(row.clause_id, row.basis_quote)"
                >
                  {{ row.document_title }} · {{ row.clause_number }} (v{{
                    row.version_no
                  }})
                </el-button>
              </template>
            </el-table-column>
            <el-table-column
              prop="compliance_obligation"
              label="合规义务"
              min-width="230"
            />
            <el-table-column label="风险等级" width="120">
              <template #default="{ row }">
                <el-tag
                  :type="
                    row.risk_level === 'HIGH'
                      ? 'danger'
                      : row.risk_level === 'MEDIUM'
                        ? 'warning'
                        : 'info'
                  "
                >
                  {{ RISK_LABEL[row.risk_level] || row.risk_level }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="判断依据 (原文子串与核验状态)" min-width="280">
              <template #default="{ row }">
                <div class="basis-cell">
                  <el-tag
                    size="small"
                    :type="
                      row.verification_status === 'VERIFIED_SUBSTRING'
                        ? 'success'
                        : 'warning'
                    "
                  >
                    {{
                      row.verification_status === 'VERIFIED_SUBSTRING'
                        ? '原文子串已核验'
                        : '待核查'
                    }}
                  </el-tag>
                  <div class="basis-quote">"{{ row.basis_quote }}"</div>
                  <div class="basis-zh">【中文对照】{{ row.basis_zh }}</div>
                </div>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>

      <!-- ==================== 页面 5：八维度国别报告（整份/拆分 + 角标弹窗 + 版本对比 + Word/PDF导出） ==================== -->
      <div v-else-if="activeSubPage === 'page5'" class="subpage-body">
        <el-empty
          v-if="!currentReport"
          description="请先在页面 4 完成事实补充并生成报告"
        />

        <template v-else>
          <!-- 报告顶部控制栏 -->
          <div class="report-toolbar">
            <div class="rt-left">
              <el-radio-group
                v-model="reportSplitMode"
                size="small"
                data-testid="report-view-mode-switch"
              >
                <el-radio-button value="full">
                  整份八维度完整报告
                </el-radio-button>
                <el-radio-button value="split">
                  按维度独立拆分查看 ({{ activeDimensionCode }})
                </el-radio-button>
              </el-radio-group>

              <span class="ver-label">报告版本:</span>
              <el-select
                v-model="selectedReportVersionNo"
                size="small"
                style="width: 170px"
                data-testid="report-version-select"
              >
                <el-option
                  v-for="rv in taskDetail.report_versions"
                  :key="rv.version_no"
                  :label="`v${rv.version_no} (${rv.status === 'edited' ? '已修订' : rv.is_partial_sample ? '部分样稿' : '自动初稿'})`"
                  :value="rv.version_no"
                />
              </el-select>
            </div>

            <div class="rt-right">
              <el-button
                size="small"
                data-testid="btn-edit-report"
                @click="openEditReportModal"
              >
                在线编辑保存新版本
              </el-button>
              <el-button
                size="small"
                type="warning"
                plain
                data-testid="btn-diff-report"
                @click="openDiffModal"
              >
                版本差异对比 (Diff)
              </el-button>
              <el-button
                size="small"
                type="primary"
                data-testid="btn-export-docx"
                @click="downloadReportFile('docx')"
              >
                导出 Word (.docx)
              </el-button>
              <el-button
                size="small"
                type="success"
                data-testid="btn-export-pdf"
                @click="downloadReportFile('pdf')"
              >
                导出 PDF (.pdf)
              </el-button>
            </div>
          </div>

          <!-- 72h 法规修订联动提示条 -->
          <div
            v-for="(imp, idx) in currentReport.stale_impacts"
            :key="idx"
            class="stale-alert-banner"
            data-testid="report-stale-impact-alert"
          >
            <el-tag size="small" type="warning" effect="dark">
              72h 法规修订联动提示 · {{ imp.dimension_code }}
            </el-tag>
            <span>
              <strong>关联条款：</strong>{{ imp.clause_ref }} ｜
              <strong>变动摘要：</strong>{{ imp.amendment_summary }} ｜
              <strong>建议动作：</strong>{{ imp.action_required }}
            </span>
          </div>

          <div class="report-split-container">
            <!-- 左侧八个维度目录 -->
            <div class="dimension-nav-aside" data-testid="report-dimension-nav">
              <div class="dim-nav-header">八维度目录 (D1 - D8)</div>
              <div
                v-for="dim in currentReport.dimensions"
                :key="dim.dimension_code"
                class="dim-nav-item"
                :class="{
                  active: activeDimensionCode === dim.dimension_code,
                }"
                :data-dimension-code="dim.dimension_code"
                @click="activeDimensionCode = dim.dimension_code"
              >
                <div class="dn-top">
                  <span class="dn-code">{{ dim.dimension_code }}</span>
                  <el-tag
                    size="small"
                    :type="dim.risk_level === 'HIGH' ? 'danger' : 'warning'"
                  >
                    {{ RISK_LABEL[dim.risk_level] || dim.risk_level }}
                  </el-tag>
                </div>
                <div class="dn-title">{{ dim.dimension_title }}</div>
              </div>
            </div>

            <!-- 右侧报告正文与可点击角标 -->
            <div class="report-body-pane">
              <div class="exec-summary-box">
                <div class="es-title">
                  执行摘要 (v{{ currentReport.version_no }} · {{ currentReport.change_note }})
                </div>
                <p class="es-text">{{ currentReport.executive_summary }}</p>
              </div>

              <div
                v-for="dim in visibleDimensions"
                :key="dim.dimension_code"
                class="dimension-chapter-card"
                :data-testid="`report-chapter-${dim.dimension_code}`"
              >
                <div class="chapter-header">
                  <div>
                    <span class="chapter-code">{{ dim.dimension_code }}</span>
                    <span class="chapter-title">{{ dim.dimension_title }}</span>
                  </div>
                  <div class="chapter-badges">
                    <el-tag
                      size="small"
                      :type="dim.status === 'COMPLETE' ? 'success' : 'warning'"
                    >
                      {{ dim.status === 'COMPLETE' ? '依据已核验' : '部分样稿缺口' }}
                    </el-tag>
                    <el-tag
                      size="small"
                      :type="dim.risk_level === 'HIGH' ? 'danger' : 'warning'"
                    >
                      {{ RISK_LABEL[dim.risk_level] || dim.risk_level }}
                    </el-tag>
                  </div>
                </div>

                <el-alert
                  v-if="dim.missing_gap_note"
                  type="warning"
                  :closable="false"
                  show-icon
                  style="margin-bottom: 10px"
                  :title="dim.missing_gap_note"
                />

                <div class="chapter-markdown">{{ dim.content_markdown }}</div>

                <div
                  v-if="dim.key_recommendations.length > 0"
                  class="chapter-recs"
                >
                  <div class="rec-label">关键合规落地建议：</div>
                  <ul>
                    <li v-for="(rec, rIdx) in dim.key_recommendations" :key="rIdx">
                      {{ rec }}
                    </li>
                  </ul>
                </div>

                <!-- 法规引用角标列表：点击弹出条款原文、来源、版本与关联图谱面板 -->
                <div class="chapter-citations">
                  <span class="cite-label">本章法规引用角标（点击查看原文与关联关系图谱）：</span>
                  <el-button
                    v-for="cite in dim.citations"
                    :key="cite.citation_tag"
                    size="small"
                    type="primary"
                    plain
                    data-testid="report-citation-badge"
                    @click="openCitationCard(cite.clause_id, cite.quote_original)"
                  >
                    {{ cite.citation_tag }} {{ cite.document_title }} ·
                    {{ cite.clause_number }} (v{{ cite.version_no }})
                  </el-button>
                </div>
              </div>
            </div>
          </div>
        </template>
      </div>

      <!-- ==================== 页面 6：多智能体协同任务过程（含高亮『编制退回分析补充依据』） ==================== -->
      <div v-else-if="activeSubPage === 'page6'" class="subpage-body">
        <div class="trace-overview-bar">
          <el-tag type="primary">
            执行步骤总数: {{ taskDetail.step_traces.length }} 步
          </el-tag>
          <el-tag type="success">
            MCP 工具调用: {{ taskDetail.mcp_calls_count }} 次
          </el-tag>
          <el-tag :type="taskDetail.backtrack_count > 0 ? 'danger' : 'info'" effect="dark">
            编制/质检退回分析补充依据: {{ taskDetail.backtrack_count }} 次
          </el-tag>
          <el-tag type="info">
            累计耗时: {{ taskDetail.total_duration_ms }} ms ｜ 累计模型用量:
            {{ taskDetail.total_tokens }} tokens
          </el-tag>
        </div>

        <div class="timeline-list" data-testid="agent-process-timeline">
          <div
            v-for="step in taskDetail.step_traces"
            :key="step.id"
            class="timeline-step-card"
            :class="{ 'is-backtrack-highlight': step.is_backtrack }"
            :data-testid="
              step.is_backtrack ? 'backtrack-step-card' : 'normal-step-card'
            "
          >
            <div class="ts-head">
              <div class="ts-left">
                <span class="step-seq">#{{ step.seq_no }}</span>
                <span class="step-agent">{{ step.agent_name }}</span>
                <el-tag
                  size="small"
                  :type="step.is_backtrack ? 'danger' : 'success'"
                  effect="dark"
                >
                  {{
                    step.is_backtrack
                      ? '⚡ 编制/质检退回分析补充依据'
                      : '✓ 核验通过并交接'
                  }}
                </el-tag>
              </div>
              <div class="ts-right">
                耗时 {{ step.duration_ms }} ms · {{ step.tokens_used }} tokens
              </div>
            </div>

            <div class="ts-summary">{{ step.business_summary }}</div>

            <!-- 退回补证高亮详情框 -->
            <div v-if="step.is_backtrack" class="backtrack-callout">
              <div>
                <strong>退回具体问题：</strong>{{ step.backtrack_reason }}
              </div>
              <div style="margin-top: 4px">
                <strong>补充依据后结果：</strong>{{ step.backtrack_resolution }}
              </div>
            </div>

            <div class="ts-tools" v-if="step.tool_calls.length > 0">
              <span class="tool-caption">调用的 MCP 工具：</span>
              <el-tag
                v-for="tc in step.tool_calls"
                :key="tc.call_id"
                size="small"
                effect="plain"
                style="margin-right: 6px"
              >
                {{ tc.tool_label }} ({{ tc.tool_name }}) · {{ tc.duration_ms }}ms →
                {{ tc.result_summary }}
              </el-tag>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 点击法规角标或适用条款弹出：条款原文、来源、版本 + 1跳关联关系图谱（复用 KgForceGraph） -->
    <el-drawer
      v-model="citationDrawerVisible"
      title="法规引用角标溯源 · 原文对照与关联关系图谱"
      size="62%"
    >
      <div v-loading="loadingCitationCard" data-testid="citation-detail-drawer">
        <template v-if="citationCard">
          <div class="cite-meta-box">
            <div class="cm-title">
              {{ citationCard.document_title }} · {{ citationCard.clause_number }}
              <el-tag size="small" type="primary">
                版本 v{{ citationCard.version_no }}
              </el-tag>
              <el-tag
                size="small"
                :type="
                  citationCard.review_status === 'approved'
                    ? 'success'
                    : 'warning'
                "
              >
                {{
                  citationCard.review_status === 'approved'
                    ? '已复核法律依据'
                    : '机器翻译 · 待核对'
                }}
              </el-tag>
            </div>
            <div class="cm-url" v-if="citationCard.source_url">
              官方来源：
              <a :href="citationCard.source_url" target="_blank" rel="noopener">
                {{ citationCard.source_url }}
              </a>
            </div>
          </div>

          <div class="cite-dual-columns">
            <div class="cite-col">
              <div class="col-label">官方原文 (Original Text)</div>
              <div class="col-content">{{ citationCard.original_text }}</div>
              <div v-if="activeCitationQuote" class="quote-anchor-box">
                <strong>报告锚定原文子串：</strong>
                <mark>{{ activeCitationQuote }}</mark>
              </div>
            </div>
            <div class="cite-col">
              <div class="col-label">术语受控中文对照 (Chinese Translation)</div>
              <div class="col-content">{{ citationCard.translated_text }}</div>
            </div>
          </div>

          <div style="margin-top: 16px">
            <div style="font-weight: 700; margin-bottom: 8px">
              该条款关联关系面板（复用 ECharts 知识图谱组件）：
            </div>
            <KgForceGraph
              v-if="citationDrawerVisible && citationFgNodes.length > 0"
              :nodes="citationFgNodes"
              :edges="citationFgEdges"
              :categories="FG_CATEGORIES"
              height="360px"
            />
          </div>
        </template>
      </div>
    </el-drawer>

    <!-- 在线编辑报告章节弹窗 -->
    <el-dialog
      v-model="editDialogVisible"
      title="在线编辑报告并保存为不可变新版本"
      width="740px"
    >
      <div style="margin-bottom: 10px">
        <label style="font-weight: 600; display: block; margin-bottom: 4px">
          版本修订说明
        </label>
        <el-input v-model="editChangeNote" />
      </div>
      <div style="margin-bottom: 10px">
        <label style="font-weight: 600; display: block; margin-bottom: 4px">
          执行摘要
        </label>
        <el-input v-model="editExecSummary" type="textarea" :rows="3" />
      </div>
      <div v-if="currentReport" style="max-height: 360px; overflow-y: auto">
        <div
          v-for="dim in currentReport.dimensions"
          :key="dim.dimension_code"
          style="margin-bottom: 12px"
        >
          <label style="font-weight: 600; display: block; margin-bottom: 4px">
            {{ dim.dimension_code }} · {{ dim.dimension_title }}
          </label>
          <el-input
            v-model="editDimensionMap[dim.dimension_code]"
            type="textarea"
            :rows="4"
          />
        </div>
      </div>
      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="savingReportEdit"
          data-testid="btn-confirm-save-report-edit"
          @click="saveEditedReport"
        >
          保存为新版本
        </el-button>
      </template>
    </el-dialog>

    <!-- 版本差异对比 (Diff) 弹窗 -->
    <el-dialog
      v-model="diffDialogVisible"
      title="报告版本差异对比 (Version Diff)"
      width="780px"
    >
      <div
        style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px"
      >
        <span>基线版本: v{{ diffFromVer }}</span>
        <span>→</span>
        <span>对比版本: v{{ diffToVer }}</span>
      </div>
      <div v-loading="loadingDiff" data-testid="report-diff-content">
        <template v-if="diffPayload">
          <div
            v-for="item in diffPayload.dimension_diffs"
            :key="item.dimension_code"
            class="diff-row-card"
            :class="{ changed: item.changed }"
          >
            <div class="diff-head">
              <strong>{{ item.dimension_code }} · {{ item.dimension_title }}</strong>
              <el-tag :type="item.changed ? 'warning' : 'info'" size="small">
                {{ item.changed ? '有修订差异' : '内容一致' }}
              </el-tag>
            </div>
            <div v-if="item.changed" class="diff-cols">
              <div class="diff-old">
                <div class="diff-sub">v{{ diffPayload.from_version }} 原内容：</div>
                <pre>{{ item.from_markdown }}</pre>
              </div>
              <div class="diff-new">
                <div class="diff-sub">v{{ diffPayload.to_version }} 修订后：</div>
                <pre>{{ item.to_markdown }}</pre>
              </div>
            </div>
          </div>
        </template>
      </div>
    </el-dialog>

    <!-- 新建合规分析任务弹窗 -->
    <el-dialog
      v-model="createDialogVisible"
      title="新建多智能体合规安全评估任务"
      width="620px"
    >
      <el-form label-width="110px">
        <el-form-item label="任务名称">
          <el-input v-model="newTaskForm.title" />
        </el-form-item>
        <el-form-item label="目标出海国家">
          <el-select v-model="newTaskForm.country_code" style="width: 100%">
            <el-option label="新加坡 (SG) - 完整法源闭环" value="SG" />
            <el-option label="突尼斯 (TN) - 法语/阿语部分样稿" value="TN" />
            <el-option label="摩洛哥 (MA) - 法语部分样稿" value="MA" />
            <el-option label="埃及 (EG) - 车联网框架部分样稿" value="EG" />
            <el-option label="黎巴嫩 (LB) - 阿语部分样稿" value="LB" />
          </el-select>
        </el-form-item>
        <el-form-item label="受评车型">
          <el-input v-model="newTaskForm.vehicle_model" />
        </el-form-item>
        <el-form-item label="初始事实模式">
          <el-radio-group v-model="newTaskForm.preset_mode">
            <el-radio value="complete">
              预填完整 5 项业务事实（直接触发多智能体生成八维度报告）
            </el-radio>
            <el-radio value="incomplete">
              仅填 1 项事实（演示智能体主动追问补充机制）
            </el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="creatingTask"
          data-testid="btn-confirm-create-task"
          @click="handleCreateTask"
        >
          创建任务
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.compliance-workbench {
  display: flex;
  height: calc(100vh - 100px);
  min-height: 700px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  overflow: hidden;
}

.task-list-sidebar {
  width: 300px;
  background: #ffffff;
  border-right: 1px solid #e2e8f0;
  display: flex;
  flex-direction: column;
}

.sidebar-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 14px;
  border-bottom: 1px solid #f1f5f9;
}

.sidebar-title {
  font-weight: 700;
  font-size: 14px;
  color: #0f172a;
}

.task-cards-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 10px;
}

.task-card-item {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 10px;
  cursor: pointer;
  background: #ffffff;
  transition: all 0.15s;
}

.task-card-item:hover {
  border-color: #93c5fd;
}

.task-card-item.active {
  border-color: #2563eb;
  background: #eff6ff;
}

.tc-header {
  display: flex;
  justify-content: space-between;
  margin-bottom: 6px;
}

.tc-title {
  font-size: 13px;
  font-weight: 700;
  color: #0f172a;
  line-height: 1.4;
  margin-bottom: 6px;
}

.tc-meta,
.tc-stats {
  display: flex;
  justify-content: space-between;
  font-size: 11.5px;
  color: #64748b;
  margin-top: 4px;
}

.backtrack-pill {
  color: #c2410c;
  font-weight: 700;
}

.workbench-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
}

.task-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 18px;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
  flex-wrap: wrap;
  gap: 12px;
}

.banner-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.banner-title {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
}

.banner-sub {
  font-size: 12px;
  color: #475569;
  margin-top: 4px;
}

.subpage-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 18px;
}

.fact-sufficiency-card,
.findings-matrix-card {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 16px;
}

.fs-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.fs-hint {
  font-size: 12px;
  color: #64748b;
}

.questions-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.question-item {
  border: 1px solid #fed7aa;
  background: #fffbeb;
  border-radius: 8px;
  padding: 10px 12px;
}

.question-item.fulfilled {
  border-color: #bbf7d0;
  background: #f0fdf4;
}

.q-top {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 4px;
}

.q-title {
  font-size: 12.5px;
  font-weight: 700;
  color: #0f172a;
  line-height: 1.4;
}

.q-why {
  font-size: 11.5px;
  color: #475569;
  margin-bottom: 6px;
}

.q-options {
  margin-bottom: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}

.opt-label {
  font-size: 11px;
  color: #64748b;
}

.opt-chip {
  cursor: pointer;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}

.fs-actions {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}

.card-section-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 700;
  font-size: 14px;
  margin-bottom: 12px;
}

.basis-quote {
  font-size: 12px;
  color: #0f172a;
  font-style: italic;
  margin-top: 4px;
}

.basis-zh {
  font-size: 11.5px;
  color: #475569;
  margin-top: 2px;
}

.report-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 10px 14px;
  margin-bottom: 12px;
  flex-wrap: wrap;
  gap: 10px;
}

.rt-left,
.rt-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.ver-label {
  font-size: 12px;
  font-weight: 600;
  color: #475569;
  margin-left: 8px;
}

.stale-alert-banner {
  background: #fffbeb;
  border: 1px solid #fcd34d;
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12.5px;
  color: #92400e;
}

.report-split-container {
  display: flex;
  gap: 14px;
  align-items: flex-start;
}

.dimension-nav-aside {
  width: 240px;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 10px;
  flex-shrink: 0;
}

.dim-nav-header {
  font-weight: 700;
  font-size: 13px;
  color: #0f172a;
  padding-bottom: 8px;
  margin-bottom: 8px;
  border-bottom: 1px solid #f1f5f9;
}

.dim-nav-item {
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  margin-bottom: 6px;
  border: 1px solid transparent;
}

.dim-nav-item:hover {
  background: #f8fafc;
}

.dim-nav-item.active {
  background: #eff6ff;
  border-color: #93c5fd;
}

.dn-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.dn-code {
  font-weight: 800;
  font-size: 12px;
  color: #1d4ed8;
}

.dn-title {
  font-size: 12.5px;
  font-weight: 600;
  color: #1e293b;
  margin-top: 3px;
}

.report-body-pane {
  flex: 1;
  min-width: 0;
}

.exec-summary-box {
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 14px;
}

.es-title {
  font-weight: 700;
  font-size: 14px;
  color: #1e3a8a;
  margin-bottom: 6px;
}

.es-text {
  margin: 0;
  font-size: 13px;
  color: #1e293b;
  line-height: 1.65;
  white-space: pre-line;
}

.dimension-chapter-card {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  padding: 16px 18px;
  margin-bottom: 14px;
}

.chapter-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid #f1f5f9;
  padding-bottom: 10px;
  margin-bottom: 10px;
}

.chapter-code {
  background: #1d4ed8;
  color: #fff;
  font-weight: 700;
  font-size: 12px;
  padding: 3px 8px;
  border-radius: 4px;
  margin-right: 8px;
}

.chapter-title {
  font-size: 15px;
  font-weight: 700;
  color: #0f172a;
}

.chapter-badges {
  display: flex;
  gap: 6px;
}

.chapter-markdown {
  font-size: 13.5px;
  color: #1e293b;
  line-height: 1.75;
  white-space: pre-line;
}

.chapter-recs {
  margin-top: 12px;
  background: #f8fafc;
  border-radius: 6px;
  padding: 10px 14px;
  font-size: 12.5px;
}

.rec-label {
  font-weight: 700;
  color: #0f172a;
  margin-bottom: 4px;
}

.chapter-citations {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed #e2e8f0;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.cite-label {
  font-size: 12px;
  font-weight: 600;
  color: #475569;
}

.trace-overview-bar {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.timeline-step-card {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 12px;
}

.timeline-step-card.is-backtrack-highlight {
  border: 2px solid #f97316;
  background: #fff7ed;
}

.ts-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.ts-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.step-seq {
  font-weight: 800;
  color: #1d4ed8;
}

.step-agent {
  font-weight: 700;
  font-size: 14px;
  color: #0f172a;
}

.ts-right {
  font-size: 12px;
  color: #64748b;
}

.ts-summary {
  font-size: 13.5px;
  font-weight: 600;
  color: #1e293b;
  margin-bottom: 8px;
}

.backtrack-callout {
  background: #ffedd5;
  border-left: 4px solid #ea580c;
  padding: 10px 12px;
  border-radius: 6px;
  font-size: 12.5px;
  color: #9a3412;
  margin-bottom: 8px;
}

.tool-caption {
  font-size: 12px;
  color: #475569;
  font-weight: 600;
  margin-right: 6px;
}

.cite-meta-box {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}

.cm-title {
  font-weight: 700;
  font-size: 15px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.cm-url {
  font-size: 12px;
  margin-top: 6px;
}

.cite-dual-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.cite-col {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 12px;
}

.col-label {
  font-weight: 700;
  font-size: 12.5px;
  color: #475569;
  margin-bottom: 6px;
}

.col-content {
  font-size: 13px;
  line-height: 1.65;
  color: #0f172a;
}

.quote-anchor-box {
  margin-top: 10px;
  padding: 8px;
  background: #fef9c3;
  border-radius: 6px;
  font-size: 12px;
}

.diff-row-card {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 10px;
}

.diff-row-card.changed {
  border-color: #f59e0b;
  background: #fffbeb;
}

.diff-head {
  display: flex;
  justify-content: space-between;
  margin-bottom: 8px;
}

.diff-cols {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.diff-cols pre {
  white-space: pre-wrap;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 8px;
  font-size: 12px;
}
</style>
