<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import KgForceGraph from '../components/KgForceGraph.vue'

interface CountryOverview {
  country_code: string
  country_name_zh: string
  country_name_orig: string
  documents_count: number
  clauses_count: number
  translations_count: number
  approved_translations_count: number
  kg_nodes_count: number
  kg_edges_count: number
}

interface ClauseTranslation {
  id: string
  clause_id: string
  country_code: string
  document_title: string
  clause_number: string | null
  heading: string | null
  original_text: string
  translated_text: string
  glossary_hits: Array<{
    source_term: string
    target_zh: string
    priority_level: number
    statutory_definition?: string | null
    source_reference?: string | null
  }>
  unmatched_terms: Array<{
    source_term: string
    provisional_zh: string
    statutory_definition: string
    source_reference: string
  }>
  usable_as_legal_basis: boolean
  status_badge: string
}

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
  extractor_model?: string
  confidence?: number
  judge_verdict?: string
  judge_reason?: string | null
  is_structural?: boolean
  review_status?: string
  usable_as_legal_basis: boolean
}

interface GraphPayload {
  country_code: string
  node_counts_by_type: Record<string, number>
  edge_counts_by_relation: Record<string, number>
  quality_summary?: Record<string, number>
  nodes: KgNode[]
  edges: KgEdge[]
}

interface RegulationSummary {
  id: string
  country_code: string
  title: string
  document_type: string
  document_type_zh: string
  language: string
  source_url: string
  publisher: string
  version_no: number
  legal_status: string
  fetched_at: string | null
  sha256_hex: string | null
  clauses_count: number
  translated_clauses_count: number
  edges_count: number
  eight_attributes: Record<string, string>
  version_count: number
}

interface RegulationClauseItem {
  clause_id: string
  ordinal: number
  clause_number: string | null
  heading: string | null
  section_path: string | null
  original_text: string
  translated_text: string | null
  status_badge: string
  usable_as_legal_basis: boolean
  glossary_hits: Array<{
    source_term: string
    target_zh: string
    priority_level: number
    statutory_definition?: string | null
    source_reference?: string | null
  }>
  unmatched_terms: Array<{
    source_term: string
    provisional_zh: string
    statutory_definition: string
    source_reference: string
  }>
  linked_edges: Array<{
    edge_id: string
    relation_type: string
    source_name_zh: string
    source_node_type: string
    target_name_zh: string
    target_node_type: string
    evidence_quote: string
    usable_as_legal_basis: boolean
  }>
}

interface RegulationDetail {
  regulation: RegulationSummary
  versions: Array<{
    version_no: number
    legal_status: string
    fetched_at: string | null
    content_hash: string
    text_source: string
    note: string
  }>
  clauses: RegulationClauseItem[]
  mini_graph: GraphPayload
}

const NODE_TYPE_META: Record<string, { label: string; color: string }> = {
  COUNTRY: { label: '国家/地区', color: '#1d4ed8' },
  REGULATOR: { label: '监管机构', color: '#0f766e' },
  INSTRUMENT: { label: '法规文件', color: '#7c3aed' },
  OBLIGATION: { label: '合规义务', color: '#b45309' },
  DATA_CATEGORY: { label: '数据类型', color: '#0369a1' },
  PENALTY: { label: '违规罚则', color: '#be123c' },
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

const overviews = ref<CountryOverview[]>([])
const selectedCountry = ref('SG')
const keyword = ref('')
const onlyApproved = ref(false)
const activeTab = ref('center')
const clauses = ref<ClauseTranslation[]>([])
const regulations = ref<RegulationSummary[]>([])
const regDetail = ref<RegulationDetail | null>(null)
const regLoading = ref(false)
const showMiniGraph = ref(true)
const showVersions = ref(false)
const highlightedQuotes = ref<Record<string, string>>({})

const graph = ref<GraphPayload>({
  country_code: 'SG',
  node_counts_by_type: {},
  edge_counts_by_relation: {},
  nodes: [],
  edges: [],
})
const loading = ref(false)

// 合规地图筛选状态
const visibleNodeTypes = ref<string[]>([...ALL_NODE_TYPES])
const relationFilter = ref('')
const maxEdges = ref(300)
const selectedNodeId = ref('')

const fgCategories = Object.entries(NODE_TYPE_META).map(([type, m]) => ({
  type,
  name: m.label,
  color: m.color,
}))

// 按「法律层级」分组左侧目录树
const regulationsByTier = computed(() => {
  const groups: Record<string, RegulationSummary[]> = {}
  for (const r of regulations.value) {
    ;(groups[r.document_type_zh] ||= []).push(r)
  }
  return groups
})

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
    const sel = graph.value.nodes.find((n) => n.id === selectedNodeId.value)
    if (sel && sel.node_type === ntype) selectedNodeId.value = ''
  } else {
    visibleNodeTypes.value = [...visibleNodeTypes.value, ntype]
  }
}

