<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import KgForceGraph from '../components/KgForceGraph.vue'
import KgLawGroups from '../components/KgLawGroups.vue'
import { groupByLaw, mergeDisplayEdges, relationPage, layoutLawOverview } from '@cyshuhe/shared/src/graphGrouping'

interface KgNode {
  id: string
  country_code: string
  node_type: string
  node_key: string
  name_zh: string
  name_orig: string | null
  summary: string | null
  attributes: Record<string, string>
  document_id: string | null
  review_status: string
}

interface KgEdge {
  id: string
  country_code: string
  source_node_id: string
  source_name_zh: string
  source_node_type: string
  target_node_id: string
  target_name_zh: string
  target_node_type: string
  relation_type: string
  evidence_clause_id: string
  evidence_quote: string
  clause_number: string | null
  document_title: string | null
  extractor_model: string
  confidence?: number
  judge_verdict?: string
  judge_reason?: string | null
  is_structural?: boolean
  review_status: string
  usable_as_legal_basis: boolean
}

interface QualitySummary {
  source_integrity_held_documents?: number
  documents_total?: number
  documents_core?: number
  documents_related?: number
  documents_out_of_scope?: number
  documents_extracted?: number
  structural_edges?: number
  semantic_edges?: number
  semantic_unjudged?: number
  low_confidence?: number
  quote_mismatch?: number
  superseded_edges_count?: number
  isolated_nodes_count?: number
  isolated_node_ids?: string[]
  total_clauses_count?: number
  linked_clauses_count?: number
  unlinked_clauses_count?: number
  pending_review_edges_count?: number
  approved_edges_count?: number
  stale_or_rejected_edges_count?: number
  quote_mismatch_edges_count?: number
  ocr_or_rtl_clauses_count?: number
}

interface GraphPayload {
  country_code: string
  node_counts_by_type: Record<string, number>
  edge_counts_by_relation: Record<string, number>
  quality_summary: QualitySummary
  nodes: KgNode[]
  edges: KgEdge[]
}

const NODE_TYPE_META: Record<string, { label: string; color: string; col: number }> = {
  COUNTRY: { label: '国家/地区', color: '#1d4ed8', col: 0 },
  REGULATOR: { label: '监管机构', color: '#0f766e', col: 1 },
  INSTRUMENT: { label: '法规文件', color: '#7c3aed', col: 2 },
  OBLIGATION: { label: '合规义务', color: '#b45309', col: 3 },
  DATA_CATEGORY: { label: '数据类型', color: '#0369a1', col: 4 },
  PENALTY: { label: '违规罚则', color: '#be123c', col: 5 },
}

const ALL_NODE_TYPES = Object.keys(NODE_TYPE_META)

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

const EIGHT_ATTR_LABELS: Array<{ key: string; label: string }> = [
  { key: 'applicable_industry', label: '1. 适用行业' },
  { key: 'data_scope', label: '2. 数据范围' },
  { key: 'cross_border_condition', label: '3. 出境条件' },
  { key: 'retention_period', label: '4. 留存期限' },
  { key: 'authorization_requirement', label: '5. 授权要求' },
  { key: 'penalty_amount', label: '6. 处罚金额' },
  { key: 'violation_level', label: '7. 违规等级' },
  { key: 'admission_checklist', label: '8. 准入材料清单' },
]

const countryCode = ref('SG')
const onlyApproved = ref(false)
const relationFilter = ref('')
const qualityFilter = ref<'all' | 'pending_review' | 'approved'>('all')
const visibleNodeTypes = ref<string[]>([...ALL_NODE_TYPES])
const loading = ref(false)
const graph = ref<GraphPayload>({
  country_code: 'SG',
  node_counts_by_type: {},
  edge_counts_by_relation: {},
  quality_summary: {},
  nodes: [],
  edges: [],
})

const selectedLawId = ref('')
const relationPageNumber = ref(1)
const evidencePageNumber = ref(1)
const selectedId = ref('')
const showAttrCard = ref(false)
const graphView = ref('graph')

function isTypeVisible(ntype: string): boolean {
  return visibleNodeTypes.value.includes(ntype)
}

