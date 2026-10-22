<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

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
  nodes_count: number
  edges_count: number
  change_note: string | null
  created_at: string
}

interface TaskSummary {
  id: string
  title: string
  country_code: string
  country_name_zh: string
  vehicle_model: string
  scenario_label: string
  status: string
  sufficiency_score: number
  total_tokens: number
  total_duration_ms: number
  mcp_calls_count: number
  backtrack_count: number
}

interface StepTrace {
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
}

interface TaskDetail {
  id: string
  title: string
  country_code: string
  country_name_zh: string
  status: string
  step_traces: StepTrace[]
  total_tokens: number
  total_duration_ms: number
  mcp_calls_count: number
  backtrack_count: number
}

interface BenchmarkSummary {
  total_cases: number
  passed_cases: number
  recall_rate_pct: number
  substring_verify_rate_pct: number
  dimension_completeness_pct: number
  fact_consistency_pct: number
  cases: Array<{
    question_id: string
    country_code: string
    dimension_code: string
    scenario_question: string
    expected_regulation: string
    expected_clause: string
    matched_document_title: string
    matched_clause_number: string
    substring_verified: boolean
    fact_consistent: boolean
    passed: boolean
    evidence_snippet: string
  }>
}

const loading = ref(false)
const workflow = ref<WorkflowConfig | null>(null)
const versions = ref<WorkflowVersionSummary[]>([])
const selectedVersionNo = ref<number | undefined>(undefined)

const tasks = ref<TaskSummary[]>([])
const selectedTaskId = ref<string>('')
const selectedTaskDetail = ref<TaskDetail | null>(null)

const selectedNodeId = ref<string>('node_dimension_analyst')
const inspectorTab = ref<'config' | 'tools' | 'trace'>('config')
const canvasScale = ref<number>(0.72)

// 导入/导出弹窗状态
const exportDialogVisible = ref(false)
const exportedDslText = ref('')
const importDialogVisible = ref(false)
const importDslText = ref('')
const importChangeNote = ref('导入维护更新的多智能体编排配置包')
const importing = ref(false)

// 10道独立评测集抽屉
const benchmarkDrawerVisible = ref(false)
const benchmarkData = ref<BenchmarkSummary | null>(null)
const loadingBenchmarks = ref(false)

const NODE_WIDTH = 240
const NODE_HEIGHT = 148

const nodeMap = computed(() => {
  const m = new Map<string, WorkflowNode>()
  for (const n of workflow.value?.nodes || []) {
    m.set(n.node_id, n)
  }
  return m
})

const selectedNode = computed<WorkflowNode | null>(() => {
  if (!workflow.value) return null
  return (
    nodeMap.value.get(selectedNodeId.value) ||
    workflow.value.nodes[0] ||
    null
  )
})

const upstreamEdges = computed(() => {
  if (!workflow.value || !selectedNode.value) return []
  return workflow.value.edges.filter(
    (e) => e.target_node_id === selectedNode.value?.node_id,
  )
})

const downstreamEdges = computed(() => {
  if (!workflow.value || !selectedNode.value) return []
  return workflow.value.edges.filter(
    (e) => e.source_node_id === selectedNode.value?.node_id,
  )
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
  for (const tr of selectedTaskDetail.value?.step_traces || []) {
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
    if (tr.status === 'backtracked') prev.status = 'backtracked'
    map.set(tr.agent_node_id, prev)
  }
  return map
})