function hideHubNodeTypes() {
  visibleNodeTypes.value = ['INSTRUMENT', 'OBLIGATION', 'DATA_CATEGORY', 'PENALTY']
}

function showAllNodeTypes() {
  visibleNodeTypes.value = [...ALL_NODE_TYPES]
}

const typeFilteredEdges = computed(() => {
  const vis = new Set(visibleNodeTypes.value)
  return graph.value.edges.filter(
    (e) =>
      vis.has(e.source_node_type) &&
      vis.has(e.target_node_type) &&
      (!e.review_status || e.review_status === 'approved' || e.review_status === 'pending_review'),
  )
})

const filteredEdges = computed(() =>
  relationFilter.value
    ? typeFilteredEdges.value.filter((e) => e.relation_type === relationFilter.value)
    : typeFilteredEdges.value,
)

interface MergedEdge {
  source: string
  target: string
  relation: string
  count: number
  approved: boolean
}

const mergedEdges = computed<MergedEdge[]>(() => {
  const map = new Map<string, MergedEdge>()
  for (const e of filteredEdges.value) {
    const key = `${e.source_node_id}|${e.target_node_id}|${e.relation_type}`
    const cur = map.get(key)
    if (cur) {
      cur.count += 1
      cur.approved = cur.approved && e.usable_as_legal_basis
    } else {
      map.set(key, {
        source: e.source_node_id,
        target: e.target_node_id,
        relation: e.relation_type,
        count: 1,
        approved: e.usable_as_legal_basis,
      })
    }
  }
  return [...map.values()]
})

const graphEdgesRaw = computed(() => {
  const edges = mergedEdges.value
  if (edges.length <= maxEdges.value) return edges
  const degree: Record<string, number> = {}
  for (const e of edges) {
    degree[e.source] = (degree[e.source] || 0) + 1
    degree[e.target] = (degree[e.target] || 0) + 1
  }
  return edges
    .slice()
    .sort((a, b) => degree[b.source] + degree[b.target] - (degree[a.source] + degree[a.target]))
    .slice(0, maxEdges.value)
})

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
  return (graph.value.nodes || [])
    .filter((n) => vis.has(n.node_type) && degree[n.id])
    .map((n) => ({ id: n.id, name: n.name_zh, type: n.node_type, degree: degree[n.id] }))
})

const tableEdges = computed(() =>
  selectedNodeId.value
    ? filteredEdges.value.filter(
        (e) => e.source_node_id === selectedNodeId.value || e.target_node_id === selectedNodeId.value,
      )
    : filteredEdges.value,
)

// 单部法规的 1 跳关联图数据
const miniFgEdges = computed(() => {
  const mg = regDetail.value?.mini_graph
  if (!mg) return []
  const map = new Map<string, MergedEdge>()
  for (const e of mg.edges) {
    const key = `${e.source_node_id}|${e.target_node_id}|${e.relation_type}`
    const cur = map.get(key)
    if (cur) {
      cur.count += 1
    } else {
      map.set(key, {
        source: e.source_node_id,
        target: e.target_node_id,
        relation: e.relation_type,
        count: 1,
        approved: e.usable_as_legal_basis,
      })
    }
  }
  return [...map.values()].map((e) => ({
    source: e.source,
    target: e.target,
    label: (RELATION_LABEL[e.relation] || e.relation) + (e.count > 1 ? ` ×${e.count}` : ''),
    approved: e.approved,
  }))
})