function toggleNodeType(ntype: string) {
  const idx = visibleNodeTypes.value.indexOf(ntype)
  if (idx >= 0) {
    if (visibleNodeTypes.value.length <= 1) {
      ElMessage.warning('请至少保留 1 类节点显示')
      return
    }
    visibleNodeTypes.value = visibleNodeTypes.value.filter((t) => t !== ntype)
    const sel = graph.value.nodes.find((n) => n.id === selectedId.value)
    if (sel && sel.node_type === ntype) {
      selectedId.value = ''
    }
  } else {
    visibleNodeTypes.value = [...visibleNodeTypes.value, ntype]
  }
}

function showAllNodeTypes() {
  visibleNodeTypes.value = [...ALL_NODE_TYPES]
}

function hideHubNodeTypes() {
  visibleNodeTypes.value = ['INSTRUMENT', 'OBLIGATION', 'DATA_CATEGORY', 'PENALTY']
  const sel = graph.value.nodes.find((n) => n.id === selectedId.value)
  if (sel && !visibleNodeTypes.value.includes(sel.node_type)) {
    selectedId.value = ''
  }
}

// 范围外文件的法规节点: 默认折叠(仍属于全部文件,可用开关显示),它们不参与语义抽取
const showOutOfScope = ref(false)
const outOfScopeNodeIds = computed(
  () =>
    new Set(
      graph.value.nodes
        .filter((n) => n.node_type === 'INSTRUMENT' && n.attributes?.scope === 'out_of_scope')
        .map((n) => n.id),
    ),
)

// 先按可见节点类型与质量体检状态过滤边
const typeFilteredEdges = computed(() => {
  const vis = new Set(visibleNodeTypes.value)
  const hidden = showOutOfScope.value ? new Set<string>() : outOfScopeNodeIds.value
  const approvedNodes = new Set(graph.value.nodes.filter(n => n.review_status === 'approved').map(n => n.id))
  return graph.value.edges.filter((e) => {
    if (onlyApproved.value && (e.review_status !== 'approved' || !approvedNodes.has(e.source_node_id) || !approvedNodes.has(e.target_node_id))) return false
    if (!vis.has(e.source_node_type) || !vis.has(e.target_node_type)) return false
    if (hidden.has(e.source_node_id) || hidden.has(e.target_node_id)) return false
    if (qualityFilter.value === 'pending_review' && e.review_status !== 'pending_review')
      return false
    if (qualityFilter.value === 'approved' && e.review_status !== 'approved') return false
    return true
  })
})

const visibleEdgeCountsByRelation = computed(() => {
  const counts: Record<string, number> = {}
  for (const e of typeFilteredEdges.value) {
    counts[e.relation_type] = (counts[e.relation_type] || 0) + 1
  }
  return counts
})

const filteredEdges = computed(() =>
  relationFilter.value
    ? typeFilteredEdges.value.filter((e) => e.relation_type === relationFilter.value)
    : typeFilteredEdges.value,
)

const lawGroups = computed(() => groupByLaw(graph.value.nodes.filter(n => (!onlyApproved.value || n.review_status === 'approved') && (showOutOfScope.value || !outOfScopeNodeIds.value.has(n.id))), filteredEdges.value))
const selectedLaw = computed(() => lawGroups.value.find(g => g.id === selectedLawId.value))
const scopedEdges = computed(() => selectedLaw.value?.edges ?? filteredEdges.value)
const mergedEdges = computed(() => mergeDisplayEdges(graph.value.nodes, selectedLaw.value?.edges ?? []))
const displayedPage = computed(() => relationPage(mergedEdges.value, relationPageNumber.value, 36))
const graphEdgesRaw = computed(() => displayedPage.value.edges)
const displayedEvidenceCount = computed(() => graphEdgesRaw.value.reduce((sum, e) => sum + e.count, 0))
const overviewEdges = computed(() => mergeDisplayEdges(graph.value.nodes, filteredEdges.value))
const overviewNodes = computed(() => layoutLawOverview(
  graph.value.nodes.filter(n => visibleNodeTypes.value.includes(n.node_type)), lawGroups.value, overviewEdges.value,
))
const overviewFgEdges = computed(() => overviewEdges.value.map(e => ({
  source: e.source, target: e.target,
  label: (RELATION_LABEL[e.relation] || e.relation) + (e.count > 1 ? ` ×${e.count}` : ''), approved: e.approved,
})))
function onOverviewNodeClick(id: string) {
  const group = lawGroups.value.find(g => g.instrumentIds.includes(id))
  if (group) selectLaw(group.id)
  else onNodeClick(id)
}

