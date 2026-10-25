<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../api'
import { useAuth } from '../auth'
import {
  boundedPosition,
  dragPosition,
  readPositions,
  spacedLayout,
  NODE_WIDTH,
  NODE_HEIGHT,
  type NodePosition,
} from '../components/workflowLayout'

interface McpToolSchema {
  tool_name: string
  display_name: string
  description: string
  input_schema: Record<string, unknown>
  output_schema: Record<string, unknown>
}

interface WorkflowNode {
  node_id: string
  title: string
  subtitle: string
  node_type: string
  category_label: string
  icon: string
  color: string
  position_x: number
  position_y: number
  readonly_locked: boolean
  lock_reason: string
  model_provider: string
  model_name: string
  parameters: Record<string, unknown>
  system_prompt: string
  code_entrypoint: string
  mcp_tools: McpToolSchema[]
  input_variables: Array<{ name: string; type: string; desc: string }>
  output_variables: Array<{ name: string; type: string; desc: string }>
  responsibility_desc: string
  runtime_settings: Record<string, unknown>
  configurable_fields: string[]
}

interface WorkflowEdge {
  edge_id: string
  source_node_id: string
  target_node_id: string
  edge_type: string
  label: string
  condition_desc: string
  animated: boolean
}

interface WorkflowConfig {
  id: string
  workflow_key: string
  version_no: number
  name: string
  description: string
  dsl_version: string
  is_active: boolean
  readonly_locked: boolean
  executable: boolean
  runtime_schema_version: string | null
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  change_note: string | null
  created_at: string
}

interface WorkflowVersionSummary {
  id: string
  version_no: number
  name: string
  dsl_version: string
  is_active: boolean
  readonly_locked: boolean
  executable: boolean
  nodes_count: number
  edges_count: number
  change_note: string | null
  created_at: string
}

interface TaskSummary {
  id: string
  conversation_code: string
  org_id: string | null
  org_name: string
  creator_email: string
  creator_name: string
  title: string
  country_code: string
  country_name_zh: string
  vehicle_model: string
  scenario_type: string
  scenario_label: string
  status: string
  sufficiency_score: number
  missing_facts_count: number
  messages_count: number
  steps_count: number
  current_active_node_id: string | null
  current_active_node_title: string | null
  findings_count: number
  latest_report_version: number
  is_partial_sample: boolean
  total_tokens: number
  total_duration_ms: number
  mcp_calls_count: number
  backtrack_count: number
  created_at: string
  updated_at: string
}

interface StepTrace {
  workflow_version_id: string | null
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
  input_snapshot: Record<string, unknown>
  output_snapshot: Record<string, unknown>
  tokens_used: number
  duration_ms: number
  created_at: string
}

interface ChatMessageItem {
  id: string
  seq_no: number
  role: string
  content: string
  options: string[]
  slot: string | null
  created_at: string
}

interface FollowUpQuestionItem {
  fact_key: string
  dimension_code: string
  question_zh: string
  why_needed: string
  current_value: string | null
  is_fulfilled: boolean
}

interface FindingItem {
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

interface ReportVersionItem {
  id: string
  version_no: number
  status: string
  is_partial_sample: boolean
  change_note: string
  executive_summary: string
  dimensions: Array<{
    dimension_code: string
    dimension_title: string
    status: string
    risk_level: string
    content_markdown: string
    key_recommendations: string[]
    citations: Array<{
      citation_tag: string
      document_title: string
      clause_number: string
      quote_original: string
      quote_zh: string
    }>
    missing_gap_note: string | null
  }>
}

interface TaskDetail {
  workflow_version_id: string | null
  workflow_version_no: number
  id: string
  conversation_code: string
  org_id: string | null
  org_name: string
  creator_email: string
  creator_name: string
  current_active_node_id: string | null
  current_active_node_title: string | null
  title: string
  country_code: string
  country_name_zh: string
  vehicle_model: string
  scenario_type: string
  scenario_label: string
  status: string
  sufficiency_score: number
  facts: Record<string, string>
  missing_facts: FollowUpQuestionItem[]
  findings: FindingItem[]
  step_traces: StepTrace[]
  report_versions: ReportVersionItem[]
  messages: ChatMessageItem[]
  total_tokens: number
  total_duration_ms: number
  mcp_calls_count: number
  backtrack_count: number
  created_at: string
  updated_at: string
}

interface BenchmarkSummary {
  total_cases: number
  completed_cases: number
  recall_at_5: number | null
  retrieval_hits: number
  retrieval_expected: number
  quote_fidelity: number | null
  faithful_findings: number
  findings_total: number
  fact_consistency: number | null
  supported_findings: number
  judged_findings: number
  technical_error_cases: number
  human_review_status: string
  note: string
  cases: Array<{
    question_id: string
    country_code: string
    dimension_code: string
    scenario_question: string
    retrieval_hits: number
    retrieval_expected: number
    faithful_findings: number
    findings_total: number
    judged_findings: number
    supported_findings: number
    analysis_error: string | null
    judge_error: string | null
  }>
}

const STATUS_LABEL_MAP: Record<string, string> = {
  running: '实时运行中',
  collecting_facts: '💬 对话采集中',
  ready_to_run: '⏳ 待启动分析',
  completed: '✅ 已出报告',
  partial_sample: '⚠️ 部分样稿',
  failed: '❌ 执行失败',
}

const STATUS_TAG_TYPE: Record<string, 'primary' | 'success' | 'warning' | 'danger' | 'info'> = {
  running: 'primary',
  collecting_facts: 'info',
  ready_to_run: 'info',
  completed: 'success',
  partial_sample: 'warning',
  failed: 'danger',
}

const PHASE_LABEL_MAP: Record<string, string> = {
  fact_check: '环节 1 · 八维度事实完备度核验',
  retrieval: '环节 2 · MCP 法规混合检索与现行状态核验',
  analysis: '环节 3 · 八维度合规矩阵模型分析初稿',
  verification: '环节 4 · 依据原文子串与数字程序化核验',
  re_analysis: '环节 4b · 质检退回补证重新分析',
  re_verification: '环节 4c · 补证结论二次程序化核验',
  compilation: '环节 5 · 八维度国别报告与合规矩阵编制',
  update_linkage: '环节 6 · 法规修订 72h 影响联动追踪',
  error: '异常终止',
}

const loading = ref(false)
const workflow = ref<WorkflowConfig | null>(null)
const versions = ref<WorkflowVersionSummary[]>([])
const selectedVersionNo = ref<number | undefined>(undefined)

const tasks = ref<TaskSummary[]>([])
const selectedTaskId = ref<string>('')
const selectedTaskDetail = ref<TaskDetail | null>(null)

const inspectorOpen = ref(false)
const selectedNodeId = ref<string>('node_dimension_analyst')
const inspectorTab = ref<'config' | 'tools' | 'trace'>('config')
const canvasScale = ref<number>(0.72)
const auth = useAuth()
const canvasScroll = ref<HTMLDivElement | null>(null)
const customPositions = ref<Record<string, NodePosition>>({})
const nodeHeights = ref<Record<string, number>>({})
const nodeElements = new Map<string, HTMLElement>()
let nodeObserver: ResizeObserver | undefined
const draggingNodeId = ref('')
let drag: {
  id: string
  pointerId: number
  target: HTMLElement
  startX: number
  startY: number
  scrollX: number
  scrollY: number
  origin: NodePosition
  previous?: NodePosition
  moved: boolean
} | null = null
let suppressClickUntil = 0
const layoutKey = computed(() =>
  workflow.value && auth.user
    ? `cysh:studio-layout:v1:${auth.user.id}:${workflow.value.id}:${workflow.value.version_no}`
    : '',
)
const defaultPositions = computed(() =>
  spacedLayout(workflow.value?.nodes || [], nodeHeights.value),
)
function positionOf(node: WorkflowNode): NodePosition {
  return (
    customPositions.value[node.node_id] || defaultPositions.value[node.node_id] || { x: 60, y: 60 }
  )
}
function heightOf(node: WorkflowNode) {
  return nodeHeights.value[node.node_id] || NODE_HEIGHT
}
const stageSize = computed(() => ({
  width: Math.max(
    1000,
    ...(workflow.value?.nodes || []).map((n) => positionOf(n).x + NODE_WIDTH + 80),
  ),
  height: Math.max(
    640,
    ...(workflow.value?.nodes || []).map((n) => positionOf(n).y + heightOf(n) + 80),
  ),
}))
watch(layoutKey, () => {
  cancelDrag()
  try {
    customPositions.value = readPositions(
      localStorage.getItem(layoutKey.value),
      workflow.value?.nodes || [],
    )
  } catch {
    customPositions.value = {}
  }
})
function saveLayout() {
  if (!layoutKey.value) return
  try {
    localStorage.setItem(
      layoutKey.value,
      JSON.stringify({ version: 1, positions: customPositions.value }),
    )
  } catch {
    ElMessage.warning('布局已调整，但当前浏览器无法保存，刷新后将恢复默认布局。')
  }
}
function registerNode(id: string, element: unknown) {
  if (!(element instanceof HTMLElement) || nodeElements.get(id) === element) return
  const old = nodeElements.get(id)
  if (old) nodeObserver?.unobserve(old)
  nodeElements.set(id, element)
  nodeObserver?.observe(element)
}
function startNodeDrag(event: PointerEvent, node: WorkflowNode) {
  if (event.button !== 0 || !event.isPrimary || drag) return
  const target = event.currentTarget as HTMLElement
  drag = {
    id: node.node_id,
    pointerId: event.pointerId,
    target,
    startX: event.clientX,
    startY: event.clientY,
    scrollX: canvasScroll.value?.scrollLeft || 0,
    scrollY: canvasScroll.value?.scrollTop || 0,
    origin: { ...positionOf(node) },
    previous: customPositions.value[node.node_id],
    moved: false,
  }
  target.setPointerCapture(event.pointerId)
  target.focus({ preventScroll: true })
}
function moveNodeDrag(event: PointerEvent) {
  if (!drag || drag.pointerId !== event.pointerId) return
  const dx = event.clientX - drag.startX + (canvasScroll.value?.scrollLeft || 0) - drag.scrollX
  const dy = event.clientY - drag.startY + (canvasScroll.value?.scrollTop || 0) - drag.scrollY
  if (!drag.moved && Math.hypot(dx, dy) < 4) return
  drag.moved = true
  draggingNodeId.value = drag.id
  customPositions.value = {
    ...customPositions.value,
    [drag.id]: dragPosition(drag.origin, dx, dy, canvasScale.value),
  }
}
function finishNodeDrag(event: PointerEvent) {
  if (!drag || drag.pointerId !== event.pointerId) return
  const active = drag
  drag = null
  draggingNodeId.value = ''
  if (active.target.hasPointerCapture(active.pointerId))
    active.target.releasePointerCapture(active.pointerId)
  if (active.moved) {
    suppressClickUntil = Date.now() + 350
    saveLayout()
  }
}
function cancelDrag() {
  if (!drag) return
  const active = drag
  drag = null
  draggingNodeId.value = ''
  const positions = { ...customPositions.value }
  if (active.previous) positions[active.id] = active.previous
  else delete positions[active.id]
  customPositions.value = positions
  if (active.target.hasPointerCapture(active.pointerId))
    active.target.releasePointerCapture(active.pointerId)
  if (active.moved) suppressClickUntil = Date.now() + 350
}
function clickNode(nodeId: string) {
  if (Date.now() < suppressClickUntil) return
  selectNode(nodeId)
}
function moveNodeWithKeyboard(event: KeyboardEvent, node: WorkflowNode) {
  if (event.key === 'Escape') {
    cancelDrag()
    return
  }
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    selectNode(node.node_id)
    return
  }
  const direction: Record<string, [number, number]> = {
    ArrowLeft: [-1, 0],
    ArrowRight: [1, 0],
    ArrowUp: [0, -1],
    ArrowDown: [0, 1],
  }
  const delta = direction[event.key]
  if (!delta || drag) return
  event.preventDefault()
  const position = positionOf(node)
  const step = event.shiftKey ? 32 : 8
  customPositions.value = {
    ...customPositions.value,
    [node.node_id]: boundedPosition(position.x + delta[0] * step, position.y + delta[1] * step),
  }
  saveLayout()
}
async function fitCanvas() {
  await nextTick()
  const viewport = canvasScroll.value
  if (!viewport) return
  canvasScale.value = Math.min(
    1,
    Math.max(
      0.25,
      Math.min(
        (viewport.clientWidth - 32) / stageSize.value.width,
        (viewport.clientHeight - 32) / stageSize.value.height,
      ),
    ),
  )
  viewport.scrollTo({ left: 0, top: 0 })
}
async function resetLayout() {
  cancelDrag()
  customPositions.value = {}
  saveLayout()
  await fitCanvas()
}