const miniFgNodes = computed(() => {
  const mg = regDetail.value?.mini_graph
  if (!mg) return []
  const degree: Record<string, number> = {}
  for (const e of miniFgEdges.value) {
    degree[e.source] = (degree[e.source] || 0) + 1
    degree[e.target] = (degree[e.target] || 0) + 1
  }
  return (mg.nodes || []).map((n) => ({
    id: n.id,
    name: n.name_zh,
    type: n.node_type,
    degree: degree[n.id] || 1,
  }))
})

function toggleHighlightQuote(clauseId: string, quote: string) {
  if (highlightedQuotes.value[clauseId] === quote) {
    const next = { ...highlightedQuotes.value }
    delete next[clauseId]
    highlightedQuotes.value = next
  } else {
    highlightedQuotes.value = { ...highlightedQuotes.value, [clauseId]: quote }
  }
}

function splitByHighlight(text: string, quote?: string): Array<{ text: string; mark: boolean }> {
  if (!quote || !text.includes(quote)) {
    return [{ text, mark: false }]
  }
  const idx = text.indexOf(quote)
  return [
    { text: text.slice(0, idx), mark: false },
    { text: quote, mark: true },
    { text: text.slice(idx + quote.length), mark: false },
  ]
}

async function selectRegulation(docId: string) {
  regLoading.value = true
  try {
    regDetail.value = await api.get<RegulationDetail>(`/knowledge/regulations/${docId}`)
    highlightedQuotes.value = {}
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  } finally {
    regLoading.value = false
  }
}