function selectLaw(id: string) {
  selectedLawId.value = id
  relationPageNumber.value = 1
  selectedId.value = ''
}
function selectRelationPage(page: number) {
  relationPageNumber.value = page
  selectedId.value = ''
}
watch(filteredEdges, () => { relationPageNumber.value = 1; selectedId.value = '' })
watch(() => graph.value.country_code, () => selectLaw(''))

const fgCategories = Object.entries(NODE_TYPE_META).map(([type, m]) => ({
  type,
  name: m.label,
  color: m.color,
}))

const fgEdges = computed(() =>
  graphEdgesRaw.value.map((e) => ({
    source: e.source,
    target: e.target,
    label: (RELATION_LABEL[e.relation] || e.relation) + (e.count > 1 ? ` ×${e.count}` : ''),
    approved: e.approved,
  })),
)

const fgNodes = computed(() => {
  const vis = new Set(visibleNodeTypes.value)
  const degree: Record<string, number> = {}
  for (const e of graphEdgesRaw.value) {
    degree[e.source] = (degree[e.source] || 0) + 1
    degree[e.target] = (degree[e.target] || 0) + 1
  }
  return graph.value.nodes
    .filter((n) => vis.has(n.node_type) && degree[n.id])
    .map((n) => ({
      id: n.id,
      name: n.name_zh,
      type: n.node_type,
      degree: degree[n.id],
    }))
})

const tableEdges = computed(() =>
  selectedId.value
    ? scopedEdges.value.filter(
        (e) => e.source_node_id === selectedId.value || e.target_node_id === selectedId.value,
      )
    : scopedEdges.value,
)
const evidencePage = computed(() => relationPage(tableEdges.value, evidencePageNumber.value, 50))
watch(tableEdges, () => { evidencePageNumber.value = 1 })


const activeNode = computed<KgNode | null>(() => {
  if (selectedId.value) {
    return graph.value.nodes.find((n) => n.id === selectedId.value) || null
  }
  return graph.value.nodes.find((n) => selectedLaw.value?.instrumentIds.includes(n.id))
    || graph.value.nodes.find((n) => n.node_type === 'INSTRUMENT') || graph.value.nodes[0] || null
})

const selectedName = computed(
  () => graph.value.nodes.find((n) => n.id === selectedId.value)?.name_zh || '',
)

function onNodeClick(id: string) {
  selectedId.value = selectedId.value === id ? '' : id
  showAttrCard.value = true
}