// 实时轮询与对话工作流历史管理状态
const autoPollEnabled = ref(true)
const autoFollowRunning = ref(true)
const conversationHistoryVisible = ref(false)
const historyFilterStatus = ref<string>('')
const historyFilterCountry = ref<string>('')
const historySearchKeyword = ref<string>('')

// 展开查看每个环节产出抽屉
const stageDeliverablesVisible = ref(false)
const expandedStageNames = ref<string[]>([])
const expandedInspectorTraceIds = ref<string[]>([])

// 导入/导出弹窗状态
const exportDialogVisible = ref(false)
const exportedDslText = ref('')
const importDialogVisible = ref(false)
const importDslText = ref('')
const importChangeNote = ref('导入维护更新的多智能体编排配置包')
const importing = ref(false)

// 真实评测结果只读查看，打开抽屉不触发模型调用
const benchmarkDrawerVisible = ref(false)
const configurationPopover = ref<{ hide: () => void } | null>(null)
const benchmarkData = ref<BenchmarkSummary | null>(null)
const loadingBenchmarks = ref(false)

const runningTasks = computed(() => tasks.value.filter((t) => t.status === 'running'))
const collectingTasks = computed(() => tasks.value.filter((t) => t.status === 'collecting_facts'))
const filteredHistoryTasks = computed(() => {
  return tasks.value.filter((t) => {
    if (historyFilterStatus.value && t.status !== historyFilterStatus.value) return false
    if (historyFilterCountry.value && t.country_code !== historyFilterCountry.value) return false
    if (historySearchKeyword.value.trim()) {
      const kw = historySearchKeyword.value.trim().toLowerCase()
      const hit =
        (t.conversation_code || '').toLowerCase().includes(kw) ||
        t.id.toLowerCase().includes(kw) ||
        (t.title || '').toLowerCase().includes(kw) ||
        (t.vehicle_model || '').toLowerCase().includes(kw) ||
        (t.org_name || '').toLowerCase().includes(kw) ||
        (t.creator_email || '').toLowerCase().includes(kw) ||
        (t.creator_name || '').toLowerCase().includes(kw)
      if (!hit) return false
    }
    return true
  })
})

const nodeMap = computed(() => {
  const m = new Map<string, WorkflowNode>()
  for (const n of workflow.value?.nodes || []) {
    m.set(n.node_id, n)
  }
  return m
})

const selectedNode = computed<WorkflowNode | null>(() => {
  if (!workflow.value) return null
  return nodeMap.value.get(selectedNodeId.value) || workflow.value.nodes[0] || null
})

const upstreamEdges = computed(() => {
  if (!workflow.value || !selectedNode.value) return []
  return workflow.value.edges.filter((e) => e.target_node_id === selectedNode.value?.node_id)
})

const downstreamEdges = computed(() => {
  if (!workflow.value || !selectedNode.value) return []
  return workflow.value.edges.filter((e) => e.source_node_id === selectedNode.value?.node_id)
})

const nodeTraceStatsMap = computed(() => {
  const map = new Map<
    string,
    {
      runs: number
      duration_ms: number
      tokens: number
      hasBacktrack: boolean
      status: string
    }
  >()
  for (const tr of versionTraces.value) {
    const prev = map.get(tr.agent_node_id) || {
      runs: 0,
      duration_ms: 0,
      tokens: 0,
      hasBacktrack: false,
      status: 'passed',
    }
    prev.runs += 1
    prev.duration_ms += tr.duration_ms
    prev.tokens += tr.tokens_used
    if (tr.is_backtrack) prev.hasBacktrack = true
    prev.status = tr.status
    map.set(tr.agent_node_id, prev)
  }
  return map
})

const versionTraces = computed(() => (selectedTaskDetail.value?.step_traces || []).filter(tr =>
  tr.workflow_version_id ? tr.workflow_version_id === workflow.value?.id : !workflow.value?.executable,
))
const isRollback = computed(() => (versions.value.find(v => v.is_active)?.version_no || 0) > (selectedVersionNo.value || 0))
async function showExecutionConfig() {
  const detail = selectedTaskDetail.value
  if (detail?.workflow_version_id) await loadStudioData(detail.workflow_version_no)
}
async function showExecutableBaseline() {
  const baseline = versions.value.find(v => v.executable)
  if (baseline) await loadStudioData(baseline.version_no)
}

const selectedNodeTraces = computed(() => {
  if (!selectedNode.value || !selectedTaskDetail.value) return []
  return versionTraces.value.filter(
    (t) => t.agent_node_id === selectedNode.value?.node_id,
  )
})