async function loadData() {
  loading.value = true
  try {
    const q = new URLSearchParams()
    q.set('country_code', selectedCountry.value)
    q.set('only_approved', String(onlyApproved.value))
    if (keyword.value.trim()) q.set('keyword', keyword.value.trim())

    const regQ = new URLSearchParams()
    regQ.set('country_code', selectedCountry.value)
    if (keyword.value.trim()) regQ.set('keyword', keyword.value.trim())

    const [ov, cl, gr, regs] = await Promise.all([
      api.get<CountryOverview[]>('/knowledge/overview'),
      api.get<ClauseTranslation[]>(`/knowledge/clauses?${q.toString()}`),
      api.get<GraphPayload>(
        `/knowledge/graph?country_code=${selectedCountry.value}&only_approved=${onlyApproved.value}`,
      ),
      api.get<RegulationSummary[]>(`/knowledge/regulations?${regQ.toString()}`),
    ])
    overviews.value = ov
    clauses.value = cl
    graph.value = gr
    regulations.value = regs
    if (regs.length > 0) {
      const preferred =
        regs.find((r) => r.edges_count > 2 && r.translated_clauses_count > 0) || regs[0]
      await selectRegulation(preferred.id)
    } else {
      regDetail.value = null
    }
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function selectCountry(cc: string) {
  selectedCountry.value = cc
  selectedNodeId.value = ''
  loadData()
}

function onGraphNodeClick(id: string) {
  selectedNodeId.value = selectedNodeId.value === id ? '' : id
}

onMounted(loadData)
</script>

<template>
  <div>
    <h2 style="margin-top: 0">五国官方法规知识库与合规关系地图</h2>
    <p class="sub">
      提示：只有经合作律所确认、标记为「已复核(可作法律依据)」的条款与关系，才会被安全评估报告与跨境申请表作为正式法律依据引用；「机器翻译，待核对」仅供检索参考。
    </p>

    <!-- 五国概览卡片 -->
    <div class="country-grid">
      <div
        v-for="c in overviews"
        :key="c.country_code"
        class="country-card"
        :class="{ active: selectedCountry === c.country_code }"
        @click="selectCountry(c.country_code)"
      >
        <div style="font-weight: 700; font-size: 16px">
          {{ c.country_name_zh }} ({{ c.country_code }})
        </div>
        <div class="sub">{{ c.country_name_orig }}</div>
        <div style="margin-top: 8px; font-size: 12px">
          官方法规 <strong>{{ c.documents_count }}</strong> 份 · 条款
          <strong>{{ c.clauses_count }}</strong> 条
        </div>
        <div style="font-size: 12px; margin-top: 2px">
          受控译文 <strong>{{ c.translations_count }}</strong> 条（已复核
          <strong>{{ c.approved_translations_count }}</strong>） · 图谱边
          <strong>{{ c.kg_edges_count }}</strong> 条
        </div>
      </div>
    </div>

    <!-- 检索栏 -->
    <div style="margin: 16px 0; display: flex; gap: 10px; align-items: center">
      <el-input
        v-model="keyword"
        style="width: 340px"
        clearable
        placeholder="输入英文/中文关键词检索法规与条款（如 transfer、autonomous、个人数据）"
        @keyup.enter="loadData"
      />
      <el-switch v-model="onlyApproved" active-text="只看律所已复核依据" @change="loadData" />
      <el-button type="primary" :loading="loading" @click="loadData">检索</el-button>
    </div>

    <el-tabs v-model="activeTab">
      <!-- 页签 1: 法规中心 + 法规详情（左右双栏对照 + 8项属性 + 1跳关联图 + 点击义务高亮原文） -->
      <el-tab-pane
        name="center"
        :label="`法规中心 · 分级目录与双栏对照 (${regulations.length} 份法规)`"
      >
        <div class="center-layout" v-loading="loading">
          <!-- 左侧三级分类树：国家 -> 法律层级 -> 法规 -->
          <div class="reg-sidebar" data-testid="user-reg-tree">
            <div class="sidebar-title">
              {{ selectedCountry }} 法规分类目录 ({{ regulations.length }})
            </div>
            <div v-for="(list, tierName) in regulationsByTier" :key="tierName" class="tier-group">
              <div class="tier-header">{{ tierName }} ({{ list.length }})</div>
              <div
                v-for="r in list"
                :key="r.id"
                class="reg-item"
                :class="{ active: regDetail?.regulation.id === r.id }"
                :data-testid="`user-reg-item-${r.id}`"
                @click="selectRegulation(r.id)"
              >
                <div class="reg-item-title">{{ r.title }}</div>
                <div class="reg-item-meta">
                  <span>{{ r.publisher }}</span>
                  <span>v{{ r.version_no }} · {{ r.clauses_count }} 条款 · {{ r.edges_count }} 关系</span>
                </div>
              </div>
            </div>
          </div>

          <!-- 右侧法规详情面板（页面 2 要求：基本信息、8项核心属性、1跳关联面板、左右双栏原文/中译对照与高亮） -->
          <div class="reg-detail" v-loading="regLoading" data-testid="user-reg-detail">
            <template v-if="regDetail">
              <div class="detail-header">
                <div>
                  <h3 style="margin: 0 0 6px 0">{{ regDetail.regulation.title }}</h3>
                  <div class="sub" style="display: flex; flex-wrap: wrap; gap: 12px; align-items: center">
                    <span>发布机构：<strong>{{ regDetail.regulation.publisher }}</strong></span>
                    <span>层级：<strong>{{ regDetail.regulation.document_type_zh }}</strong></span>
                    <span>当前版本：<strong>v{{ regDetail.regulation.version_no }} ({{ regDetail.regulation.legal_status }})</strong></span>
                    <span>条款数：<strong>{{ regDetail.regulation.clauses_count }} 条</strong></span>
                    <a
                      :href="regDetail.regulation.source_url"
                      target="_blank"
                      rel="noopener noreferrer"
                      style="color: #2563eb; font-weight: 600"
                    >
                      打开官方来源链接 ↗
                    </a>
                  </div>
                </div>
                <div style="display: flex; gap: 8px">
                  <el-button size="small" plain @click="showMiniGraph = !showMiniGraph">
                    {{ showMiniGraph ? '收起本法规关联图谱' : '展开本法规关联图谱' }}
                  </el-button>
                  <el-button size="small" plain @click="showVersions = !showVersions">
                    版本历史 ({{ regDetail.versions.length }})
                  </el-button>
                </div>
              </div>

              <!-- 版本历史折叠面板 -->
              <div v-if="showVersions" class="version-box">
                <div style="font-weight: 600; font-size: 12px; margin-bottom: 6px">
                  法规版本归档记录（官方渠道仅提供现行版本时如实登记）
                </div>
                <div v-for="v in regDetail.versions" :key="v.version_no" class="sub" style="margin-bottom: 4px">
                  版本 v{{ v.version_no }} · 状态: {{ v.legal_status }} · 解析来源: {{ v.text_source }} ·
                  SHA-256: <code>{{ v.content_hash.slice(0, 16) }}…</code> · {{ v.note }}
                </div>
              </div>

              <!-- 八项必备核心结构化属性（需求文档 1.3.3） -->
              <div class="eight-attr-box" data-testid="user-eight-attributes">
                <div style="font-weight: 700; font-size: 13px; color: #1e3a8a; margin-bottom: 6px">
                  八项合规核心属性画像（点击下方条款中的义务标签可在原文中高亮依据片段）
                </div>
                <div class="eight-attr-grid">
                  <div v-for="item in EIGHT_ATTR_LABELS" :key="item.key" class="eight-attr-cell">
                    <div class="eight-attr-k">{{ item.label }}</div>
                    <div class="eight-attr-v">
                      {{ regDetail.regulation.eight_attributes?.[item.key] || '-' }}
                    </div>
                  </div>
                </div>
              </div>

              <!-- 单法规 1 跳关联关系面板（复用 ECharts 力导向图组件） -->
              <div v-if="activeTab === 'center' && showMiniGraph && miniFgNodes.length" class="mini-graph-box">
                <div class="sub" style="padding: 4px 8px; font-weight: 600">
                  本法规一层关联关系面板（{{ miniFgNodes.length }} 个关联要素 / {{ miniFgEdges.length }} 条关系）
                </div>
                <KgForceGraph
                  :nodes="miniFgNodes"
                  :edges="miniFgEdges"
                  :categories="fgCategories"
                  height="280px"
                />
              </div>

              <!-- 条款原文与受控中文左右双栏逐条对照 -->
              <div class="dual-header">
                <div>官方原文（点击右侧义务/关系芯片可高亮原文依据片段）</div>
                <div>中文译文（机器翻译，待核对） · 抽取出的合规义务、数据类型与罚则</div>
              </div>
              <div class="dual-list">
                <div
                  v-for="cl in regDetail.clauses"
                  :key="cl.clause_id"
                  class="dual-row"
                >
                  <!-- 左栏：官方原文（支持高亮依据片段） -->
                  <div class="dual-col orig-col">
                    <div class="clause-meta">
                      <el-tag size="small" effect="plain">条号 {{ cl.clause_number || cl.ordinal }}</el-tag>
                      <span v-if="cl.heading" style="font-weight: 600; margin-left: 6px">{{ cl.heading }}</span>
                      <span v-if="cl.section_path" class="sub" style="margin-left: 6px">[{{ cl.section_path }}]</span>
                    </div>
                    <div class="clause-text">
                      <template
                        v-for="(seg, idx) in splitByHighlight(cl.original_text, highlightedQuotes[cl.clause_id])"
                        :key="idx"
                      >
                        <mark v-if="seg.mark" class="quote-highlight">{{ seg.text }}</mark>
                        <span v-else>{{ seg.text }}</span>
                      </template>
                    </div>
                  </div>

                  <!-- 右栏：中文对照 + 术语悬停 + 关联义务/数据类型芯片 -->
                  <div class="dual-col zh-col">
                    <div class="clause-meta" style="display: flex; justify-content: space-between">
                      <el-tag
                        :type="cl.usable_as_legal_basis ? 'success' : cl.translated_text ? 'warning' : 'info'"
                        size="small"
                      >
                        {{
                          cl.usable_as_legal_basis
                            ? '中文译文（已复核，可作法律依据）'
                            : cl.translated_text
                              ? '中文译文（机器翻译，待核对）'
                              : '未翻译'
                        }}
                      </el-tag>
                      <span v-if="cl.linked_edges.length" class="sub">
                        点击下方关联要素芯片高亮左侧原文依据
                      </span>
                    </div>
                    <div class="clause-text" :style="{ color: cl.translated_text ? '#1e293b' : '#94a3b8' }">
                      {{ cl.translated_text || '暂无中文译文' }}
                    </div>

                    <!-- 术语命中与悬停法定定义 -->
                    <div
                      v-if="cl.glossary_hits?.length || cl.unmatched_terms?.length"
                      style="margin-top: 6px; display: flex; flex-wrap: wrap; gap: 4px"
                    >
                      <el-tooltip
                        v-for="h in cl.glossary_hits"
                        :key="h.source_term"
                        placement="top"
                        :content="`法定定义: ${h.statutory_definition || '-'} (依据: ${h.source_reference || '-'})`"
                      >
                        <el-tag size="small" type="success">
                          术语: {{ h.source_term }}（{{ h.target_zh }}）
                        </el-tag>
                      </el-tooltip>
                      <el-tooltip
                        v-for="u in cl.unmatched_terms"
                        :key="u.source_term"
                        placement="top"
                        :content="`解释: ${u.statutory_definition} (${u.source_reference})`"
                      >
                        <el-tag size="small" type="warning">
                          术语解释: {{ u.source_term }}（{{ u.provisional_zh }}）
                        </el-tag>
                      </el-tooltip>
                    </div>

                    <!-- 抽取出的合规义务、数据类型、罚则芯片（点击高亮左栏原文依据片段） -->
                    <div
                      v-if="cl.linked_edges.length"
                      style="margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px"
                    >
                      <el-tag
                        v-for="le in cl.linked_edges"
                        :key="le.edge_id"
                        size="small"
                        :effect="highlightedQuotes[cl.clause_id] === le.evidence_quote ? 'dark' : 'plain'"
                        type="primary"
                        style="cursor: pointer"
                        data-testid="clause-obligation-chip"
                        @click="toggleHighlightQuote(cl.clause_id, le.evidence_quote)"
                      >
                        🔍 {{ RELATION_LABEL[le.relation_type] || le.relation_type }} →
                        {{ le.target_name_zh }}
                      </el-tag>
                    </div>
                  </div>
                </div>
              </div>
            </template>
          </div>
        </div>
      </el-tab-pane>

      <!-- 页签 2: 六要素 · 十二关系合规图谱（ECharts 合规地图 + 按节点类型显隐） -->
      <el-tab-pane
        name="graph"
        :label="`六要素 · 十二关系合规图谱 (${graph.edges.length})`"
      >
        <!-- 按节点类型显示/隐藏控制栏 -->
        <div class="filter-row" data-testid="user-kg-node-type-bar">
          <span class="sub" style="font-weight: 600">按节点类型显示/隐藏：</span>
          <el-tag
            v-for="(meta, ntype) in NODE_TYPE_META"
            :key="ntype"
            :effect="isTypeVisible(ntype) ? 'dark' : 'plain'"
            :style="{
              cursor: 'pointer',
              backgroundColor: isTypeVisible(ntype) ? meta.color : '#f4f4f5',
              borderColor: isTypeVisible(ntype) ? meta.color : '#d4d7de',
              color: isTypeVisible(ntype) ? '#ffffff' : '#909399',
              textDecoration: isTypeVisible(ntype) ? 'none' : 'line-through',
            }"
            @click="toggleNodeType(ntype)"
          >
            {{ isTypeVisible(ntype) ? '✓ ' : '✕ ' }}{{ meta.label }} ({{
              graph.node_counts_by_type?.[ntype] || 0
            }})
          </el-tag>
          <el-button size="small" plain data-testid="user-kg-hide-hub-btn" @click="hideHubNodeTypes">
            隐藏枢纽节点（国家/监管机构）
          </el-button>
          <el-button
            v-if="visibleNodeTypes.length < ALL_NODE_TYPES.length"
            size="small"
            type="primary"
            plain
            @click="showAllNodeTypes"
          >
            显示全部 6 类要素 ({{ visibleNodeTypes.length }}/6)
          </el-button>
        </div>

        <!-- 十二类语义关系筛选 -->
        <div class="filter-row">
          <span class="sub" style="font-weight: 600">十二类语义关系筛选：</span>
          <el-tag
            :type="relationFilter === '' ? 'primary' : 'info'"
            style="cursor: pointer"
            @click="relationFilter = ''"
          >
            全部 ({{ typeFilteredEdges.length }})
          </el-tag>
          <el-tag
            v-for="(label, rtype) in RELATION_LABEL"
            :key="rtype"
            :type="relationFilter === rtype ? 'primary' : 'info'"
            style="cursor: pointer"
            @click="relationFilter = rtype"
          >
            {{ label }} ({{ graph.edge_counts_by_relation?.[rtype] || 0 }})
          </el-tag>
        </div>

        <!-- ECharts 合规地图画布 -->
        <div class="graph-canvas">
          <div class="sub" style="padding: 4px 8px">
            合规地图展示 {{ fgNodes.length }} 个节点 / {{ fgEdges.length }} 条关系（合并自
            {{ filteredEdges.length }} 条可见证据边，仅展示「已复核」与明确标注「待核对」的合规边；结构边
            {{ graph.quality_summary?.structural_edges ?? 0 }} 条，大模型语义边
            {{ graph.quality_summary?.semantic_edges ?? 0 }} 条）；点击节点可筛选下方逐字证据明细。
          </div>
          <KgForceGraph
            v-if="activeTab === 'graph'"
            :nodes="fgNodes"
            :edges="fgEdges"
            :categories="fgCategories"
            :visible-types="visibleNodeTypes"
            :selected-id="selectedNodeId"
            height="500px"
            @node-click="onGraphNodeClick"
            @toggle-type="toggleNodeType"
          />
        </div>

        <el-table :data="tableEdges" v-loading="loading" border stripe style="margin-top: 12px">
          <el-table-column prop="source_name_zh" label="源要素" min-width="180" />
          <el-table-column label="关系类型" width="150">
            <template #default="{ row }">
              <el-tag size="small" type="primary">
                {{ RELATION_LABEL[row.relation_type] || row.relation_type }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="target_name_zh" label="目标要素" min-width="180" />
          <el-table-column label="证据条款与逐字原文摘录" min-width="280">
            <template #default="{ row }">
              <div class="sub" style="font-weight: 600">
                {{ row.document_title || '官方法规' }} · 条号 {{ row.clause_number || '-' }}
              </div>
              <div>{{ row.evidence_quote }}</div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="160">
            <template #default="{ row }">
              <el-tag :type="row.usable_as_legal_basis ? 'success' : 'warning'" size="small">
                {{ row.usable_as_legal_basis ? '已复核(可作依据)' : '待核对(大模型抽取)' }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- 页签 3: 法规条款跨文件检索 -->
      <el-tab-pane
        name="clauses"
        :label="`法规条款与受控中译 (${clauses.length})`"
      >
        <el-table :data="clauses" v-loading="loading" border stripe>
          <el-table-column prop="document_title" label="法规文件" width="230" />
          <el-table-column prop="clause_number" label="条号" width="90" />
          <el-table-column label="官方原文片段" min-width="260">
            <template #default="{ row }">
              <div class="text-box">{{ row.original_text }}</div>
            </template>
          </el-table-column>
          <el-table-column label="中文译文（机器翻译，待核对）" min-width="300">
            <template #default="{ row }">
              <div class="text-box">{{ row.translated_text || '暂无中文译文' }}</div>
              <div
                v-if="row.glossary_hits?.length || row.unmatched_terms?.length"
                style="margin-top: 6px; display: flex; flex-wrap: wrap; gap: 4px"
              >
                <el-tooltip
                  v-for="h in row.glossary_hits"
                  :key="h.source_term"
                  placement="top"
                  :content="`法定定义: ${h.statutory_definition || '-'} (依据: ${h.source_reference || '-'})`"
                >
                  <el-tag size="small" type="success">
                    {{ h.source_term }}（{{ h.target_zh }}）
                  </el-tag>
                </el-tooltip>
                <el-tooltip
                  v-for="u in row.unmatched_terms"
                  :key="u.source_term"
                  placement="top"
                  :content="`解释: ${u.statutory_definition} (${u.source_reference})`"
                >
                  <el-tag size="small" type="warning">
                    {{ u.source_term }}（{{ u.provisional_zh }}）
                  </el-tag>
                </el-tooltip>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="效力状态" width="165">
            <template #default="{ row }">
              <el-tag :type="row.usable_as_legal_basis ? 'success' : 'warning'" size="small">
                {{ row.status_badge }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.sub {
  color: #606266;
  font-size: 12px;
}
.country-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 10px;
}
.country-card {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 12px;
  cursor: pointer;
  background: #fafafa;
}
.country-card.active {
  border-color: #409eff;
  background: #ecf5ff;
}
.center-layout {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 14px;
  align-items: start;
}
.reg-sidebar {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #fafafa;
  max-height: 780px;
  overflow-y: auto;
  padding: 10px;
}
.sidebar-title {
  font-weight: 700;
  font-size: 14px;
  margin-bottom: 8px;
  color: #1e293b;
}
.tier-group {
  margin-bottom: 10px;
}
.tier-header {
  font-size: 12px;
  font-weight: 700;
  color: #475569;
  background: #e2e8f0;
  padding: 4px 8px;
  border-radius: 4px;
  margin-bottom: 6px;
}
.reg-item {
  padding: 8px;
  border-radius: 6px;
  border: 1px solid transparent;
  cursor: pointer;
  margin-bottom: 4px;
  background: #ffffff;
}
.reg-item:hover {
  border-color: #93c5fd;
}
.reg-item.active {
  border-color: #2563eb;
  background: #eff6ff;
}
.reg-item-title {
  font-size: 12px;
  font-weight: 600;
  color: #1e293b;
  line-height: 1.35;
}
.reg-item-meta {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: #64748b;
  margin-top: 4px;
}
.reg-detail {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 14px;
  background: #ffffff;
}
.detail-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  border-bottom: 1px solid #f1f5f9;
  padding-bottom: 10px;
}
.version-box {
  margin-top: 10px;
  padding: 8px 12px;
  background: #f8fafc;
  border: 1px dashed #cbd5e1;
  border-radius: 6px;
}
.eight-attr-box {
  margin-top: 10px;
  padding: 10px 12px;
  background: #f8fafc;
  border: 1px solid #dbeafe;
  border-radius: 8px;
}
.eight-attr-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}
.eight-attr-cell {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 6px 8px;
}
.eight-attr-k {
  font-size: 11px;
  font-weight: 700;
  color: #1d4ed8;
}
.eight-attr-v {
  font-size: 12px;
  color: #334155;
  margin-top: 2px;
  line-height: 1.4;
}
.mini-graph-box {
  margin-top: 10px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  background: #f8fafc;
  padding: 4px;
}
.dual-header {
  margin-top: 12px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  font-weight: 700;
  font-size: 13px;
  padding: 8px 10px;
  background: #f1f5f9;
  border-radius: 6px;
}
.dual-list {
  max-height: 520px;
  overflow-y: auto;
  margin-top: 6px;
}
.dual-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  border-bottom: 1px solid #e2e8f0;
  padding: 10px 6px;
}
.dual-col {
  font-size: 13px;
  line-height: 1.55;
}
.clause-meta {
  margin-bottom: 6px;
  font-size: 12px;
}
.clause-text {
  white-space: pre-wrap;
  color: #1e293b;
}
.quote-highlight {
  background-color: #fef08a;
  color: #854d0e;
  font-weight: 600;
  padding: 1px 3px;
  border-radius: 3px;
}
.filter-row {
  margin: 8px 0;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.graph-canvas {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #f8fafc;
  padding: 6px;
}
.text-box {
  max-height: 110px;
  overflow-y: auto;
  white-space: pre-wrap;
  font-size: 13px;
}
</style>