const selectedNodeTraces = computed(() => {
  if (!selectedNode.value || !selectedTaskDetail.value) return []
  return selectedTaskDetail.value.step_traces.filter(
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

      if (e.edge_type === 'backtrack') {
        // 质检退回分析补证边：从 verifier 左侧或顶部连回 analyst 底部
        const x1 = src.position_x + NODE_WIDTH / 2
        const y1 = src.position_y
        const x2 = dst.position_x + NODE_WIDTH / 2
        const y2 = dst.position_y + NODE_HEIGHT
        const path = `M ${x1} ${y1} C ${x1 - 90} ${y1 - 40}, ${x2 - 90} ${y2 + 40}, ${x2} ${y2}`
        return {
          ...e,
          path,
          labelX: x1 - 95,
          labelY: (y1 + y2) / 2,
        }
      }

      const x1 = src.position_x + NODE_WIDTH
      const y1 = src.position_y + NODE_HEIGHT / 2
      const x2 = dst.position_x
      const y2 = dst.position_y + NODE_HEIGHT / 2
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
    if (
      wf.nodes.length > 0 &&
      !wf.nodes.some((n) => n.node_id === selectedNodeId.value)
    ) {
      selectedNodeId.value = wf.nodes[0].node_id
    }
    if (!selectedTaskId.value && taskList.length > 0) {
      const sgCompleted =
        taskList.find((t) => t.country_code === 'SG' && t.status === 'completed') ||
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
    selectedTaskDetail.value = await api.get<TaskDetail>(
      `/api/admin/agent-studio/tasks/${taskId}`,
    )
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载任务执行轨迹失败')
  }
}

async function handleVersionChange(verNo: number) {
  await loadStudioData(verNo)
}

async function activateSelectedVersion() {
  if (!selectedVersionNo.value) return
  try {
    const wf = await api.post<WorkflowConfig>(
      `/api/admin/agent-studio/workflow/versions/${selectedVersionNo.value}/activate`,
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
  if (!importDslText.value && workflow.value) {
    const dsl = await api.get<Record<string, unknown>>(
      '/api/admin/agent-studio/workflow/export',
    )
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
    const newWf = await api.post<WorkflowConfig>(
      '/api/admin/agent-studio/workflow/import',
      {
        dsl_payload: parsed,
        change_note: importChangeNote.value,
        activate_immediately: true,
      },
    )
    importDialogVisible.value = false
    await loadStudioData(newWf.version_no)
    ElMessage.success(
      `配置导入成功！已生成并激活版本 v${newWf.version_no}（保持生产只读安全锁）`,
    )
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '导入配置失败')
  } finally {
    importing.value = false
  }
}

async function openBenchmarksDrawer() {
  benchmarkDrawerVisible.value = true
  loadingBenchmarks.value = true
  try {
    benchmarkData.value = await api.get<BenchmarkSummary>(
      '/api/admin/agent-studio/benchmarks',
    )
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载评测集结果失败')
  } finally {
    loadingBenchmarks.value = false
  }
}

function selectNode(nodeId: string) {
  selectedNodeId.value = nodeId
}

onMounted(() => {
  loadStudioData()
})
</script>

<template>
  <div class="agent-studio-page" v-loading="loading">
    <!-- 顶部 Dify 风格工作流导航与锁定状态栏 -->
    <div class="studio-topbar" data-testid="agent-studio-toolbar">
      <div class="topbar-left">
        <div class="studio-badge">Dify-Style Workflow Studio</div>
        <div class="workflow-title-block">
          <h2 class="workflow-title">
            {{ workflow?.name || '多智能体合规评估编排工作台' }}
          </h2>
          <div class="workflow-meta">
            <el-tag type="danger" effect="dark" size="small" data-testid="agent-readonly-lock-badge">
              🔒 生产只读锁定：仅开放查看工作流、连接关系与节点详情（禁止在线修改参数与代码）
            </el-tag>
            <el-tag type="info" size="small">
              DSL: {{ workflow?.dsl_version }}
            </el-tag>
          </div>
        </div>
      </div>

      <div class="topbar-actions">
        <div class="version-selector">
          <span class="ctrl-label">配置版本:</span>
          <el-select
            v-model="selectedVersionNo"
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
            v-if="workflow && !workflow.is_active"
            size="small"
            type="warning"
            plain
            @click="activateSelectedVersion"
          >
            设为当前激活版本
          </el-button>
        </div>

        <div class="task-selector">
          <span class="ctrl-label">叠加运行轨迹:</span>
          <el-select
            v-model="selectedTaskId"
            size="small"
            style="width: 260px"
            data-testid="agent-trace-task-select"
            @change="loadTaskTrace"
          >
            <el-option
              v-for="t in tasks"
              :key="t.id"
              :label="`[${t.country_code}] ${t.title}`"
              :value="t.id"
            />
          </el-select>
        </div>

        <el-button
          size="small"
          type="primary"
          plain
          data-testid="btn-export-workflow"
          @click="openExportModal"
        >
          导出配置 (JSON DSL)
        </el-button>
        <el-button
          size="small"
          type="success"
          plain
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
          10道独立评测集看板
        </el-button>
      </div>
    </div>

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
          <template v-if="selectedTaskDetail">
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
          </template>
          <div class="zoom-controls">
            <el-button
              size="small"
              circle
              @click="canvasScale = Math.max(0.75, +(canvasScale - 0.1).toFixed(2))"
            >
              -
            </el-button>
            <span class="zoom-text">{{ Math.round(canvasScale * 100) }}%</span>
            <el-button
              size="small"
              circle
              @click="canvasScale = Math.min(1.25, +(canvasScale + 0.1).toFixed(2))"
            >
              +
            </el-button>
            <el-button size="small" @click="canvasScale = 1">重置</el-button>
          </div>
        </div>

        <div class="canvas-scroll-area">
          <div
            class="canvas-stage"
            :style="{
              transform: `scale(${canvasScale})`,
              transformOrigin: '0 0',
            }"
          >
            <!-- SVG 连线层 -->
            <svg class="edges-svg" width="1600" height="560">
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
                  :stroke-dasharray="
                    edge.edge_type === 'backtrack' ? '6,4' : undefined
                  "
                  :marker-end="
                    edge.edge_type === 'backtrack'
                      ? 'url(#arrow-backtrack)'
                      : edge.edge_type === 'event_trigger'
                        ? 'url(#arrow-event)'
                        : 'url(#arrow-forward)'
                  "
                />
                <g
                  :transform="`translate(${edge.labelX}, ${edge.labelY})`"
                >
                  <rect
                    x="-78"
                    y="-11"
                    width="156"
                    height="20"
                    rx="10"
                    :fill="
                      edge.edge_type === 'backtrack'
                        ? '#fff7ed'
                        : '#f8fafc'
                    "
                    :stroke="
                      edge.edge_type === 'backtrack'
                        ? '#fdba74'
                        : '#cbd5e1'
                    "
                  />
                  <text
                    text-anchor="middle"
                    dominant-baseline="middle"
                    font-size="11"
                    :fill="
                      edge.edge_type === 'backtrack'
                        ? '#c2410c'
                        : '#334155'
                    "
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
              class="dify-node-card"
              :class="{
                active: selectedNode?.node_id === node.node_id,
                'has-backtrack':
                  nodeTraceStatsMap.get(node.node_id)?.hasBacktrack,
              }"
              :style="{
                left: `${node.position_x}px`,
                top: `${node.position_y}px`,
                width: `${NODE_WIDTH}px`,
              }"
              data-testid="dify-node-card"
              :data-node-id="node.node_id"
              @click="selectNode(node.node_id)"
            >
              <!-- 左侧输入端口圆点 -->
              <span
                v-if="node.node_type !== 'trigger'"
                class="port-dot port-in"
                :style="{ borderColor: node.color }"
              />
              <!-- 右侧输出端口圆点 -->
              <span
                class="port-dot port-out"
                :style="{ borderColor: node.color }"
              />

              <div
                class="node-card-header"
                :style="{ borderTop: `3px solid ${node.color}` }"
              >
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

              <!-- 叠加任务运行耗时与退回状态条 -->
              <div
                v-if="nodeTraceStatsMap.get(node.node_id)"
                class="node-trace-footer"
              >
                <el-tag
                  size="small"
                  :type="
                    nodeTraceStatsMap.get(node.node_id)?.hasBacktrack
                      ? 'warning'
                      : 'success'
                  "
                >
                  {{
                    nodeTraceStatsMap.get(node.node_id)?.hasBacktrack
                      ? `执行 ${nodeTraceStatsMap.get(node.node_id)?.runs} 次 (含补证)`
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

      <!-- 右侧 Dify 风格节点检查器与运行调试栏（只读） -->
      <div
        v-if="selectedNode"
        class="inspector-sidebar"
        data-testid="dify-inspector-panel"
      >
        <div class="inspector-header">
          <div class="inspector-title-row">
            <span
              class="inspector-dot"
              :style="{ backgroundColor: selectedNode.color }"
            />
            <span class="inspector-node-title" data-testid="inspector-node-title">
              {{ selectedNode.title }}
            </span>
            <el-tag size="small" type="danger" effect="plain">
              🔒 参数与代码只读
            </el-tag>
          </div>
          <div class="inspector-node-sub">
            节点 ID: <code>{{ selectedNode.node_id }}</code> ·
            {{ selectedNode.category_label }}
          </div>
        </div>

        <el-alert
          type="warning"
          :closable="false"
          show-icon
          class="readonly-banner"
        >
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
                  <el-tag
                    size="small"
                    :type="e.edge_type === 'backtrack' ? 'warning' : 'info'"
                  >
                    {{ e.label }}
                  </el-tag>
                  <span class="link-node-name">
                    ← {{ nodeMap.get(e.source_node_id)?.title }}
                  </span>
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
                  <el-tag
                    size="small"
                    :type="e.edge_type === 'backtrack' ? 'danger' : 'success'"
                  >
                    {{ e.label }}
                  </el-tag>
                  <span class="link-node-name">
                    → {{ nodeMap.get(e.target_node_id)?.title }}
                  </span>
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
                  <el-input
                    :model-value="selectedNode.model_provider"
                    disabled
                    size="small"
                  />
                </div>
                <div class="form-item">
                  <label>绑定模型标识</label>
                  <el-input
                    :model-value="selectedNode.model_name"
                    disabled
                    size="small"
                  />
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
                System Prompt 提示词模板
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
          <el-tab-pane
            :label="`MCP 工具 (${selectedNode.mcp_tools.length})`"
            name="tools"
          >
            <div
              v-if="selectedNode.mcp_tools.length === 0"
              class="empty-state-box"
            >
              该节点不直接调用外部 MCP 工具。
            </div>
            <div
              v-for="tool in selectedNode.mcp_tools"
              :key="tool.tool_name"
              class="mcp-tool-card"
            >
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

          <!-- Tab 3: 最近任务运行轨迹观测 -->
          <el-tab-pane
            :label="`运行轨迹 (${selectedNodeTraces.length})`"
            name="trace"
          >
            <div
              v-if="selectedNodeTraces.length === 0"
              class="empty-state-box"
            >
              当前选中任务在该节点无执行记录（或任务处于待补充事实阶段）。
            </div>
            <div
              v-for="tr in selectedNodeTraces"
              :key="tr.id"
              class="trace-step-card"
              :class="{ 'trace-backtrack': tr.is_backtrack }"
            >
              <div class="trace-step-head">
                <el-tag
                  size="small"
                  :type="tr.is_backtrack ? 'danger' : 'success'"
                >
                  步骤 #{{ tr.seq_no }} · {{ tr.phase }}
                </el-tag>
                <span class="trace-metrics">
                  耗时 {{ tr.duration_ms }} ms · {{ tr.tokens_used }} tokens
                </span>
              </div>
              <div class="trace-summary">{{ tr.business_summary }}</div>

              <div v-if="tr.is_backtrack" class="backtrack-detail-box">
                <div>
                  <strong>退回原因：</strong>{{ tr.backtrack_reason }}
                </div>
                <div style="margin-top: 4px">
                  <strong>补证结果：</strong>{{ tr.backtrack_resolution }}
                </div>
              </div>

              <div v-if="tr.tool_calls.length > 0" class="trace-tools-list">
                <div class="sub-label">调用的 MCP 工具明细：</div>
                <div
                  v-for="tc in tr.tool_calls"
                  :key="tc.call_id"
                  class="trace-tool-item"
                >
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
            </div>
          </el-tab-pane>
        </el-tabs>
      </div>
    </div>

    <!-- 导出多智能体配置 JSON DSL 弹窗 -->
    <el-dialog
      v-model="exportDialogVisible"
      title="导出多智能体编排配置 (Dify-Compatible Workflow DSL)"
      width="720px"
    >
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
      >
        导出的 JSON 配置包包含全部 7 个智能体节点的拓扑坐标、上下游连接边、MCP 工具绑定、I/O
        Schema 与只读安全策略，可用于版本归档、长久维护与跨环境迁移。
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
        <el-button
          type="primary"
          data-testid="btn-download-dsl"
          @click="downloadExportedJson"
        >
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
      <div style="margin-bottom: 12px">
        <label style="font-weight: 600; display: block; margin-bottom: 6px">
          方式一：选择本地 .json 配置文件
        </label>
        <input
          type="file"
          accept=".json,application/json"
          @change="handleImportFileChange"
        />
      </div>
      <div style="margin-bottom: 12px">
        <label style="font-weight: 600; display: block; margin-bottom: 6px">
          版本变更说明
        </label>
        <el-input
          v-model="importChangeNote"
          placeholder="填写本次导入维护说明"
        />
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
          校验拓扑并导入为新版本
        </el-button>
      </template>
    </el-dialog>

    <!-- 10 道独立测试问题评测集抽屉 -->
    <el-drawer
      v-model="benchmarkDrawerVisible"
      title="多智能体合规分析 · 10 道冻结独立评测集 (H01 - H10)"
      size="65%"
    >
      <div v-loading="loadingBenchmarks">
        <div v-if="benchmarkData" class="benchmark-summary-cards">
          <div class="bm-card">
            <div class="bm-num">{{ benchmarkData.recall_rate_pct }}%</div>
            <div class="bm-label">法条定位召回率 (目标 ≥90%)</div>
          </div>
          <div class="bm-card">
            <div class="bm-num">
              {{ benchmarkData.substring_verify_rate_pct }}%
            </div>
            <div class="bm-label">原文子串精确核验率 (100%)</div>
          </div>
          <div class="bm-card">
            <div class="bm-num">
              {{ benchmarkData.dimension_completeness_pct }}%
            </div>
            <div class="bm-label">八维度结构完整率</div>
          </div>
          <div class="bm-card">
            <div class="bm-num">{{ benchmarkData.fact_consistency_pct }}%</div>
            <div class="bm-label">事实一致性 (目标 ≥90%)</div>
          </div>
        </div>

        <el-table
          v-if="benchmarkData"
          :data="benchmarkData.cases"
          border
          stripe
          size="small"
          style="margin-top: 16px"
        >
          <el-table-column prop="question_id" label="题号" width="65" />
          <el-table-column prop="country_code" label="国家" width="65" />
          <el-table-column prop="dimension_code" label="维度" width="65" />
          <el-table-column
            prop="scenario_question"
            label="独立测试问题"
            min-width="220"
          />
          <el-table-column
            prop="matched_document_title"
            label="命中法规"
            min-width="160"
          />
          <el-table-column
            prop="matched_clause_number"
            label="命中条款"
            width="100"
          />
          <el-table-column label="子串核验" width="90">
            <template #default="{ row }">
              <el-tag :type="row.substring_verified ? 'success' : 'danger'" size="small">
                {{ row.substring_verified ? '100%匹配' : '未匹配' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column
            prop="evidence_snippet"
            label="核验原文子串片段"
            min-width="220"
          />
        </el-table>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
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
  font-size: 12px;
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
  font-size: 12px;
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
  font-size: 12px;
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
  font-size: 11px;
  font-weight: 700;
  padding: 2px 8px;
  border-radius: 999px;
}

.node-lock-tag {
  font-size: 11px;
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
  font-size: 11px;
  color: #64748b;
  margin-bottom: 8px;
}

.node-card-meta {
  background: #f8fafc;
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 11px;
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
  font-size: 11px;
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
  font-size: 12px;
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
  font-size: 12.5px;
  color: #334155;
  line-height: 1.6;
}

.link-sub {
  font-size: 12px;
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
  font-size: 12.5px;
  font-weight: 700;
  color: #1e293b;
  margin-left: 6px;
}

.link-cond {
  font-size: 11.5px;
  color: #64748b;
  margin-top: 3px;
}

.empty-link {
  font-size: 12px;
  color: #94a3b8;
}

.readonly-form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.form-item label {
  display: block;
  font-size: 11.5px;
  color: #475569;
  margin-bottom: 3px;
}

.code-readonly-box {
  background: #0f172a;
  color: #38bdf8;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 12px;
}

.io-title {
  font-size: 12px;
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
  font-size: 12px;
  color: #475569;
  margin: 0 0 8px 0;
}

.tool-schema-row {
  font-size: 11.5px;
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
  font-size: 11.5px;
  color: #475569;
  font-weight: 600;
}

.trace-summary {
  font-size: 12.5px;
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
  font-size: 12px;
  color: #9a3412;
}

.trace-tools-list {
  margin-top: 8px;
}

.sub-label {
  font-size: 11.5px;
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
  font-size: 11.5px;
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
  font-size: 11px;
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
  font-size: 12px;
  color: #475569;
  margin-top: 4px;
}
</style>