async function loadGraph() {
  loading.value = true
  try {
    graph.value = await api.get<GraphPayload>(
      `/admin/knowledge/graph?country_code=${countryCode.value}&only_approved=false`,
    )
    selectLaw('')
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function runExtract() {
  loading.value = true
  try {
    const res = await api.post<{
      status: string
      reason?: string
      nodes_total: number
      edges_total: number
    }>(`/admin/knowledge/extract?country_code=${countryCode.value}`)
    if (res.status !== 'completed') {
      ElMessage.warning(`图谱未替换，保留旧结果：${res.reason || res.status}`)
      return
    }
    ElMessage.success(
      `已重跑 ${countryCode.value} 知识图谱抽取：共 ${res.nodes_total} 个节点、${res.edges_total} 条语义边`,
    )
    await loadGraph()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function reviewEdge(edge: KgEdge, decision: 'approved' | 'rejected') {
  try {
    await api.post(`/admin/knowledge/edges/${edge.id}/review`, { decision })
    ElMessage.success(decision === 'approved' ? '已复核通过该语义边' : '已驳回该语义边')
    await loadGraph()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  }
}

onMounted(loadGraph)
</script>
<template>
  <div class="knowledge-graph-page">
    <header class="page-heading">
      <div>
        <div class="eyebrow">LEGAL KNOWLEDGE</div>
        <h1>法规知识图谱</h1>
        <p>探索法规、义务与数据之间的关系，沿每一条关联追溯原文依据。</p>
      </div>
      <div class="page-actions">
        <el-select
          v-model="countryCode"
          style="width: 168px"
          aria-label="图谱国家"
          @change="loadGraph"
          ><el-option value="SG" label="新加坡 · SG" /><el-option
            value="EG"
            label="埃及 · EG" /><el-option value="TN" label="突尼斯 · TN" /><el-option
            value="MA"
            label="摩洛哥 · MA" /><el-option value="LB" label="黎巴嫩 · LB" /></el-select
        ><el-popover trigger="click" placement="bottom-end" :width="300"
          ><template #reference><el-button>图谱管理</el-button></template>
          <div class="secondary-actions">
            <p class="page-description">
              重新抽取会更新本国图谱，并调用模型。已有复核状态仍以实际记录为准。
            </p>
            <el-button :loading="loading" @click="runExtract">重新抽取本国图谱</el-button>
          </div></el-popover
        >
      </div>
    </header>
    <el-alert
      v-if="graph.quality_summary.source_integrity_held_documents"
      type="error"
      :closable="false"
      show-icon
      :title="`${graph.quality_summary.source_integrity_held_documents} 份资料存在来源绑定异常，已从分析依据隔离。原文与审批仍保留，可在法规文档中核查。`"
      style="margin-bottom: 16px"
    />
    <div class="stat-grid" aria-label="按要素类型筛选">
      <button
        v-for="(meta, ntype) in NODE_TYPE_META"
        :key="ntype"
        class="stat-card"
        :class="{ 'stat-card-hidden': !isTypeVisible(ntype) }"
        :aria-pressed="isTypeVisible(ntype)"
        :data-testid="`kg-stat-card-${ntype}`"
        @click="toggleNodeType(ntype)"
      >
        <span class="stat-label"><i :style="{ background: meta.color }" />{{ meta.label }}</span
        ><strong class="stat-num">{{ graph.node_counts_by_type[ntype] || 0 }}</strong
        ><span class="stat-state">{{
          isTypeVisible(ntype) ? '点击隐藏' : '已隐藏 · 点击显示'
        }}</span>
      </button>
    </div>
    <div class="surface graph-surface">
      <div class="graph-filter-toolbar">
        <el-select
          v-model="relationFilter"
          placeholder="全部关系"
          aria-label="关系类型"
          style="width: 190px"
          ><el-option value="" :label="`全部关系 (${typeFilteredEdges.length})`" /><el-option
            v-for="(label, rtype) in RELATION_LABEL"
            :key="rtype"
            :value="rtype"
            :label="`${label} (${visibleEdgeCountsByRelation[rtype] || 0})`"
        /></el-select>
        <el-select v-model="qualityFilter" aria-label="复核状态" style="width: 168px"
          ><el-option value="all" label="全部复核状态" /><el-option
            value="pending_review"
            :label="`待核对 (${graph.quality_summary?.pending_review_edges_count ?? 0})`" /><el-option
            value="approved"
            :label="`已复核 (${graph.quality_summary?.approved_edges_count ?? 0})`"
        /></el-select>
        <el-popover trigger="click" placement="bottom-end" :width="310"
          ><template #reference><el-button>显示设置</el-button></template>
          <div class="display-settings">
            <el-switch
              aria-label="仅展示已复核依据"
              v-model="onlyApproved"
              active-text="仅展示已复核依据"
            /><el-switch
              aria-label="显示范围外文件"
              v-model="showOutOfScope"
              active-text="显示范围外文件"
              data-testid="kg-show-out-of-scope"
            /><el-button data-testid="kg-hide-hub-btn" @click="hideHubNodeTypes"
              >隐藏国家与监管枢纽</el-button
            ><el-button data-testid="kg-show-all-types-btn" @click="showAllNodeTypes"
              >显示全部要素</el-button
            >
          </div></el-popover
        >
        <div v-if="selectedLaw" class="selected-law-context"><span>当前法规：{{ selectedLaw.name }}</span>
          <el-button link @click="selectLaw('')">清除法规筛选</el-button></div>
        <el-tag v-if="selectedId" closable type="info" @close="selectedId = ''">{{
          selectedName
        }}</el-tag>
      </div>
      <el-tabs v-model="graphView"
        ><el-tab-pane label="关系图谱" name="graph"
          ><KgLawGroups :key="countryCode"
            v-loading="loading"
            :groups="lawGroups" :selected-group-id="selectedLawId"
            :total="graph.edges.length" :filtered="filteredEdges.length"
            :shown="displayedEvidenceCount" :shown-groups="fgEdges.length"
            :overview-shown="filteredEdges.length" :overview-groups="overviewFgEdges.length"
            :page="displayedPage.page" :pages="displayedPage.pages"
            @select="selectLaw" @page="selectRelationPage"
          >
            <template #overview>
              <KgForceGraph :nodes="overviewNodes" :edges="overviewFgEdges" :categories="fgCategories"
                :visible-types="visibleNodeTypes" :selected-id="selectedId" overview height="620px"
                @node-click="onOverviewNodeClick" @toggle-type="toggleNodeType" />
              <div class="graph-caption">{{ overviewNodes.length }} 个节点 · {{ lawGroups.length }} 个法规分组（含其他关联） · 全部筛选后关系已绘制</div>
            </template>
            <KgForceGraph :nodes="fgNodes" :edges="fgEdges" :categories="fgCategories"
              :visible-types="visibleNodeTypes" :selected-id="selectedId" height="480px"
              @node-click="onNodeClick" @toggle-type="toggleNodeType" />
            <div class="graph-caption"><span>{{ fgNodes.length }} 个节点 · 点击节点查看属性与关联证据</span>
              <el-button link @click="showAttrCard = true">查看节点详情</el-button></div>
          </KgLawGroups></el-tab-pane
        ><el-tab-pane :label="`证据明细 (${tableEdges.length})`" name="evidence">
          <el-table
            v-if="graphView === 'evidence'"
            data-testid="kg-edge-table"
            :data="evidencePage.edges"
            v-loading="loading"
            border
            stripe
            style="margin-top: 14px"
          >
            <el-table-column label="来源要素" min-width="180">
              <template #default="{ row }">
                <el-tag size="small" effect="plain">
                  {{ NODE_TYPE_META[row.source_node_type]?.label || row.source_node_type }}
                </el-tag>
                <div style="font-weight: 600; margin-top: 3px">
                  {{ row.source_name_zh }}
                </div>
              </template>
            </el-table-column>
            <el-table-column label="语义关系" width="145">
              <template #default="{ row }">
                <el-tag type="primary" size="small">
                  {{ RELATION_LABEL[row.relation_type] || row.relation_type }}
                </el-tag>
                <div class="sub">{{ row.relation_type }}</div>
              </template>
            </el-table-column>
            <el-table-column label="关联要素" min-width="180">
              <template #default="{ row }">
                <el-tag size="small" effect="plain">
                  {{ NODE_TYPE_META[row.target_node_type]?.label || row.target_node_type }}
                </el-tag>
                <div style="font-weight: 600; margin-top: 3px">
                  {{ row.target_name_zh }}
                </div>
              </template>
            </el-table-column>
            <el-table-column label="证据条款与原文" min-width="320">
              <template #default="{ row }">
                <div class="sub" style="font-weight: 600">
                  {{ row.document_title || '官方法规文本' }} · 条号
                  {{ row.clause_number || '-' }}
                </div>
                <div class="quote-box">{{ row.evidence_quote }}</div>
                <div v-if="row.judge_reason" class="sub" style="margin-top: 3px; color: #0f766e">
                  {{ row.is_structural ? '结构来源登记' : '语义裁判' }} ({{
                    row.judge_verdict || '未裁判'
                  }}
                  · 置信度 {{ ((row.confidence ?? 0.85) * 100).toFixed(0) }}% ·
                  {{ row.extractor_model }}):
                  {{ row.judge_reason }}
                </div>
              </template>
            </el-table-column>
            <el-table-column label="复核状态" width="150">
              <template #default="{ row }">
                <el-tag :type="row.usable_as_legal_basis ? 'success' : 'warning'" size="small">
                  {{
                    row.is_structural
                      ? '结构登记（非法律审批）'
                      : row.usable_as_legal_basis
                        ? '已复核(可作依据)'
                        : '待核对(大模型抽取)'
                  }}
                </el-tag>
                <div class="sub" style="margin-top: 2px">
                  {{ row.is_structural ? '结构边' : '语义边' }}
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="140" fixed="right">
              <template #default="{ row }">
                <el-button
                  v-if="!row.usable_as_legal_basis"
                  size="small"
                  type="success"
                  link
                  @click="reviewEdge(row, 'approved')"
                >
                  通过
                </el-button>
                <el-button size="small" type="danger" link @click="reviewEdge(row, 'rejected')">
                  驳回
                </el-button>
              </template>
            </el-table-column>
          </el-table><div class="graph-caption" aria-label="证据分页">
            <el-button :disabled="evidencePage.page <= 1" @click="evidencePageNumber--">上一页证据</el-button>
            <span>第 {{ evidencePage.page }} / {{ evidencePage.pages }} 页 · 共 {{ tableEdges.length }} 条证据，每页 50 条</span>
            <el-button :disabled="evidencePage.page >= evidencePage.pages" @click="evidencePageNumber++">下一页证据</el-button>
          </div></el-tab-pane
        ></el-tabs
      >
    </div>
    <details class="ui-disclosure">
      <summary>
        数据质量与覆盖情况
        <span
          >待核对 {{ graph.quality_summary?.pending_review_edges_count ?? 0 }} · 已复核
          {{ graph.quality_summary?.approved_edges_count ?? 0 }}</span
        >
      </summary>
      <div class="disclosure-body">
        <div class="quality-bar" data-testid="kg-quality-bar">
          <el-tag
            :type="qualityFilter === 'all' ? 'primary' : 'info'"
            style="cursor: pointer"
            data-testid="kg-quality-all"
            @click="qualityFilter = 'all'"
          >
            活跃总边 ({{ graph.edges.length }})
          </el-tag>
          <el-tag
            v-if="(graph.quality_summary?.documents_total ?? 0) > 0"
            type="success"
            effect="plain"
            data-testid="kg-doc-scope"
          >
            法规文件 {{ graph.quality_summary?.documents_total }} 份(核心
            {{ graph.quality_summary?.documents_core ?? 0 }} / 相关
            {{ graph.quality_summary?.documents_related ?? 0 }} / 范围外
            {{ graph.quality_summary?.documents_out_of_scope ?? 0 }}),其中
            {{ graph.quality_summary?.documents_extracted ?? 0 }} 份参与语义抽取
          </el-tag>
          <el-tag type="primary" effect="plain">
            条款语义边: {{ graph.quality_summary?.semantic_edges ?? 0 }} 条
          </el-tag>
          <el-tag type="info" effect="plain">
            结构边(发布/监管):
            {{ graph.quality_summary?.structural_edges ?? 0 }} 条
          </el-tag>
          <el-tag
            :type="qualityFilter === 'pending_review' ? 'warning' : 'info'"
            style="cursor: pointer"
            data-testid="kg-quality-pending"
            @click="qualityFilter = qualityFilter === 'pending_review' ? 'all' : 'pending_review'"
          >
            待核对 ({{ graph.quality_summary?.pending_review_edges_count ?? 0 }})
          </el-tag>
          <el-tag
            :type="qualityFilter === 'approved' ? 'success' : 'info'"
            style="cursor: pointer"
            data-testid="kg-quality-approved"
            @click="qualityFilter = qualityFilter === 'approved' ? 'all' : 'approved'"
          >
            已复核 ({{ graph.quality_summary?.approved_edges_count ?? 0 }})
          </el-tag>
          <el-tag type="success" effect="plain">
            子串校验失配:
            {{
              graph.quality_summary?.quote_mismatch ??
              graph.quality_summary?.quote_mismatch_edges_count ??
              0
            }}
            条
          </el-tag>
          <el-tag type="success" effect="plain">
            未裁判语义边: {{ graph.quality_summary?.semantic_unjudged ?? 0 }} 条 | 低置信度:
            {{ graph.quality_summary?.low_confidence ?? 0 }} 条
          </el-tag>
          <el-tag
            v-if="(graph.quality_summary?.superseded_edges_count ?? 0) > 0"
            type="info"
            effect="plain"
          >
            已隐藏旧规则边(superseded):
            {{ graph.quality_summary?.superseded_edges_count }} 条
          </el-tag>
          <el-tag type="info" effect="plain">
            孤立节点: {{ graph.quality_summary?.isolated_nodes_count ?? 0 }} 个
          </el-tag>
          <el-tag type="info" effect="plain">
            未挂图谱条款:
            {{ graph.quality_summary?.unlinked_clauses_count ?? 0 }} /
            {{ graph.quality_summary?.total_clauses_count ?? 0 }} 条
          </el-tag>
        </div>
      </div>
    </details>
    <el-drawer v-model="showAttrCard" title="节点详情" size="520px"
      ><template v-if="activeNode"
        ><el-tag effect="plain">{{
          NODE_TYPE_META[activeNode.node_type]?.label || activeNode.node_type
        }}</el-tag>
        <h3 class="node-detail-title">{{ activeNode.name_zh }}</h3>
        <p class="page-description">
          {{ selectedId ? '当前选中节点' : '当前国家的代表性法规节点' }} · 结构化属性
        </p>
        <div class="attr-grid" data-testid="kg-eight-attributes-panel">
          <div v-for="item in EIGHT_ATTR_LABELS" :key="item.key" class="attr-item">
            <div class="attr-key">{{ item.label }}</div>
            <div class="attr-val">
              {{ activeNode.attributes?.[item.key] || '暂无登记信息' }}
            </div>
          </div>
        </div></template
      ><el-empty v-else description="请先在图谱中选择一个节点"
    /></el-drawer>
  </div>
</template>
<style scoped>
.stat-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 24px;
}
.stat-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  padding: 20px 18px;
  background: white;
  border: 1px solid var(--cy-line);
  border-radius: 11px;
  text-align: left;
  transition:
    border-color 0.18s,
    transform 0.18s;
}
.stat-card:hover {
  border-color: #bdcdee;
  transform: translateY(-2px);
}
.stat-card-hidden {
  background: #f0f3f6;
  color: #9aa7b4;
}
.stat-label {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #74828e;
  font-size: 13px;
}
.stat-label i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}
.stat-num {
  font-size: 28px;
  font-weight: 600;
  line-height: 1.4;
  margin: 8px 0 3px;
  color: #273b4a;
}
.stat-state {
  font-size: 11px;
  color: #a0acb5;
}
.graph-surface {
  padding: 22px 24px 10px;
}
.graph-filter-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.graph-canvas {
  border-radius: 10px;
  background: #f8fafc;
  border: 1px solid #edf0f4;
  overflow: hidden;
}
.selected-law-context { flex-basis: 100%; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; color: #526174; overflow-wrap: anywhere; }
.graph-caption {
  display: flex;
  gap: 18px;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  font-size: 12px;
  color: #81919d;
  padding: 12px 0 4px;
}
.display-settings {
  display: grid;
  gap: 14px;
  padding: 4px;
}
.display-settings label {
  display: flex;
  gap: 14px;
  align-items: center;
  font-size: 13px;
  color: #6a7b89;
}
.display-settings label .el-select {
  width: 105px;
}
.quality-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  padding-top: 8px;
}
.quality-bar .el-tag {
  font-size: 12px;
  line-height: 1.7;
}
.node-detail-title {
  font-size: 20px;
  margin: 18px 0 12px;
  overflow-wrap: anywhere;
}
.attr-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-top: 24px;
}
.attr-item {
  padding: 16px;
  background: #f7f9fc;
  border-radius: 8px;
}
.attr-key {
  color: #70828f;
  font-size: 13px;
  margin-bottom: 8px;
}
.attr-val {
  font-size: 14px;
  line-height: 1.9;
  white-space: pre-line;
  overflow-wrap: anywhere;
}
.quote-box {
  font-size: 14px;
  line-height: 1.8;
  max-height: 140px;
  overflow-y: auto;
  white-space: pre-wrap;
  margin-top: 10px;
}
@media (max-width: 1100px) {
  .stat-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .stat-state {
    display: none;
  }
  .stat-card {
    padding: 14px 16px;
  }
  .stat-num {
    font-size: 24px;
  }
}
@media (max-width: 600px) {
  .stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .graph-surface {
    padding: 16px 12px 6px;
  }
  .attr-grid {
    grid-template-columns: 1fr;
  }
  .graph-caption {
    gap: 8px;
  }
}
</style>