const computedSvgEdges = computed(() => {
  if (!workflow.value) return []
  return workflow.value.edges
    .map((e) => {
      const src = nodeMap.value.get(e.source_node_id)
      const dst = nodeMap.value.get(e.target_node_id)
      if (!src || !dst) return null
      const sourcePosition = positionOf(src)
      const targetPosition = positionOf(dst)

      if (e.edge_type === 'backtrack') {
        // 质检退回分析补证边：从 verifier 左侧或顶部连回 analyst 底部
        const x1 = sourcePosition.x + NODE_WIDTH / 2
        const y1 = sourcePosition.y
        const x2 = targetPosition.x + NODE_WIDTH / 2
        const y2 = targetPosition.y + heightOf(dst)
        const path = `M ${x1} ${y1} C ${x1 - 90} ${y1 - 40}, ${x2 - 90} ${y2 + 40}, ${x2} ${y2}`
        return {
          ...e,
          path,
          labelX: x1 - 95,
          labelY: (y1 + y2) / 2,
        }
      }

      const x1 = sourcePosition.x + NODE_WIDTH
      const y1 = sourcePosition.y + heightOf(src) / 2
      const x2 = targetPosition.x
      const y2 = targetPosition.y + heightOf(dst) / 2
      const dx = Math.max(Math.abs(x2 - x1) * 0.45, 50)
      const path = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`
      return {
        ...e,
        path,
        labelX: (x1 + x2) / 2,
        labelY: (y1 + y2) / 2 - 12,
      }
    })
    .filter((item): item is NonNullable<typeof item> => item !== null)
})

async function loadStudioData(versionNo?: number) {
  loading.value = true
  try {
    const query = versionNo ? `?version_no=${versionNo}` : ''
    const [wf, verList, taskList] = await Promise.all([
      api.get<WorkflowConfig>(`/api/admin/agent-studio/workflow${query}`),
      api.get<WorkflowVersionSummary[]>('/api/admin/agent-studio/workflow/versions'),
      api.get<TaskSummary[]>('/api/admin/agent-studio/tasks'),
    ])
    workflow.value = wf
    selectedVersionNo.value = wf.version_no
    versions.value = verList
    tasks.value = taskList
    if (wf.nodes.length > 0 && !wf.nodes.some((n) => n.node_id === selectedNodeId.value)) {
      selectedNodeId.value = wf.nodes[0].node_id
    }
    if (!selectedTaskId.value && taskList.length > 0) {
      const runningFirst = taskList.find((t) => t.status === 'running')
      const sgCompleted =
        runningFirst ||
        taskList.find(
          (t) =>
            t.country_code === 'SG' &&
            (t.status === 'completed' || t.status === 'partial_sample') &&
            t.steps_count > 0,
        ) ||
        taskList.find((t) => t.steps_count > 0) ||
        taskList[0]
      selectedTaskId.value = sgCompleted.id
      await loadTaskTrace(sgCompleted.id)
    }
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载多智能体编排工作台失败')
  } finally {
    loading.value = false
  }
}

async function loadTaskTrace(taskId: string) {
  if (!taskId) return
  try {
    const detail = await api.get<TaskDetail>(`/api/admin/agent-studio/tasks/${taskId}`)
    selectedTaskDetail.value = detail
    if (detail.workflow_version_id && workflow.value?.id !== detail.workflow_version_id) {
      workflow.value = await api.get<WorkflowConfig>(`/api/admin/agent-studio/workflow?version_no=${detail.workflow_version_no}`)
      selectedVersionNo.value = workflow.value.version_no
    }
    // 如果当前任务正在实时运行中，且开启了自动跟随，自动选中当前活跃节点
    if (detail.status === 'running' && detail.current_active_node_id && autoFollowRunning.value) {
      selectedNodeId.value = detail.current_active_node_id
    }
    if (expandedStageNames.value.length === 0 && detail.step_traces.length > 0) {
      expandedStageNames.value = [
        'stage-chat',
        ...detail.step_traces.map((tr) => `stage-step-${tr.seq_no}`),
      ]
    }
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载任务执行轨迹失败')
  }
}

let pollTimer: number | undefined
async function pollLiveWorkflows() {
  if (!autoPollEnabled.value) return
  try {
    const list = await api.get<TaskSummary[]>('/api/admin/agent-studio/tasks')
    tasks.value = list
    const liveTask = list.find((t) => t.status === 'running')
    if (liveTask && autoFollowRunning.value && selectedTaskId.value !== liveTask.id) {
      selectedTaskId.value = liveTask.id
      await loadTaskTrace(liveTask.id)
      return
    }
    if (selectedTaskId.value) {
      const curr = list.find((t) => t.id === selectedTaskId.value)
      if (
        curr &&
        (curr.status === 'running' ||
          curr.status === 'collecting_facts' ||
          curr.steps_count !== (selectedTaskDetail.value?.step_traces.length || 0) ||
          curr.status !== selectedTaskDetail.value?.status)
      ) {
        await loadTaskTrace(selectedTaskId.value)
      }
    }
  } catch {
    /* 静默跳过单次轮询网络抖动 */
  }
}

async function trackConversationOnCanvas(task: TaskSummary) {
  selectedTaskId.value = task.id
  await loadTaskTrace(task.id)
  if (task.current_active_node_id) {
    selectedNodeId.value = task.current_active_node_id
  }
  conversationHistoryVisible.value = false
  ElMessage.success(`已切换画布跟踪对话: ${task.conversation_code}`)
}

async function openStageDeliverables(task?: TaskSummary) {
  if (task && task.id !== selectedTaskId.value) {
    selectedTaskId.value = task.id
    await loadTaskTrace(task.id)
  }
  if (selectedTaskDetail.value) {
    expandedStageNames.value = [
      'stage-chat',
      ...selectedTaskDetail.value.step_traces.map((tr) => `stage-step-${tr.seq_no}`),
    ]
  }
  stageDeliverablesVisible.value = true
}

function expandAllStages() {
  if (!selectedTaskDetail.value) return
  expandedStageNames.value = [
    'stage-chat',
    ...selectedTaskDetail.value.step_traces.map((tr) => `stage-step-${tr.seq_no}`),
  ]
}

function collapseAllStages() {
  expandedStageNames.value = []
}

async function copyCode(code: string) {
  try {
    await navigator.clipboard.writeText(code)
    ElMessage.success(`已复制对话 ID: ${code}`)
  } catch {
    ElMessage.info(`对话 ID: ${code}`)
  }
}

async function rerunTaskWorkflow(task: TaskSummary) {
  try {
    const detail = await api.post<TaskDetail>(`/api/admin/agent-studio/tasks/${task.id}/rerun`, {})
    selectedTaskId.value = detail.id
    selectedTaskDetail.value = detail
    await pollLiveWorkflows()
    ElMessage.success(`已触发对话 ${task.conversation_code} 的多智能体工作流实时重跑`)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '触发重跑失败')
  }
}

async function deleteTaskRecord(task: TaskSummary) {
  try {
    await ElMessageBox.confirm(
      `确定删除对话工作流记录「${task.conversation_code} (${task.title})」吗？`,
      '删除对话工作流记录',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  try {
    await api.delete(`/api/admin/agent-studio/tasks/${task.id}`)
    ElMessage.success(`已删除对话记录 ${task.conversation_code}`)
    const list = await api.get<TaskSummary[]>('/api/admin/agent-studio/tasks')
    tasks.value = list
    if (selectedTaskId.value === task.id) {
      selectedTaskId.value = list[0]?.id || ''
      if (selectedTaskId.value) {
        await loadTaskTrace(selectedTaskId.value)
      } else {
        selectedTaskDetail.value = null
      }
    }
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '删除失败')
  }
}

function formatTime(iso?: string): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { hour12: false })
}

async function handleVersionChange(verNo: number) {
  await loadStudioData(verNo)
}

async function activateSelectedVersion() {
  if (!selectedVersionNo.value) return
  try {
    const wf = await api.post<WorkflowConfig>(
      `/api/admin/agent-studio/workflow/versions/${workflow.value?.id}/activate`,
      {},
    )
    workflow.value = wf
    versions.value = await api.get<WorkflowVersionSummary[]>(
      '/api/admin/agent-studio/workflow/versions',
    )
    ElMessage.success(`已切换当前激活编排版本为 v${wf.version_no}`)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '激活版本失败')
  }
}

async function openExportModal() {
  try {
    const q = selectedVersionNo.value ? `?version_no=${selectedVersionNo.value}` : ''
    const dsl = await api.get<Record<string, unknown>>(
      `/api/admin/agent-studio/workflow/export${q}`,
    )
    exportedDslText.value = JSON.stringify(dsl, null, 2)
    exportDialogVisible.value = true
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '导出配置失败')
  }
}

function downloadExportedJson() {
  const blob = new Blob([exportedDslText.value], {
    type: 'application/json;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `cyshuhe-agent-workflow-v${workflow.value?.version_no || 1}.json`
  a.click()
  URL.revokeObjectURL(url)
  ElMessage.success('已下载多智能体编排配置文件 (.json)')
}

async function copyExportedJson() {
  try {
    await navigator.clipboard.writeText(exportedDslText.value)
    ElMessage.success('已复制配置 JSON 到剪贴板')
  } catch {
    ElMessage.info('请直接全选文本框复制')
  }
}

async function openImportModal() {
  if (workflow.value) {
    const dsl = await api.get<Record<string, unknown>>(`/api/admin/agent-studio/workflow/export?version_no=${workflow.value.version_no}`)
    importDslText.value = JSON.stringify(dsl, null, 2)
  }
  importDialogVisible.value = true
}

function handleImportFileChange(ev: Event) {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => {
    importDslText.value = String(reader.result || '')
    ElMessage.success(`已读取配置文件: ${file.name}`)
  }
  reader.readAsText(file, 'utf-8')
}

async function submitImportWorkflow() {
  let parsed: Record<string, unknown>
  try {
    parsed = JSON.parse(importDslText.value)
  } catch {
    ElMessage.error('JSON 格式解析失败，请检查配置包语法')
    return
  }
  importing.value = true
  try {
    const newWf = await api.post<WorkflowConfig>('/api/admin/agent-studio/workflow/import', {
      dsl_payload: parsed,
      change_note: importChangeNote.value,
      activate_immediately: false,
    })
    importDialogVisible.value = false
    await loadStudioData(newWf.version_no)
    ElMessage.success(`配置已保存为 v${newWf.version_no}，尚未激活；确认后点击激活版本`)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '导入配置失败')
  } finally {
    importing.value = false
  }
}

async function openBenchmarksDrawer() {
  configurationPopover.value?.hide()
  benchmarkDrawerVisible.value = true
  loadingBenchmarks.value = true
  try {
    benchmarkData.value = await api.get<BenchmarkSummary>('/api/admin/agent-studio/evaluations/latest')
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载评测集结果失败')
  } finally {
    loadingBenchmarks.value = false
  }
}

function selectNode(nodeId: string) {
  inspectorOpen.value = true
  selectedNodeId.value = nodeId
}

onMounted(() => {
  nodeObserver = new ResizeObserver((entries) => {
    const heights = { ...nodeHeights.value }
    let changed = false
    for (const entry of entries) {
      const element = entry.target as HTMLElement
      const id = element.dataset.nodeId
      if (id && element.offsetHeight && heights[id] !== element.offsetHeight) {
        heights[id] = element.offsetHeight
        changed = true
      }
    }
    if (changed) nodeHeights.value = heights
  })
  for (const element of nodeElements.values()) nodeObserver.observe(element)
  loadStudioData()
  pollTimer = window.setInterval(pollLiveWorkflows, 2000)
})

onUnmounted(() => {
  cancelDrag()
  nodeObserver?.disconnect()
  nodeElements.clear()
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="agent-studio-page" v-loading="loading">
    <!-- 顶部 Dify 风格工作流导航与锁定状态栏 -->
    <div class="studio-topbar" data-testid="agent-studio-toolbar">
      <div class="topbar-left">
        <div class="studio-badge">工作流</div>
        <div class="workflow-title-block">
          <h2 class="workflow-title">智能体工作台</h2>
          <div class="workflow-meta">
            <el-tag type="info" effect="plain" size="small" data-testid="agent-readonly-lock-badge">
              配置只读 · 布局可拖动
            </el-tag>
            <el-tag type="info" size="small"> DSL: {{ workflow?.dsl_version }} </el-tag>
          </div>
        </div>
      </div>

      <div class="topbar-actions">
        <div class="version-selector">
          <span class="ctrl-label">配置版本:</span>
          <el-select
            v-model="selectedVersionNo"
            aria-label="配置版本"
            size="small"
            style="width: 155px"
            @change="handleVersionChange"
          >
            <el-option
              v-for="v in versions"
              :key="v.version_no"
              :label="`v${v.version_no} ${v.is_active ? '(当前激活)' : ''}`"
              :value="v.version_no"
            />
          </el-select>
          <el-button
            v-if="workflow?.executable && !workflow.is_active && auth.user?.role === 'platform_admin'"
            size="small"
            type="warning"
            plain
            data-testid="btn-activate-workflow"
            @click="activateSelectedVersion"
          >
            {{ isRollback ? '回滚到此版本' : '激活此版本' }}
          </el-button>
        </div>

        <div class="task-selector">
          <span class="ctrl-label">当前跟踪对话:</span>
          <el-select
            v-model="selectedTaskId"
            aria-label="当前跟踪对话"
            size="small"
            style="width: 310px"
            data-testid="agent-trace-task-select"
            @change="loadTaskTrace"
          >
            <el-option
              v-for="t in tasks"
              :key="t.id"
              :label="`${t.conversation_code} · [${t.country_code}] ${t.title} (${STATUS_LABEL_MAP[t.status] || t.status})`"
              :value="t.id"
            />
          </el-select>
        </div>

        <el-button
          size="small"
          type="primary"
          data-testid="btn-open-conversation-history"
          @click="conversationHistoryVisible = true"
        >
          任务历史 ({{ tasks.length }})
        </el-button>
        <el-button
          size="small"
          type="success"
          data-testid="btn-expand-stage-outputs"
          :disabled="!selectedTaskDetail"
          @click="openStageDeliverables()"
        >
          查看环节产出
        </el-button>
        <el-button size="small" @click="inspectorOpen = !inspectorOpen">{{
          inspectorOpen ? '收起节点详情' : '节点详情'
        }}</el-button
        ><el-popover ref="configurationPopover" trigger="click" placement="bottom-end" :width="280"
          ><template #reference><el-button size="small">配置与评测</el-button></template>
          <div class="secondary-actions">
            <el-button
              size="small"
              type="primary"
              plain
              data-testid="btn-export-workflow"
              @click="openExportModal"
            >
              导出工作流配置
            </el-button>
            <el-button
              size="small"
              type="success"
              plain
              v-if="auth.user?.role === 'platform_admin'"
              data-testid="btn-import-workflow"
              @click="openImportModal"
            >
              导入配置
            </el-button>
            <el-button
              size="small"
              type="warning"
              data-testid="btn-open-benchmarks"
              @click="openBenchmarksDrawer"
            >
              质量评测
            </el-button>
          </div></el-popover
        >
      </div>
    </div>

    <!-- 实时运行监控与当前对话工作流状态栏 -->
    <div class="live-monitor-bar" data-testid="live-monitor-bar">
      <div class="live-monitor-left">
        <span class="live-dot" :class="{ active: runningTasks.length > 0 }" />
        <el-tag
          size="small"
          :type="runningTasks.length > 0 ? 'primary' : 'success'"
          effect="dark"
          data-testid="live-running-count-badge"
        >
          {{
            runningTasks.length > 0
              ? `实时运行中工作流: ${runningTasks.length} 个`
              : '实时监控就绪 (0 个运行中)'
          }}
        </el-tag>
        <el-tag size="small" type="info"> 对话采集中: {{ collectingTasks.length }} 个 </el-tag>
        <el-tag size="small" type="info"> 累计对话工作流: {{ tasks.length }} 条 </el-tag>

        <template v-if="runningTasks.length > 0">
          <div class="live-running-pills">
            <el-button
              v-for="rt in runningTasks"
              :key="rt.id"
              size="small"
              type="primary"
              plain
              data-testid="live-running-task-pill"
              @click="trackConversationOnCanvas(rt)"
            >
              {{ rt.conversation_code }} ·
              {{ rt.current_active_node_title || '执行中' }}
            </el-button>
          </div>
        </template>
      </div>

      <div class="live-monitor-right">
        <template v-if="selectedTaskDetail">
          <el-tag
            size="small"
            effect="plain"
            type="primary"
            data-testid="admin-current-conversation-code"
          >
            当前对话 ID: {{ selectedTaskDetail.conversation_code }}
          </el-tag>
          <el-tag size="small" :type="STATUS_TAG_TYPE[selectedTaskDetail.status] || 'info'">
            {{ STATUS_LABEL_MAP[selectedTaskDetail.status] || selectedTaskDetail.status }}
          </el-tag>
          <el-tag
            v-if="selectedTaskDetail.current_active_node_title"
            size="small"
            type="warning"
            effect="dark"
            data-testid="canvas-active-node-banner"
          >
            当前所在环节: {{ selectedTaskDetail.current_active_node_title }}
          </el-tag>
        </template>
        <el-switch
          aria-label="自动同步"
          v-model="autoPollEnabled"
          size="small"
          active-text="自动同步"
        />
        <el-button size="small" @click="pollLiveWorkflows">刷新</el-button>
      </div>
    </div>

    <el-alert v-if="workflow && !workflow.executable" type="warning" :closable="false" show-icon>
      <template #title>历史协议版本仅供查看，与当前执行引擎不兼容；历史参数和运行轨迹仍保留。</template>
      <el-button size="small" @click="showExecutableBaseline">查看可执行基线</el-button>
    </el-alert>
    <el-alert v-if="selectedTaskDetail" type="info" :closable="false" data-testid="workflow-execution-version">
      <template #title>
        <template v-if="selectedTaskDetail.workflow_version_id">
          本次任务执行版本 v{{ selectedTaskDetail.workflow_version_no }} · {{ selectedTaskDetail.workflow_version_id }}
          <template v-if="workflow?.id !== selectedTaskDetail.workflow_version_id">（当前画布查看其他版本）</template>
        </template>
        <template v-else>历史任务未记录实际执行配置，不能将当前画布视为其执行版本。</template>
      </template>
      <el-button v-if="selectedTaskDetail.workflow_version_id && workflow?.id !== selectedTaskDetail.workflow_version_id"
        size="small" @click="showExecutionConfig">查看任务执行配置</el-button>
    </el-alert>
    <!-- 主体：左侧 Dify 风格节点连线画布 + 右侧只读详情与轨迹调试栏 -->
    <div class="studio-workspace">
      <!-- 左侧画布 -->
      <div class="canvas-wrapper" data-testid="dify-workflow-canvas">
        <div class="canvas-floating-bar">
          <el-tag size="small" effect="plain">
            节点数: {{ workflow?.nodes.length || 0 }} 个智能体
          </el-tag>
          <el-tag size="small" effect="plain" type="success">
            连接边: {{ workflow?.edges.length || 0 }} 条（含 1 条退回补证回流边）
          </el-tag>
          <el-popover v-if="selectedTaskDetail" trigger="click" placement="bottom" :width="320"
            ><template #reference><el-button size="small" text>任务统计</el-button></template>
            <div class="task-metrics-list">
              <el-tag size="small" type="primary">
                对话问答: {{ selectedTaskDetail.messages.length }} 条
              </el-tag>
              <el-tag size="small" type="primary">
                累计 MCP 工具调用: {{ selectedTaskDetail.mcp_calls_count }} 次
              </el-tag>
              <el-tag size="small" type="warning">
                质检退回补证: {{ selectedTaskDetail.backtrack_count }} 次
              </el-tag>
              <el-tag size="small" type="info">
                总耗时: {{ selectedTaskDetail.total_duration_ms }} ms | Token:
                {{ selectedTaskDetail.total_tokens }}
              </el-tag>
            </div></el-popover
          >
          <div class="zoom-controls">
            <el-button size="small" @click="resetLayout" data-testid="btn-reset-layout"
              >恢复布局</el-button
            >
            <el-button size="small" @click="fitCanvas" data-testid="btn-fit-canvas"
              >适应画布</el-button
            >
            <el-button
              size="small"
              circle
              aria-label="缩小画布"
              @click="canvasScale = Math.max(0.25, +(canvasScale - 0.1).toFixed(2))"
            >
              -
            </el-button>
            <span class="zoom-text">{{ Math.round(canvasScale * 100) }}%</span>
            <el-button
              size="small"
              circle
              aria-label="放大画布"
              @click="canvasScale = Math.min(1.25, +(canvasScale + 0.1).toFixed(2))"
            >
              +
            </el-button>
          </div>
        </div>

        <div class="layout-hint" id="workflow-layout-hint">
          拖动卡片自由排布 · 方向键微调，Shift 加快 · 布局仅保存在当前浏览器
        </div>
        <div class="canvas-scroll-area" ref="canvasScroll">
          <div
            class="canvas-extent"
            :style="{
              width: `${stageSize.width * canvasScale}px`,
              height: `${stageSize.height * canvasScale}px`,
            }"
          >
            <div
              class="canvas-stage"
              :style="{
                width: `${stageSize.width}px`,
                height: `${stageSize.height}px`,
                transform: `scale(${canvasScale})`,
                transformOrigin: '0 0',
              }"
            >
              <!-- SVG 连线层 -->
              <svg class="edges-svg" :width="stageSize.width" :height="stageSize.height">
                <defs>
                  <marker
                    id="arrow-forward"
                    viewBox="0 0 10 10"
                    refX="8"
                    refY="5"
                    markerWidth="6"
                    markerHeight="6"
                    orient="auto-start-reverse"
                  >
                    <path d="M 0 1 L 10 5 L 0 9 z" fill="#3b82f6" />
                  </marker>
                  <marker
                    id="arrow-backtrack"
                    viewBox="0 0 10 10"
                    refX="8"
                    refY="5"
                    markerWidth="6"
                    markerHeight="6"
                    orient="auto-start-reverse"
                  >
                    <path d="M 0 1 L 10 5 L 0 9 z" fill="#ea580c" />
                  </marker>
                  <marker
                    id="arrow-event"
                    viewBox="0 0 10 10"
                    refX="8"
                    refY="5"
                    markerWidth="6"
                    markerHeight="6"
                    orient="auto-start-reverse"
                  >
                    <path d="M 0 1 L 10 5 L 0 9 z" fill="#ca8a04" />
                  </marker>
                </defs>

                <g v-for="edge in computedSvgEdges" :key="edge.edge_id">
                  <path
                    :d="edge.path"
                    fill="none"
                    :stroke="
                      edge.edge_type === 'backtrack'
                        ? '#ea580c'
                        : edge.edge_type === 'event_trigger'
                          ? '#ca8a04'
                          : '#3b82f6'
                    "
                    :stroke-width="edge.edge_type === 'backtrack' ? 2.6 : 2.2"
                    :stroke-dasharray="edge.edge_type === 'backtrack' ? '6,4' : undefined"
                    :marker-end="
                      edge.edge_type === 'backtrack'
                        ? 'url(#arrow-backtrack)'
                        : edge.edge_type === 'event_trigger'
                          ? 'url(#arrow-event)'
                          : 'url(#arrow-forward)'
                    "
                  />
                  <g :transform="`translate(${edge.labelX}, ${edge.labelY})`">
                    <rect
                      x="-78"
                      y="-11"
                      width="156"
                      height="20"
                      rx="10"
                      :fill="edge.edge_type === 'backtrack' ? '#fff7ed' : '#f8fafc'"
                      :stroke="edge.edge_type === 'backtrack' ? '#fdba74' : '#cbd5e1'"
                    />
                    <text
                      text-anchor="middle"
                      dominant-baseline="middle"
                      font-size="11"
                      :fill="edge.edge_type === 'backtrack' ? '#c2410c' : '#334155'"
                      font-weight="600"
                    >
                      {{ edge.label }}
                    </text>
                  </g>
                </g>
              </svg>

              <!-- Dify 风格节点卡片层 -->
              <div
                v-for="node in workflow?.nodes || []"
                :key="node.node_id"
                :ref="(el) => registerNode(node.node_id, el)"
                class="dify-node-card"
                :class="{
                  active: selectedNode?.node_id === node.node_id,
                  'is-dragging': draggingNodeId === node.node_id,
                  'has-backtrack': nodeTraceStatsMap.get(node.node_id)?.hasBacktrack,
                  'is-live-running':
                    selectedTaskDetail?.workflow_version_id === workflow?.id &&
                    selectedTaskDetail?.status === 'running' &&
                    selectedTaskDetail?.current_active_node_id === node.node_id,
                  'is-collecting':
                    selectedTaskDetail?.status === 'collecting_facts' &&
                    selectedTaskDetail?.current_active_node_id === node.node_id,
                }"
                :style="{
                  left: `${positionOf(node).x}px`,
                  top: `${positionOf(node).y}px`,
                  width: `${NODE_WIDTH}px`,
                  minHeight: `${NODE_HEIGHT}px`,
                }"
                data-testid="dify-node-card"
                :data-node-id="node.node_id"
                role="button"
                tabindex="0"
                :aria-label="`${node.title}，可拖动或使用方向键调整位置，按回车查看详情`"
                aria-describedby="workflow-layout-hint"
                @pointerdown="startNodeDrag($event, node)"
                @pointermove="moveNodeDrag"
                @pointerup="finishNodeDrag"
                @pointercancel="cancelDrag"
                @lostpointercapture="finishNodeDrag"
                @keydown="moveNodeWithKeyboard($event, node)"
                @click="clickNode(node.node_id)"
              >
                <!-- 左侧输入端口圆点 -->
                <span
                  v-if="node.node_type !== 'trigger'"
                  class="port-dot port-in"
                  :style="{ borderColor: node.color }"
                />
                <!-- 右侧输出端口圆点 -->
                <span class="port-dot port-out" :style="{ borderColor: node.color }" />

                <div class="node-card-header" :style="{ borderTop: `3px solid ${node.color}` }">
                  <span
                    class="node-type-pill"
                    :style="{
                      backgroundColor: `${node.color}18`,
                      color: node.color,
                    }"
                  >
                    {{ node.category_label }}
                  </span>
                  <span class="node-lock-tag" title="生产锁定只读">🔒 只读</span>
                </div>

                <div class="node-card-title">{{ node.title }}</div>
                <div class="node-card-subtitle">{{ node.subtitle }}</div>

                <div class="node-card-meta">
                  <div class="meta-row">
                    <span class="meta-key">模型:</span>
                    <span class="meta-val">{{ node.model_name }}</span>
                  </div>
                  <div class="meta-row">
                    <span class="meta-key">MCP工具:</span>
                    <span class="meta-val">{{ node.mcp_tools.length }} 个绑定工具</span>
                  </div>
                </div>

                <!-- 实时运行中指示条 -->
                <div
                  v-if="
                    selectedTaskDetail?.workflow_version_id === workflow?.id &&
                    selectedTaskDetail?.status === 'running' &&
                    selectedTaskDetail?.current_active_node_id === node.node_id
                  "
                  class="node-live-running-banner"
                  data-testid="node-live-running-badge"
                >
                  实时运行中...
                </div>
                <div
                  v-else-if="
                    selectedTaskDetail?.status === 'collecting_facts' &&
                    selectedTaskDetail?.current_active_node_id === node.node_id
                  "
                  class="node-collecting-banner"
                >
                  💬 对话采集中 ({{ selectedTaskDetail.sufficiency_score }}%)
                </div>

                <!-- 叠加任务运行耗时与退回状态条 -->
                <div v-if="nodeTraceStatsMap.get(node.node_id)" class="node-trace-footer">
                  <el-tag
                    size="small"
                    :type="
                      nodeTraceStatsMap.get(node.node_id)?.status === 'skipped' ? 'info'
                      : nodeTraceStatsMap.get(node.node_id)?.status === 'failed' ? 'danger'
                      : nodeTraceStatsMap.get(node.node_id)?.hasBacktrack ? 'warning' : 'success'
                    "
                  >
                    {{
                      nodeTraceStatsMap.get(node.node_id)?.hasBacktrack
                        ? `执行 ${nodeTraceStatsMap.get(node.node_id)?.runs} 次 (含补证)`
                        : nodeTraceStatsMap.get(node.node_id)?.status === 'skipped' ? '按配置跳过'
                        : nodeTraceStatsMap.get(node.node_id)?.status === 'failed' ? '执行失败'
                        : '执行通过'
                    }}
                  </el-tag>
                  <span class="trace-ms">
                    {{ nodeTraceStatsMap.get(node.node_id)?.duration_ms }}ms ·
                    {{ nodeTraceStatsMap.get(node.node_id)?.tokens }} tok
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 右侧 Dify 风格节点检查器与运行调试栏（只读） -->
      <div
        v-if="selectedNode && inspectorOpen"
        class="inspector-sidebar"
        data-testid="dify-inspector-panel"
      >
        <div class="inspector-header">
          <div class="inspector-title-row">
            <span class="inspector-dot" :style="{ backgroundColor: selectedNode.color }" />
            <span class="inspector-node-title" data-testid="inspector-node-title">
              {{ selectedNode.title }}
            </span>
            <el-tag size="small" type="danger" effect="plain"> 🔒 参数与代码只读 </el-tag>
          </div>
          <div class="inspector-node-sub">
            节点 ID: <code>{{ selectedNode.node_id }}</code> ·
            {{ selectedNode.category_label }}
          </div>
        </div>

        <el-alert type="warning" :closable="false" show-icon class="readonly-banner">
          <template #title>
            生产保护模式：当前仅可查看工作流连接关系、参数配置与运行轨迹，禁止直接修改模型参数或节点代码。
          </template>
        </el-alert>

        <el-tabs v-model="inspectorTab" class="inspector-tabs">
          <!-- Tab 1: 节点详情、连接关系、只读参数与代码 -->
          <el-tab-pane label="配置与连接详情" name="config">
            <div class="inspector-section">
              <div class="sec-title">职责说明</div>
              <p class="sec-desc">{{ selectedNode.responsibility_desc }}</p>
            </div>

            <div class="inspector-section">
              <div class="sec-title">上下游连接关系 (Topology Links)</div>
              <div class="link-group">
                <div class="link-sub">上游流入节点 ({{ upstreamEdges.length }})：</div>
                <div v-if="upstreamEdges.length === 0" class="empty-link">
                  无（当前为工作流起始触发节点）
                </div>
                <div
                  v-for="e in upstreamEdges"
                  :key="e.edge_id"
                  class="link-item"
                  @click="selectNode(e.source_node_id)"
                >
                  <el-tag size="small" :type="e.edge_type === 'backtrack' ? 'warning' : 'info'">
                    {{ e.label }}
                  </el-tag>
                  <span class="link-node-name"> ← {{ nodeMap.get(e.source_node_id)?.title }} </span>
                  <div class="link-cond">{{ e.condition_desc }}</div>
                </div>
              </div>

              <div class="link-group" style="margin-top: 8px">
                <div class="link-sub">下游流出节点 ({{ downstreamEdges.length }})：</div>
                <div v-if="downstreamEdges.length === 0" class="empty-link">
                  无（当前为终端监听节点）
                </div>
                <div
                  v-for="e in downstreamEdges"
                  :key="e.edge_id"
                  class="link-item"
                  @click="selectNode(e.target_node_id)"
                >
                  <el-tag size="small" :type="e.edge_type === 'backtrack' ? 'danger' : 'success'">
                    {{ e.label }}
                  </el-tag>
                  <span class="link-node-name"> → {{ nodeMap.get(e.target_node_id)?.title }} </span>
                  <div class="link-cond">{{ e.condition_desc }}</div>
                </div>
              </div>
            </div>

            <div class="inspector-section">
              <div class="sec-title">
                模型与运行参数
                <el-tag size="small" type="info">禁用调参</el-tag>
              </div>
              <div class="readonly-form-grid">
                <div class="form-item">
                  <label>模型供应方</label>
                  <el-input :model-value="selectedNode.model_provider" disabled size="small" />
                </div>
                <div class="form-item">
                  <label>绑定模型标识</label>
                  <el-input :model-value="selectedNode.model_name" disabled size="small" />
                </div>
                <div
                  v-for="(val, key) in selectedNode.parameters"
                  :key="String(key)"
                  class="form-item"
                >
                  <label>{{ key }}</label>
                  <el-input
                    :model-value="String(val)"
                    disabled
                    size="small"
                    data-testid="disabled-param-input"
                  />
                </div>
              </div>
            </div>

            <div class="inspector-section">
              <div class="sec-title">
                {{ !workflow?.executable ? '历史配置说明（不证明实际执行）' : selectedNode.node_type === 'analyst' || selectedNode.runtime_settings?.semantic_planning === true ? '实际模型 System Prompt' : '执行说明（不发送给模型）' }}
                <el-tag size="small" type="info">只读锁定</el-tag>
              </div>
              <el-input
                type="textarea"
                :rows="6"
                :model-value="selectedNode.system_prompt"
                readonly
                disabled
                data-testid="readonly-system-prompt"
              />
            </div>

            <div v-if="workflow?.executable" class="inspector-section">
              <div class="sec-title">此版本实际执行参数（只读）</div>
              <pre class="runtime-settings" data-testid="workflow-runtime-settings">{{ JSON.stringify(Object.fromEntries(Object.entries(selectedNode.runtime_settings).filter(([key]) => key !== 'system_prompt')), null, 2) }}</pre>
              <p>可通过配置包修改：{{ selectedNode.configurable_fields.join('、') || '无可变参数' }}</p>
            </div>
            <div class="inspector-section">
              <div class="sec-title">
                节点执行入口与代码绑定
                <el-tag size="small" type="danger">禁止修改代码</el-tag>
              </div>
              <div class="code-readonly-box" data-testid="readonly-code-entrypoint">
                <code>{{ selectedNode.code_entrypoint }}</code>
              </div>
            </div>

            <div class="inspector-section">
              <div class="sec-title">输入 / 输出变量契约 (I/O Schema)</div>
              <div class="io-title">输入变量 (Inputs)</div>
              <el-table
                :data="selectedNode.input_variables"
                size="small"
                border
                style="margin-bottom: 10px"
              >
                <el-table-column prop="name" label="变量名" width="130" />
                <el-table-column prop="type" label="类型" width="75" />
                <el-table-column prop="desc" label="说明" />
              </el-table>
              <div class="io-title">输出变量 (Outputs)</div>
              <el-table :data="selectedNode.output_variables" size="small" border>
                <el-table-column prop="name" label="变量名" width="130" />
                <el-table-column prop="type" label="类型" width="75" />
                <el-table-column prop="desc" label="说明" />
              </el-table>
            </div>
          </el-tab-pane>

          <!-- Tab 2: 绑定 MCP 工具清单 -->
          <el-tab-pane :label="`MCP 工具 (${selectedNode.mcp_tools.length})`" name="tools">
            <div v-if="selectedNode.mcp_tools.length === 0" class="empty-state-box">
              该节点不直接调用外部 MCP 工具。
            </div>
            <div v-for="tool in selectedNode.mcp_tools" :key="tool.tool_name" class="mcp-tool-card">
              <div class="tool-header">
                <span class="tool-title">{{ tool.display_name }}</span>
                <el-tag size="small" type="primary">{{ tool.tool_name }}</el-tag>
              </div>
              <p class="tool-desc">{{ tool.description }}</p>
              <div class="tool-schema-row">
                <div>
                  <strong>入参 Schema:</strong>
                  <code>{{ JSON.stringify(tool.input_schema) }}</code>
                </div>
                <div>
                  <strong>出参 Schema:</strong>
                  <code>{{ JSON.stringify(tool.output_schema) }}</code>
                </div>
              </div>
            </div>
          </el-tab-pane>

          <!-- Tab 3: 最近任务运行轨迹观测与本环节产出展开 -->
          <el-tab-pane :label="`运行轨迹 (${selectedNodeTraces.length})`" name="trace">
            <div v-if="selectedTaskDetail" class="inspector-trace-top">
              <div>
                <div style="font-size: 13px; font-weight: 700; color: #0f172a">
                  对话 ID: {{ selectedTaskDetail.conversation_code }}
                </div>
                <div style="font-size: 13px; color: #64748b">
                  {{ selectedTaskDetail.title }} ·
                  {{ STATUS_LABEL_MAP[selectedTaskDetail.status] || selectedTaskDetail.status }}
                </div>
              </div>
              <el-button
                size="small"
                type="success"
                plain
                data-testid="btn-inspector-expand-all-stages"
                @click="openStageDeliverables()"
              >
                展开全流程产出
              </el-button>
            </div>

            <div v-if="selectedNodeTraces.length === 0" class="empty-state-box">
              <template
                v-if="
                  selectedTaskDetail?.workflow_version_id === workflow?.id &&
                    selectedTaskDetail?.status === 'running' &&
                  selectedTaskDetail?.current_active_node_id === selectedNode.node_id
                "
              >
                当前节点正在实时执行中，完成后将立即写入本环节产出与工具调用明细...
              </template>
              <template v-else>
                当前选中任务在该节点无执行记录（或任务处于待补充事实阶段）。
              </template>
            </div>
            <div
              v-for="tr in selectedNodeTraces"
              :key="tr.id"
              class="trace-step-card"
              :class="{ 'trace-backtrack': tr.is_backtrack }"
              data-testid="inspector-trace-step-card"
            >
              <div class="trace-step-head">
                <el-tag size="small" :type="tr.is_backtrack ? 'danger' : 'success'">
                  步骤 #{{ tr.seq_no }} ·
                  {{ PHASE_LABEL_MAP[tr.phase] || tr.phase }}
                  · {{ tr.workflow_version_id ? '执行配置 ' + tr.workflow_version_id.slice(0, 8) : '历史配置未记录' }}
                </el-tag>
                <span class="trace-metrics">
                  耗时 {{ tr.duration_ms }} ms · {{ tr.tokens_used }} tokens
                </span>
              </div>
              <div class="trace-summary">{{ tr.business_summary }}</div>

              <div v-if="tr.is_backtrack" class="backtrack-detail-box">
                <div><strong>退回原因：</strong>{{ tr.backtrack_reason }}</div>
                <div style="margin-top: 4px">
                  <strong>补证结果：</strong>{{ tr.backtrack_resolution }}
                </div>
              </div>

              <div v-if="tr.tool_calls.length > 0" class="trace-tools-list">
                <div class="sub-label">调用的 MCP 工具明细：</div>
                <div v-for="tc in tr.tool_calls" :key="tc.call_id" class="trace-tool-item">
                  <div class="tc-top">
                    <el-tag size="small" effect="plain">
                      {{ tc.tool_name }}
                    </el-tag>
                    <span>{{ tc.duration_ms }} ms</span>
                  </div>
                  <div class="tc-args">
                    参数: <code>{{ JSON.stringify(tc.arguments) }}</code>
                  </div>
                  <div class="tc-res">结果: {{ tc.result_summary }}</div>
                </div>
              </div>

              <el-collapse v-model="expandedInspectorTraceIds" style="margin-top: 8px">
                <el-collapse-item
                  :name="tr.id"
                  title="展开查看本环节详细产出快照 (Input & Output)"
                  data-testid="inspector-step-snapshot-collapse"
                >
                  <div class="snapshot-block">
                    <div>
                      <strong>输入快照:</strong>
                      <pre>{{ JSON.stringify(tr.input_snapshot, null, 2) }}</pre>
                    </div>
                    <div>
                      <strong>输出快照:</strong>
                      <pre>{{ JSON.stringify(tr.output_snapshot, null, 2) }}</pre>
                    </div>
                  </div>
                </el-collapse-item>
              </el-collapse>
            </div>
          </el-tab-pane>
        </el-tabs>
      </div>
    </div>

    <!-- 对话产生工作流历史与实时运行监控管理抽屉 -->
    <el-drawer
      v-model="conversationHistoryVisible"
      title="对话工作流历史管理与实时运行监控"
      size="78%"
    >
      <div data-testid="conversation-history-drawer">
        <div class="history-filter-bar">
          <el-input
            v-model="historySearchKeyword"
            size="small"
            clearable
            placeholder="搜索对话 ID (如 CONV-...) / 标题 / 车型 / 企业 / 发起人邮箱"
            style="width: 320px"
            data-testid="conv-history-search"
          />
          <el-select
            v-model="historyFilterStatus"
            size="small"
            clearable
            placeholder="按工作流状态筛选"
            style="width: 170px"
            data-testid="conv-history-status-select"
          >
            <el-option label="全部状态" value="" />
            <el-option label="实时运行中" value="running" />
            <el-option label="💬 对话采集中" value="collecting_facts" />
            <el-option label="✅ 已出报告" value="completed" />
            <el-option label="⚠️ 部分样稿" value="partial_sample" />
            <el-option label="❌ 执行失败" value="failed" />
          </el-select>
          <el-select
            v-model="historyFilterCountry"
            size="small"
            clearable
            placeholder="按国家筛选"
            style="width: 140px"
          >
            <el-option label="全部国家" value="" />
            <el-option label="新加坡 (SG)" value="SG" />
            <el-option label="埃及 (EG)" value="EG" />
            <el-option label="突尼斯 (TN)" value="TN" />
            <el-option label="摩洛哥 (MA)" value="MA" />
            <el-option label="黎巴嫩 (LB)" value="LB" />
          </el-select>
          <el-checkbox v-model="autoFollowRunning" size="small">
            新任务启动时自动跟随画布
          </el-checkbox>
          <el-button size="small" type="primary" plain @click="pollLiveWorkflows">
            立即同步
          </el-button>
        </div>

        <el-table
          :data="filteredHistoryTasks"
          border
          stripe
          size="small"
          data-testid="conv-history-table"
        >
          <el-table-column label="对话 ID" width="210">
            <template #default="{ row }">
              <div class="conv-code-cell">
                <code data-testid="history-row-conv-code">{{ row.conversation_code }}</code>
                <el-button
                  link
                  type="primary"
                  size="small"
                  @click="copyCode(row.conversation_code)"
                >
                  复制
                </el-button>
              </div>
              <div class="cell-muted">UUID: {{ row.id.slice(0, 8) }}…</div>
            </template>
          </el-table-column>

          <el-table-column label="所属企业 / 发起用户" width="180">
            <template #default="{ row }">
              <div style="font-weight: 600">
                {{ row.org_name || '示范企业' }}
              </div>
              <div class="cell-muted">
                {{ row.creator_name || '企业成员' }}
                <span v-if="row.creator_email">({{ row.creator_email }})</span>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="对话评估标题与场景" min-width="210">
            <template #default="{ row }">
              <div style="font-weight: 600">{{ row.title }}</div>
              <div class="cell-muted">
                [{{ row.country_code }} {{ row.country_name_zh }}]
                {{ row.vehicle_model || '待填写车型' }} ·
                {{ row.scenario_label || '待选择场景' }}
              </div>
            </template>
          </el-table-column>

          <el-table-column label="工作流状态与当前环节" min-width="220">
            <template #default="{ row }">
              <div>
                <el-tag size="small" :type="STATUS_TAG_TYPE[row.status] || 'info'">
                  {{ STATUS_LABEL_MAP[row.status] || row.status }}
                </el-tag>
                <el-tag size="small" type="info" style="margin-left: 4px">
                  完备度 {{ row.sufficiency_score }}%
                </el-tag>
              </div>
              <div v-if="row.current_active_node_title" class="cell-active-node">
                {{ row.current_active_node_title }}
              </div>
              <div v-else class="cell-muted">
                已完成 {{ row.steps_count }} 个环节 · 结论 {{ row.findings_count }} 条 · 报告 v{{
                  row.latest_report_version
                }}
              </div>
            </template>
          </el-table-column>

          <el-table-column label="对话问答 / 耗时 / Token" width="175">
            <template #default="{ row }">
              <div>问答消息: {{ row.messages_count }} 条</div>
              <div class="cell-muted">
                {{ row.total_duration_ms }} ms · {{ row.total_tokens }} tok · 退回
                {{ row.backtrack_count }} 次
              </div>
            </template>
          </el-table-column>

          <el-table-column label="最后更新时间" width="155">
            <template #default="{ row }">
              {{ formatTime(row.updated_at) }}
            </template>
          </el-table-column>

          <el-table-column label="管理操作" width="250" fixed="right">
            <template #default="{ row }">
              <el-button
                size="small"
                type="primary"
                plain
                data-testid="btn-track-on-canvas"
                @click="trackConversationOnCanvas(row)"
              >
                画布跟踪
              </el-button>
              <el-button
                size="small"
                type="success"
                plain
                data-testid="btn-inspect-stages"
                @click="openStageDeliverables(row)"
              >
                展开各环节产出
              </el-button>
              <el-button
                v-if="row.sufficiency_score >= 100 && row.status !== 'running'"
                size="small"
                type="warning"
                plain
                data-testid="btn-rerun-workflow"
                @click="rerunTaskWorkflow(row)"
              >
                重跑
              </el-button>
              <el-button
                size="small"
                type="danger"
                plain
                data-testid="btn-delete-workflow"
                @click="deleteTaskRecord(row)"
              >
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-drawer>

    <!-- 展开查看当前对话工作流每个环节的产出抽屉 -->
    <el-drawer
      v-model="stageDeliverablesVisible"
      :title="`对话工作流全环节产出详情 · ${selectedTaskDetail?.conversation_code || ''}`"
      size="76%"
    >
      <div v-if="selectedTaskDetail" data-testid="stage-deliverables-drawer">
        <div class="stage-drawer-top">
          <div class="stage-meta-info">
            <el-tag type="primary" effect="dark" size="small">
              对话 ID: {{ selectedTaskDetail.conversation_code }}
            </el-tag>
            <el-tag :type="STATUS_TAG_TYPE[selectedTaskDetail.status] || 'info'" size="small">
              {{ STATUS_LABEL_MAP[selectedTaskDetail.status] || selectedTaskDetail.status }}
            </el-tag>
            <span class="stage-meta-text">
              企业: {{ selectedTaskDetail.org_name }} | 发起人:
              {{
                selectedTaskDetail.creator_name || selectedTaskDetail.creator_email || '企业用户'
              }}
              | 国家: {{ selectedTaskDetail.country_name_zh }} ({{
                selectedTaskDetail.country_code
              }}) | 车型: {{ selectedTaskDetail.vehicle_model || '待填写' }}
            </span>
          </div>
          <div class="stage-drawer-actions">
            <el-button
              size="small"
              type="primary"
              plain
              data-testid="btn-expand-all-stages"
              @click="expandAllStages"
            >
              全部展开
            </el-button>
            <el-button size="small" @click="collapseAllStages">全部收起</el-button>
          </div>
        </div>

        <el-alert
          v-if="selectedTaskDetail.status === 'running'"
          type="info"
          :closable="false"
          show-icon
          style="margin-bottom: 12px"
        >
          <template #title>
            该对话工作流正在实时运行中（当前环节：{{
              selectedTaskDetail.current_active_node_title || '执行中'
            }}），每完成一个环节将自动在此展开最新产出。
          </template>
        </el-alert>

        <el-collapse v-model="expandedStageNames">
          <!-- 环节 0：用户对话问答与八维度业务事实采集产出 -->
          <el-collapse-item name="stage-chat" data-testid="stage-collapse-chat">
            <template #title>
              <div class="stage-collapse-header">
                <el-tag size="small" type="primary">环节 0 · 对话交互与槽位采集</el-tag>
                <b
                  >用户对话问答记录 ({{ selectedTaskDetail.messages.length }}
                  条) & 已采集业务事实 (完备度
                  {{ selectedTaskDetail.sufficiency_score }}%)</b
                >
              </div>
            </template>

            <div class="stage-deliverable-box">
              <div class="sub-label">一、对话交互问答记录：</div>
              <div v-if="selectedTaskDetail.messages.length === 0" class="cell-muted">
                该任务通过 API 直接传入初始事实创建，无多轮聊天消息记录。
              </div>
              <div v-else class="chat-transcript-list">
                <div
                  v-for="m in selectedTaskDetail.messages"
                  :key="m.id"
                  class="transcript-item"
                  :class="m.role"
                >
                  <el-tag size="small" :type="m.role === 'user' ? 'success' : 'info'">
                    {{ m.role === 'user' ? '用户回答' : '合规助手' }}
                  </el-tag>
                  <span class="transcript-content">{{ m.content }}</span>
                </div>
              </div>

              <div class="sub-label" style="margin-top: 12px">
                二、八维度必填业务事实采集产出表：
              </div>
              <el-table :data="selectedTaskDetail.missing_facts" border size="small">
                <el-table-column prop="dimension_code" label="对应维度" width="90" />
                <el-table-column prop="fact_key" label="槽位标识" width="190" />
                <el-table-column prop="question_zh" label="采集问题" min-width="220" />
                <el-table-column label="用户确认的业务事实产出" min-width="240">
                  <template #default="{ row }">
                    <span v-if="row.current_value">{{ row.current_value }}</span>
                    <span v-else class="cell-muted">（尚未补充）</span>
                  </template>
                </el-table-column>
                <el-table-column label="状态" width="95">
                  <template #default="{ row }">
                    <el-tag size="small" :type="row.is_fulfilled ? 'success' : 'warning'">
                      {{ row.is_fulfilled ? '已采集' : '待补充' }}
                    </el-tag>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </el-collapse-item>

          <!-- 环节 1..N：每个 Agent 节点执行环节的详细产出 -->
          <el-collapse-item
            v-for="tr in selectedTaskDetail.step_traces"
            :key="tr.id"
            :name="`stage-step-${tr.seq_no}`"
            :data-testid="`stage-collapse-step-${tr.seq_no}`"
          >
            <template #title>
              <div class="stage-collapse-header">
                <el-tag
                  size="small"
                  :type="
                    tr.is_backtrack ? 'danger' : tr.status === 'passed' ? 'success' : 'warning'
                  "
                >
                  步骤 #{{ tr.seq_no }} ·
                  {{ PHASE_LABEL_MAP[tr.phase] || tr.phase }}
                  · {{ tr.workflow_version_id ? '执行配置 ' + tr.workflow_version_id.slice(0, 8) : '历史配置未记录' }}
                </el-tag>
                <b>{{ tr.agent_name }}</b>
                <span class="cell-muted">
                  （耗时 {{ tr.duration_ms }} ms · {{ tr.tokens_used }} tokens · 工具调用
                  {{ tr.tool_calls.length }} 次）
                </span>
              </div>
            </template>

            <div class="stage-deliverable-box">
              <div class="stage-summary-line">
                <strong>本环节执行结论：</strong>{{ tr.business_summary }}
              </div>

              <div v-if="tr.is_backtrack" class="backtrack-detail-box" style="margin-bottom: 10px">
                <div><strong>质检退回原因：</strong>{{ tr.backtrack_reason }}</div>
                <div v-if="tr.backtrack_resolution" style="margin-top: 4px">
                  <strong>补证闭环结果：</strong>{{ tr.backtrack_resolution }}
                </div>
              </div>

              <!-- 针对 retrieval 环节：展示每个维度检索出的候选条款明细 -->
              <template
                v-if="tr.phase === 'retrieval' && tr.output_snapshot.retrieved_clauses_by_dimension"
              >
                <div class="sub-label">本环节检索命中的各维度候选法规条款产出：</div>
                <div
                  v-for="(clauses, dimCode) in tr.output_snapshot
                    .retrieved_clauses_by_dimension as Record<
                    string,
                    Array<Record<string, unknown>>
                  >"
                  :key="dimCode"
                  class="dim-output-card"
                >
                  <div class="dim-output-title">
                    <b>{{ dimCode }}</b> · 检索命中候选条款 {{ clauses.length }} 条
                  </div>
                  <el-table v-if="clauses.length > 0" :data="clauses" border size="small">
                    <el-table-column prop="document_title" label="法规文件" min-width="180" />
                    <el-table-column prop="clause_number" label="条款号" width="110" />
                    <el-table-column prop="version_no" label="版本" width="70" />
                    <el-table-column prop="snippet" label="原文片段摘录" min-width="260" />
                  </el-table>
                </div>
              </template>

              <!-- 针对 analysis / re_analysis 环节：展示每个维度的模型分析初稿产出 -->
              <template
                v-if="
                  (tr.phase === 'analysis' || tr.phase === 're_analysis') &&
                  tr.output_snapshot.dimension_outputs
                "
              >
                <div class="sub-label">本环节各维度模型分析产出明细：</div>
                <div
                  v-for="(dimOut, dimCode) in tr.output_snapshot.dimension_outputs as Record<
                    string,
                    Record<string, unknown>
                  >"
                  :key="dimCode"
                  class="dim-output-card"
                >
                  <div class="dim-output-title">
                    <b>{{ dimCode }} {{ dimOut.dimension_title }}</b>
                    <el-tag size="small" type="info" style="margin-left: 8px">
                      模型: {{ dimOut.model || 'deepseek-flash' }} · {{ dimOut.tokens || 0 }} tokens
                    </el-tag>
                    <el-tag
                      size="small"
                      :type="Number(dimOut.findings_count || 0) > 0 ? 'success' : 'warning'"
                      style="margin-left: 6px"
                    >
                      产出结论 {{ dimOut.findings_count || 0 }} 条
                    </el-tag>
                  </div>
                  <div v-if="dimOut.summary" style="margin: 6px 0; font-size: 13px">
                    <strong>维度小结：</strong>{{ dimOut.summary }}
                  </div>
                  <div v-if="dimOut.gaps" class="cell-muted" style="margin-bottom: 6px">
                    <strong>缺口说明：</strong>{{ dimOut.gaps }}
                  </div>
                  <el-table
                    v-if="Array.isArray(dimOut.findings) && dimOut.findings.length > 0"
                    :data="dimOut.findings as Array<Record<string, unknown>>"
                    border
                    size="small"
                  >
                    <el-table-column prop="business_fact" label="对应业务事实" min-width="140" />
                    <el-table-column prop="obligation" label="提取合规义务" min-width="180" />
                    <el-table-column prop="risk_level" label="风险" width="80" />
                    <el-table-column prop="quote" label="引用原文片段 (待核验)" min-width="220" />
                  </el-table>
                </div>
              </template>

              <!-- 针对 verification / re_verification 环节：展示每条结论的子串与数字核验产出 -->
              <template
                v-if="
                  (tr.phase === 'verification' || tr.phase === 're_verification') &&
                  tr.output_snapshot.verification_items
                "
              >
                <div class="sub-label">
                  本环节程序化原文子串与数字核验明细（通过
                  {{ tr.output_snapshot.verified_passed_count }} 条 / 拦截
                  {{ tr.output_snapshot.verified_failed_count }} 条）：
                </div>
                <el-table
                  :data="tr.output_snapshot.verification_items as Array<Record<string, unknown>>"
                  border
                  size="small"
                >
                  <el-table-column prop="dimension_code" label="维度" width="70" />
                  <el-table-column prop="document_title" label="法规文件" min-width="160" />
                  <el-table-column prop="clause_number" label="条款号" width="95" />
                  <el-table-column prop="obligation" label="合规义务(含数字校验)" min-width="180" />
                  <el-table-column prop="quote" label="被核验原文片段" min-width="200" />
                  <el-table-column label="核验结果" width="95">
                    <template #default="{ row }">
                      <el-tag size="small" :type="row.passed ? 'success' : 'danger'">
                        {{ row.passed ? '核验通过' : '拦截退回' }}
                      </el-tag>
                    </template>
                  </el-table-column>
                </el-table>
              </template>

              <!-- 针对 compilation 环节：展示八维度报告章节产出 -->
              <template
                v-if="tr.phase === 'compilation' && tr.output_snapshot.dimensions_deliverables"
              >
                <div class="sub-label">
                  本环节编制的八维度报告章节产出（版本 v{{ tr.output_snapshot.report_version }}）：
                </div>
                <el-table
                  :data="
                    tr.output_snapshot.dimensions_deliverables as Array<Record<string, unknown>>
                  "
                  border
                  size="small"
                >
                  <el-table-column prop="dimension_code" label="维度" width="70" />
                  <el-table-column prop="dimension_title" label="章节标题" width="180" />
                  <el-table-column label="状态" width="100">
                    <template #default="{ row }">
                      <el-tag
                        size="small"
                        :type="row.status === 'COMPLETE' ? 'success' : 'warning'"
                      >
                        {{ row.status === 'COMPLETE' ? '依据已核验' : '部分缺口' }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column prop="risk_level" label="风险等级" width="110" />
                  <el-table-column prop="citations_count" label="核验引用数" width="95" />
                  <el-table-column prop="content_markdown" label="章节正文产出" min-width="260" />
                </el-table>
              </template>

              <!-- 工具调用与原始输入输出 JSON 快照 -->
              <div v-if="tr.tool_calls.length > 0" style="margin-top: 10px">
                <div class="sub-label">
                  本环节调用的 MCP 工具 / 模型记录 ({{ tr.tool_calls.length }})：
                </div>
                <el-table :data="tr.tool_calls" border size="small">
                  <el-table-column prop="tool_label" label="工具/模型名称" min-width="160" />
                  <el-table-column prop="tool_name" label="方法标识" width="180" />
                  <el-table-column prop="result_summary" label="返回摘要" min-width="220" />
                  <el-table-column prop="duration_ms" label="耗时(ms)" width="90" />
                </el-table>
              </div>

              <div class="snapshot-block" style="margin-top: 10px">
                <div>
                  <strong>环节输入快照 (input_snapshot):</strong>
                  <pre>{{ JSON.stringify(tr.input_snapshot, null, 2) }}</pre>
                </div>
                <div>
                  <strong>环节输出快照 (output_snapshot):</strong>
                  <pre>{{ JSON.stringify(tr.output_snapshot, null, 2) }}</pre>
                </div>
              </div>
            </div>
          </el-collapse-item>
        </el-collapse>
      </div>
    </el-drawer>

    <!-- 导出多智能体配置 JSON DSL 弹窗 -->
    <el-dialog
      v-model="exportDialogVisible"
      title="导出多智能体编排配置 (Dify-Compatible Workflow DSL)"
      width="720px"
    >
      <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
        导出的 JSON 配置包包含全部 7 个智能体节点的拓扑坐标、上下游连接边、MCP 工具绑定、I/O Schema
        与只读安全策略，可用于版本归档、长久维护与跨环境迁移。
      </el-alert>
      <el-input
        v-model="exportedDslText"
        type="textarea"
        :rows="15"
        readonly
        data-testid="export-dsl-textarea"
      />
      <template #footer>
        <el-button @click="exportDialogVisible = false">关闭</el-button>
        <el-button @click="copyExportedJson">复制 JSON</el-button>
        <el-button type="primary" data-testid="btn-download-dsl" @click="downloadExportedJson">
          下载 .json 配置文件
        </el-button>
      </template>
    </el-dialog>

    <!-- 导入多智能体配置 JSON DSL 弹窗 -->
    <el-dialog
      v-model="importDialogVisible"
      title="导入多智能体编排配置包（生成新版本并保持生产只读锁）"
      width="720px"
    >
      <el-alert type="info" :closable="false" style="margin-bottom: 16px">
        只允许修改节点白名单参数。代码入口、工具绑定、拓扑与核验闸门不可修改；导入后需单独激活，新运行才使用该版本。
      </el-alert>
      <div style="margin-bottom: 12px">
        <label style="font-weight: 600; display: block; margin-bottom: 6px">
          方式一：选择本地 .json 配置文件
        </label>
        <input type="file" accept=".json,application/json" @change="handleImportFileChange" />
      </div>
      <div style="margin-bottom: 12px">
        <label style="font-weight: 600; display: block; margin-bottom: 6px"> 版本变更说明 </label>
        <el-input v-model="importChangeNote" placeholder="填写本次导入维护说明" />
      </div>
      <div>
        <label style="font-weight: 600; display: block; margin-bottom: 6px">
          方式二：粘贴或检查 Workflow DSL JSON 内容
        </label>
        <el-input
          v-model="importDslText"
          type="textarea"
          :rows="12"
          data-testid="import-dsl-textarea"
        />
      </div>
      <template #footer>
        <el-button @click="importDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="importing"
          data-testid="btn-confirm-import-workflow"
          @click="submitImportWorkflow"
        >
          校验并保存（不激活）
        </el-button>
      </template>
    </el-dialog>

    <!-- 工程来源标注集：真实检索、逐字核验、独立模型审查，人工复核单列 -->
    <el-drawer
      v-model="benchmarkDrawerVisible"
      title="分析质量评测 · 五国 / 八维度"
      size="65%"
    >
      <div v-loading="loadingBenchmarks">
        <template v-if="benchmarkData">
          <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 12px">
            <template #title>{{ benchmarkData.note }}</template>
          </el-alert>
          <div class="benchmark-summary-cards">
            <div class="bm-card">
              <div class="bm-num">
                {{ benchmarkData.recall_at_5 === null ? '未评测' : `${(benchmarkData.recall_at_5 * 100).toFixed(1)}%` }}
              </div>
              <div class="bm-label">Recall@5 · {{ benchmarkData.retrieval_hits }}/{{ benchmarkData.retrieval_expected }} 条期望条款</div>
            </div>
            <div class="bm-card">
              <div class="bm-num">{{ benchmarkData.quote_fidelity === null ? '未评测' : `${(benchmarkData.quote_fidelity * 100).toFixed(1)}%` }}</div>
              <div class="bm-label">引文忠实度 · {{ benchmarkData.faithful_findings }}/{{ benchmarkData.findings_total }} 条结论</div>
            </div>
            <div class="bm-card">
              <div class="bm-num">{{ benchmarkData.fact_consistency === null ? '未评测' : `${(benchmarkData.fact_consistency * 100).toFixed(1)}%` }}</div>
              <div class="bm-label">引文支持结论 · llm_judged · {{ benchmarkData.supported_findings }}/{{ benchmarkData.judged_findings }}</div>
              <div class="bm-label">Judge覆盖 {{ benchmarkData.judged_findings }}/{{ benchmarkData.findings_total }} 条</div>
            </div>
            <div class="bm-card">
              <div class="bm-num">人工核对</div>
              <div class="bm-label">
                {{ benchmarkData.human_review_status }}
              </div>
            </div>
          </div>
          <p data-testid="evaluation-completeness">已完成 {{ benchmarkData.completed_cases }}/{{ benchmarkData.total_cases }} 题 · 技术错误 {{ benchmarkData.technical_error_cases }} 题</p>
          <p>其中 {{ benchmarkData.cases.filter(c => c.findings_total > 0).length }} 题有分析项（含拒绝/失败尝试）；无输出不算引文通过。</p>

          <el-table :data="benchmarkData.cases" border stripe size="small" style="margin-top: 16px">
            <el-table-column prop="question_id" label="题号" width="65" />
            <el-table-column prop="country_code" label="国家" width="65" />
            <el-table-column prop="dimension_code" label="维度" width="65" />
            <el-table-column prop="scenario_question" label="测试问题" min-width="220" />
            <el-table-column label="期望条款" width="95"><template #default="{ row }">{{ row.retrieval_hits }}/{{ row.retrieval_expected }}</template></el-table-column>
            <el-table-column label="引文通过" width="95"><template #default="{ row }">{{ row.faithful_findings }}/{{ row.findings_total }}</template></el-table-column>
            <el-table-column label="Judge支持" width="100"><template #default="{ row }">{{ row.supported_findings }}/{{ row.judged_findings }}</template></el-table-column>
            <el-table-column label="技术错误" min-width="180"><template #default="{ row }">{{ row.analysis_error || row.judge_error || '无' }}</template></el-table-column>
          </el-table>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.runtime-settings { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; max-height: 240px; overflow: auto; }
.agent-studio-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 100px);
  min-height: 700px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  overflow: hidden;
}

.studio-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 18px;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
  flex-wrap: wrap;
  gap: 12px;
}

.topbar-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.studio-badge {
  background: linear-gradient(135deg, #1d4ed8, #4f46e5);
  color: #fff;
  font-weight: 700;
  font-size: 13px;
  padding: 6px 10px;
  border-radius: 6px;
  letter-spacing: 0.3px;
}

.workflow-title {
  margin: 0 0 4px 0;
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
}

.workflow-meta {
  display: flex;
  align-items: center;
  gap: 8px;
}

.topbar-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.version-selector,
.task-selector {
  display: flex;
  align-items: center;
  gap: 6px;
}

.ctrl-label {
  font-size: 13px;
  color: #475569;
  font-weight: 600;
}

.studio-workspace {
  display: flex;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.canvas-wrapper {
  flex: 1;
  position: relative;
  display: flex;
  flex-direction: column;
  background-color: #f1f5f9;
  background-image: radial-gradient(#cbd5e1 1.2px, transparent 1.2px);
  background-size: 20px 20px;
  overflow: hidden;
}

.canvas-floating-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  background: rgba(255, 255, 255, 0.92);
  border-bottom: 1px solid #e2e8f0;
  z-index: 5;
  flex-wrap: wrap;
}

.zoom-controls {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 6px;
}

.zoom-text {
  font-size: 13px;
  font-weight: 600;
  color: #334155;
  min-width: 42px;
  text-align: center;
}

.canvas-scroll-area {
  flex: 1;
  overflow: auto;
  position: relative;
  padding: 16px;
}

.canvas-stage {
  position: relative;
  width: 1580px;
  height: 550px;
}

.edges-svg {
  position: absolute;
  left: 0;
  top: 0;
  pointer-events: none;
  z-index: 1;
}

.dify-node-card {
  position: absolute;
  z-index: 2;
  background: #ffffff;
  border: 2px solid #e2e8f0;
  border-radius: 12px;
  padding: 10px 12px;
  box-shadow: 0 4px 14px rgba(15, 23, 42, 0.06);
  cursor: pointer;
  transition: all 0.16s ease;
  user-select: none;
}

.dify-node-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 20px rgba(37, 99, 235, 0.14);
}

.dify-node-card.active {
  border-color: #2563eb;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.22);
}

.dify-node-card.has-backtrack {
  border-color: #f97316;
}

.port-dot {
  position: absolute;
  top: 72px;
  width: 10px;
  height: 10px;
  background: #ffffff;
  border: 2.5px solid #2563eb;
  border-radius: 50%;
}

.port-in {
  left: -6px;
}

.port-out {
  right: -6px;
}

.node-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 6px;
  margin-bottom: 6px;
}

.node-type-pill {
  font-size: 13px;
  font-weight: 700;
  padding: 2px 8px;
  border-radius: 999px;
}

.node-lock-tag {
  font-size: 13px;
  color: #64748b;
  font-weight: 600;
}

.node-card-title {
  font-size: 14px;
  font-weight: 700;
  color: #0f172a;
  line-height: 1.3;
}

.node-card-subtitle {
  font-size: 13px;
  color: #64748b;
  margin-bottom: 8px;
}

.node-card-meta {
  background: #f8fafc;
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 13px;
  border: 1px solid #f1f5f9;
}

.meta-row {
  display: flex;
  justify-content: space-between;
  line-height: 1.5;
}

.meta-key {
  color: #64748b;
}

.meta-val {
  color: #1e293b;
  font-weight: 600;
}

.node-trace-footer {
  margin-top: 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 6px;
  border-top: 1px dashed #e2e8f0;
}

.trace-ms {
  font-size: 13px;
  color: #475569;
  font-weight: 600;
}

.inspector-sidebar {
  width: 440px;
  background: #ffffff;
  border-left: 1px solid #e2e8f0;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  padding: 14px 16px;
}

.inspector-header {
  padding-bottom: 10px;
  border-bottom: 1px solid #f1f5f9;
  flex-shrink: 0;
}

.inspector-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.inspector-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}

.inspector-node-title {
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
}

.inspector-node-sub {
  font-size: 13px;
  color: #64748b;
  margin-top: 4px;
}

.readonly-banner {
  margin: 10px 0;
  flex-shrink: 0;
}

.inspector-section {
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid #f1f5f9;
}

.sec-title {
  font-size: 13px;
  font-weight: 700;
  color: #0f172a;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.sec-desc {
  margin: 0;
  font-size: 13px;
  color: #334155;
  line-height: 1.6;
}

.link-sub {
  font-size: 13px;
  font-weight: 600;
  color: #475569;
  margin-bottom: 4px;
}

.link-item {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 6px 10px;
  margin-bottom: 6px;
  cursor: pointer;
}

.link-item:hover {
  border-color: #3b82f6;
}

.link-node-name {
  font-size: 13px;
  font-weight: 700;
  color: #1e293b;
  margin-left: 6px;
}

.link-cond {
  font-size: 13px;
  color: #64748b;
  margin-top: 3px;
}

.empty-link {
  font-size: 13px;
  color: #94a3b8;
}

.readonly-form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.form-item label {
  display: block;
  font-size: 13px;
  color: #475569;
  margin-bottom: 3px;
}

.code-readonly-box {
  background: #0f172a;
  color: #38bdf8;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
}

.io-title {
  font-size: 13px;
  font-weight: 600;
  color: #475569;
  margin-bottom: 4px;
}

.mcp-tool-card {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 10px;
}

.tool-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.tool-title {
  font-weight: 700;
  font-size: 13px;
  color: #0f172a;
}

.tool-desc {
  font-size: 13px;
  color: #475569;
  margin: 0 0 8px 0;
}

.tool-schema-row {
  font-size: 13px;
  color: #334155;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.trace-step-card {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
  background: #f8fafc;
}

.trace-step-card.trace-backtrack {
  border-color: #f97316;
  background: #fff7ed;
}

.trace-step-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.trace-metrics {
  font-size: 13px;
  color: #475569;
  font-weight: 600;
}

.trace-summary {
  font-size: 13px;
  font-weight: 600;
  color: #0f172a;
  line-height: 1.5;
}

.backtrack-detail-box {
  margin-top: 8px;
  padding: 8px;
  background: #ffedd5;
  border-left: 3px solid #ea580c;
  border-radius: 4px;
  font-size: 13px;
  color: #9a3412;
}

.trace-tools-list {
  margin-top: 8px;
}

.sub-label {
  font-size: 13px;
  font-weight: 700;
  color: #334155;
  margin-bottom: 4px;
}

.trace-tool-item {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 6px 8px;
  margin-bottom: 6px;
  font-size: 13px;
}

.tc-top {
  display: flex;
  justify-content: space-between;
  margin-bottom: 3px;
}

.snapshot-block pre {
  background: #0f172a;
  color: #e2e8f0;
  padding: 6px 8px;
  border-radius: 6px;
  font-size: 13px;
  overflow-x: auto;
  max-height: 140px;
}

.benchmark-summary-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}

.bm-card {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 12px;
  text-align: center;
}

.bm-num {
  font-size: 24px;
  font-weight: 800;
  color: #1d4ed8;
}

.bm-label {
  font-size: 13px;
  color: #475569;
  margin-top: 4px;
}

.live-monitor-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 18px;
  background: #f0f9ff;
  border-bottom: 1px solid #bae6fd;
  flex-wrap: wrap;
  gap: 10px;
}

.live-monitor-left,
.live-monitor-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.live-running-pills {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.live-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: #10b981;
  display: inline-block;
}

.live-dot.active {
  background: #2563eb;
  box-shadow: 0 0 0 4px rgba(37, 99, 235, 0.25);
  animation: pulse-dot 1.2s infinite ease-in-out;
}

@keyframes pulse-dot {
  0%,
  100% {
    transform: scale(1);
    opacity: 1;
  }
  50% {
    transform: scale(1.25);
    opacity: 0.75;
  }
}

.dify-node-card.is-live-running {
  border-color: #2563eb;
  box-shadow:
    0 0 0 4px rgba(37, 99, 235, 0.28),
    0 8px 24px rgba(37, 99, 235, 0.22);
  animation: pulse-card 1.5s infinite ease-in-out;
}

.dify-node-card.is-collecting {
  border-color: #0891b2;
  box-shadow: 0 0 0 3px rgba(8, 145, 178, 0.2);
}

@keyframes pulse-card {
  0%,
  100% {
    box-shadow:
      0 0 0 3px rgba(37, 99, 235, 0.22),
      0 6px 18px rgba(37, 99, 235, 0.16);
  }
  50% {
    box-shadow:
      0 0 0 6px rgba(37, 99, 235, 0.35),
      0 10px 26px rgba(37, 99, 235, 0.28);
  }
}

.node-live-running-banner {
  margin-top: 8px;
  padding: 4px 8px;
  border-radius: 6px;
  background: linear-gradient(90deg, #2563eb, #1d4ed8);
  color: #ffffff;
  font-size: 13px;
  font-weight: 700;
  text-align: center;
}

.node-collecting-banner {
  margin-top: 8px;
  padding: 4px 8px;
  border-radius: 6px;
  background: #ecfeff;
  border: 1px solid #a5f3fc;
  color: #0e7490;
  font-size: 13px;
  font-weight: 700;
  text-align: center;
}

.inspector-trace-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 10px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  margin-bottom: 10px;
}

.history-filter-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}

.conv-code-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 700;
}

.cell-muted {
  font-size: 13px;
  color: #64748b;
}

.cell-active-node {
  margin-top: 4px;
  font-size: 13px;
  font-weight: 700;
  color: #1d4ed8;
}

.stage-drawer-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
  gap: 8px;
}

.stage-meta-info {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.stage-meta-text {
  font-size: 13px;
  color: #334155;
  font-weight: 600;
}

.stage-drawer-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.stage-collapse-header {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.stage-deliverable-box {
  padding: 8px 4px;
}

.stage-summary-line {
  font-size: 13px;
  color: #0f172a;
  margin-bottom: 8px;
}

.chat-transcript-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 240px;
  overflow-y: auto;
  padding: 8px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
}

.transcript-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
  line-height: 1.5;
}

.transcript-content {
  white-space: pre-line;
  color: #1e293b;
}

.dim-output-card {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 10px;
}

.dim-output-title {
  display: flex;
  align-items: center;
  margin-bottom: 6px;
  font-size: 13px;
  color: #0f172a;
}

.agent-studio-page {
  height: calc(100dvh - 145px);
  min-height: 700px;
  border-radius: 14px;
  background: #fff;
  border-color: var(--cy-line);
}
.studio-topbar {
  padding: 20px 24px;
  gap: 18px;
}
.topbar-left {
  gap: 12px;
}
.workflow-title {
  font-size: 23px;
  font-weight: 600;
  margin-bottom: 8px;
}
.studio-badge {
  display: none;
}
.workflow-meta {
  gap: 8px;
}
.topbar-actions {
  gap: 10px;
}
.ctrl-label {
  font-size: 12px;
  color: #8493a0;
  font-weight: 400;
}
.canvas-wrapper {
  background-color: #f7f9fc;
  background-image: radial-gradient(#dce4ed 0.8px, transparent 0.8px);
  background-size: 22px 22px;
}
.canvas-floating-bar {
  padding: 12px 16px;
  gap: 10px;
}
.canvas-floating-bar .el-tag {
  background: transparent;
  border-color: transparent;
  color: #8293a0;
}
.dify-node-card {
  border-width: 1px;
  padding: 16px;
  border-color: #d9e2ed;
  box-shadow: 0 4px 16px #273f5810;
  transition:
    border-color 0.18s,
    box-shadow 0.18s;
}
.dify-node-card.active {
  border-width: 2px;
  box-shadow: 0 5px 20px #315fe817;
}
.node-card-title {
  font-size: 16px;
  font-weight: 600;
}
.node-card-subtitle {
  font-size: 13px;
  line-height: 1.7;
}
.inspector-sidebar {
  width: 370px;
  max-width: 40%;
  min-width: 300px;
  background: #fff;
}
.live-monitor-bar {
  background: #f9fbfd;
  border-color: var(--cy-line);
  padding: 12px 20px;
  gap: 12px;
}
.live-monitor-left,
.live-monitor-right {
  gap: 10px;
}
.live-monitor-left .el-tag,
.live-monitor-right .el-tag {
  font-size: 12px;
}
.live-monitor-bar .el-tag--dark {
  background: #edf3fa;
  border-color: #dce5ee;
  color: #506d89;
}
.live-dot {
  display: none;
}
.readonly-banner {
  background: #f4f6f9;
  border-radius: 8px;
}
.history-filter-bar,
.stage-drawer-actions {
  gap: 10px;
  flex-wrap: wrap;
}
.trace-step-card {
  padding: 18px;
}
.form-item label {
  font-size: 13px;
}
.node-type-pill,
.node-lock-tag {
  font-size: 11px;
}
@media (max-width: 1100px) {
  .studio-topbar {
    padding: 18px;
  }
  .topbar-actions {
    width: 100%;
  }
  .inspector-sidebar {
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    z-index: 10;
    box-shadow: -8px 0 32px #263e5522;
    max-width: 90%;
  }
  .studio-workspace {
    position: relative;
  }
  .live-monitor-left,
  .live-monitor-right {
    flex-wrap: wrap;
  }
  .task-selector .el-select {
    width: 240px !important;
  }
}
@media (max-width: 800px) {
  .agent-studio-page {
    height: auto;
    min-height: 820px;
  }
  .studio-workspace {
    min-height: 550px;
  }
  .studio-topbar {
    padding: 16px;
  }
  .topbar-actions {
    align-items: flex-start;
  }
  .task-selector {
    width: 100%;
    flex-wrap: wrap;
  }
  .task-selector .el-select {
    width: 100% !important;
  }
  .live-monitor-bar {
    padding: 12px;
    flex-wrap: wrap;
  }
  .canvas-floating-bar {
    gap: 4px;
  }
  .canvas-stage {
    transform-origin: 0 0;
  }
  .inspector-sidebar {
    min-width: 0;
    width: 340px;
  }
  .zoom-controls {
    margin-left: 0;
  }
}

/* Node cards show the business role; implementation details remain in the inspector. */
.node-card-subtitle,
.node-lock-tag {
  display: none;
}
.node-card-meta .meta-row:first-child {
  display: none;
}
.node-card-title {
  font-size: 18px;
  line-height: 1.5;
}
.task-metrics-list {
  display: grid;
  gap: 10px;
}
.layout-hint {
  padding: 9px 16px;
  color: #667b8b;
  font-size: 12px;
  border-bottom: 1px solid #e7edf3;
  background: #ffffffa8;
}
.canvas-extent {
  position: relative;
}
.canvas-stage {
  position: absolute;
  left: 0;
  top: 0;
}
.canvas-scroll-area {
  min-height: 360px;
}
.dify-node-card {
  cursor: grab;
  touch-action: none;
}
.dify-node-card:hover {
  transform: none;
}
.dify-node-card.is-dragging {
  cursor: grabbing;
  z-index: 4;
  transition: none;
  box-shadow: 0 10px 30px #263e552b;
}
.dify-node-card.active {
  border-width: 1px;
}
.port-dot {
  top: 50%;
  transform: translateY(-50%);
}
.node-trace-footer {
  flex-wrap: wrap;
  gap: 6px;
}
</style>
