<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import KgForceGraph from '../components/KgForceGraph.vue'

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

const maxEdges = ref(300)
const selectedId = ref('')
const showAttrCard = ref(true)

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

// 先按可见节点类型与质量体检状态过滤边
const typeFilteredEdges = computed(() => {
  const vis = new Set(visibleNodeTypes.value)
  return graph.value.edges.filter((e) => {
    if (!vis.has(e.source_node_type) || !vis.has(e.target_node_type)) return false
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
    .map((n) => ({ id: n.id, name: n.name_zh, type: n.node_type, degree: degree[n.id] }))
})

const tableEdges = computed(() =>
  selectedId.value
    ? filteredEdges.value.filter(
        (e) => e.source_node_id === selectedId.value || e.target_node_id === selectedId.value,
      )
    : filteredEdges.value,
)

const activeNode = computed<KgNode | null>(() => {
  if (selectedId.value) {
    return graph.value.nodes.find((n) => n.id === selectedId.value) || null
  }
  return (
    graph.value.nodes.find((n) => n.node_type === 'INSTRUMENT') ||
    graph.value.nodes[0] ||
    null
  )
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
      `/admin/knowledge/graph?country_code=${countryCode.value}&only_approved=${onlyApproved.value}`,
    )
    selectedId.value = ''
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function runExtract() {
  loading.value = true
  try {
    const res = await api.post<{ nodes_total: number; edges_total: number }>(
      `/admin/knowledge/extract?country_code=${countryCode.value}&translate_sample_per_doc=6`,
    )
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
  <div>
    <div class="bar">
      <div>
        <h2 style="margin: 0">六要素 · 十二类语义关系法规知识图谱</h2>
        <div class="sub">
          每条语义关系边均强制挂载证据条款原文摘录；结构包含关系（文件包含条款）独立存储，不混入语义边统计。
        </div>
      </div>
      <div style="display: flex; gap: 10px; align-items: center">
        <el-select v-model="countryCode" style="width: 140px" @change="loadGraph">
          <el-option value="SG" label="SG 新加坡" />
          <el-option value="EG" label="EG 埃及" />
          <el-option value="TN" label="TN 突尼斯" />
          <el-option value="MA" label="MA 摩洛哥" />
          <el-option value="LB" label="LB 黎巴嫩" />
        </el-select>
        <el-switch v-model="onlyApproved" active-text="仅展示已复核依据" @change="loadGraph" />
        <el-button type="primary" :loading="loading" @click="runExtract">
          重新抽取本国图谱
        </el-button>
      </div>
    </div>

    <!-- 六要素统计卡片（点击可直接切换该类节点的显示/隐藏） -->
    <div class="stat-grid">
      <div
        v-for="(meta, ntype) in NODE_TYPE_META"
        :key="ntype"
        class="stat-card"
        :class="{ 'stat-card-hidden': !isTypeVisible(ntype) }"
        :style="{ borderColor: isTypeVisible(ntype) ? meta.color : '#dcdfe6' }"
        :data-testid="`kg-stat-card-${ntype}`"
        @click="toggleNodeType(ntype)"
      >
        <div class="stat-head">
          <span class="stat-label" :style="{ color: meta.color }">
            {{ meta.label }} ({{ ntype }})
          </span>
          <el-tag
            size="small"
            :type="isTypeVisible(ntype) ? 'success' : 'info'"
            effect="plain"
          >
            {{ isTypeVisible(ntype) ? '显示中' : '已隐藏' }}
          </el-tag>
        </div>
        <div class="stat-num">{{ graph.node_counts_by_type[ntype] || 0 }}</div>
      </div>
    </div>

    <!-- 图谱与数据质量体检视图（S2 结构边与语义边分层统计 + 语义裁判核验） -->
    <div class="quality-bar" data-testid="kg-quality-bar">
      <span class="sub" style="font-weight: 700; color: #1e293b">图谱质量体检视图：</span>
      <el-tag
        :type="qualityFilter === 'all' ? 'primary' : 'info'"
        style="cursor: pointer"
        data-testid="kg-quality-all"
        @click="qualityFilter = 'all'"
      >
        活跃总边 ({{ graph.edges.length }})
      </el-tag>
      <el-tag type="primary" effect="plain">
        条款语义边: {{ graph.quality_summary?.semantic_edges ?? 0 }} 条
      </el-tag>
      <el-tag type="info" effect="plain">
        结构边(发布/监管): {{ graph.quality_summary?.structural_edges ?? 0 }} 条
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
        子串校验失配: {{ graph.quality_summary?.quote_mismatch ?? graph.quality_summary?.quote_mismatch_edges_count ?? 0 }} 条 (100% 逐字匹配)
      </el-tag>
      <el-tag type="success" effect="plain">
        未裁判语义边: {{ graph.quality_summary?.semantic_unjudged ?? 0 }} 条 | 低置信度: {{ graph.quality_summary?.low_confidence ?? 0 }} 条
      </el-tag>
      <el-tag
        v-if="(graph.quality_summary?.superseded_edges_count ?? 0) > 0"
        type="info"
        effect="plain"
      >
        已隐藏旧规则边(superseded): {{ graph.quality_summary?.superseded_edges_count }} 条
      </el-tag>
      <el-tag type="info" effect="plain">
        孤立节点: {{ graph.quality_summary?.isolated_nodes_count ?? 0 }} 个
      </el-tag>
      <el-tag type="info" effect="plain">
        未挂图谱条款: {{ graph.quality_summary?.unlinked_clauses_count ?? 0 }} /
        {{ graph.quality_summary?.total_clauses_count ?? 0 }} 条
      </el-tag>
    </div>

    <!-- 按节点类型显示/隐藏控制栏 -->
    <div class="filter-row" data-testid="kg-node-type-filter-bar">
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
        :data-testid="`kg-node-type-tag-${ntype}`"
        @click="toggleNodeType(ntype)"
      >
        {{ isTypeVisible(ntype) ? '✓ ' : '✕ ' }}{{ meta.label }} ({{
          graph.node_counts_by_type[ntype] || 0
        }})
      </el-tag>
      <el-button size="small" plain data-testid="kg-hide-hub-btn" @click="hideHubNodeTypes">
        隐藏枢纽节点（国家/监管机构）
      </el-button>
      <el-button
        v-if="visibleNodeTypes.length < ALL_NODE_TYPES.length"
        size="small"
        type="primary"
        plain
        data-testid="kg-show-all-types-btn"
        @click="showAllNodeTypes"
      >
        显示全部 6 类要素 ({{ visibleNodeTypes.length }}/6)
      </el-button>
    </div>

    <!-- 十二类关系筛选标签 -->
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
        {{ label }} ({{ visibleEdgeCountsByRelation[rtype] || 0 }})
      </el-tag>
    </div>

    <!-- ECharts 力导向知识图谱 -->
    <div class="graph-canvas">
      <div class="graph-tools">
        <span class="sub">
          图中展示 {{ fgNodes.length }} 个节点 / {{ fgEdges.length }} 条关系（合并自
          {{ filteredEdges.length }} 条可见证据边，已启用
          {{ visibleNodeTypes.length }}/6 类节点）；滚轮缩放、拖拽节点、点击节点或底部图例可筛选。绿线=已复核，灰线=待复核
        </span>
        <span class="sub">最多显示边数：</span>
        <el-select v-model="maxEdges" size="small" style="width: 90px" data-testid="kg-max-edges">
          <el-option :value="60" label="60" />
          <el-option :value="150" label="150" />
          <el-option :value="300" label="300" />
          <el-option :value="600" label="600" />
        </el-select>
        <el-button size="small" link type="primary" @click="showAttrCard = !showAttrCard">
          {{ showAttrCard ? '收起八项核心属性' : '展开八项核心属性' }}
        </el-button>
        <el-tag v-if="selectedId" closable type="warning" @close="selectedId = ''">
          已选：{{ selectedName }}
        </el-tag>
      </div>
      <KgForceGraph
        :nodes="fgNodes"
        :edges="fgEdges"
        :categories="fgCategories"
        :visible-types="visibleNodeTypes"
        :selected-id="selectedId"
        height="540px"
        @node-click="onNodeClick"
        @toggle-type="toggleNodeType"
      />
    </div>

    <!-- 八项核心法规/要素结构化属性画像（需求文档 1.3.3） -->
    <div
      v-if="showAttrCard && activeNode"
      class="attr-panel"
      data-testid="kg-eight-attributes-panel"
    >
      <div class="attr-head">
        <div>
          <el-tag size="small" effect="dark" :style="{ backgroundColor: NODE_TYPE_META[activeNode.node_type]?.color || '#409eff', borderColor: 'transparent' }">
            {{ NODE_TYPE_META[activeNode.node_type]?.label || activeNode.node_type }}
          </el-tag>
          <strong style="margin-left: 8px; font-size: 14px">{{ activeNode.name_zh }}</strong>
          <span class="sub" style="margin-left: 8px">
            （{{ selectedId ? '当前点击选中节点' : '本国代表性法规要素节点，点击图中任意节点可切换' }} · 八项必备结构化属性）
          </span>
        </div>
      </div>
      <div class="attr-grid">
        <div v-for="item in EIGHT_ATTR_LABELS" :key="item.key" class="attr-item">
          <div class="attr-key">{{ item.label }}</div>
          <div class="attr-val">{{ activeNode.attributes?.[item.key] || '-' }}</div>
        </div>
      </div>
    </div>

    <!-- 语义关系边与证据条款明细表 -->
    <el-table
      data-testid="kg-edge-table"
      :data="tableEdges"
      v-loading="loading"
      border
      stripe
      style="margin-top: 14px"
    >
      <el-table-column label="源要素节点" min-width="180">
        <template #default="{ row }">
          <el-tag size="small" effect="plain">
            {{ NODE_TYPE_META[row.source_node_type]?.label || row.source_node_type }}
          </el-tag>
          <div style="font-weight: 600; margin-top: 3px">{{ row.source_name_zh }}</div>
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
      <el-table-column label="目标要素节点" min-width="180">
        <template #default="{ row }">
          <el-tag size="small" effect="plain">
            {{ NODE_TYPE_META[row.target_node_type]?.label || row.target_node_type }}
          </el-tag>
          <div style="font-weight: 600; margin-top: 3px">{{ row.target_name_zh }}</div>
        </template>
      </el-table-column>
      <el-table-column label="挂载证据条款与原文摘录" min-width="290">
        <template #default="{ row }">
          <div class="sub" style="font-weight: 600">
            {{ row.document_title || '官方法规文本' }} · 条号 {{ row.clause_number || '-' }}
          </div>
          <div class="quote-box">{{ row.evidence_quote }}</div>
          <div v-if="row.judge_reason" class="sub" style="margin-top: 3px; color: #0f766e">
            语义裁判 ({{ row.judge_verdict || 'yes' }} · 置信度 {{ ((row.confidence ?? 0.85) * 100).toFixed(0) }}% · {{ row.extractor_model }}): {{ row.judge_reason }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="复核状态" width="150">
        <template #default="{ row }">
          <el-tag :type="row.usable_as_legal_basis ? 'success' : 'warning'" size="small">
            {{ row.usable_as_legal_basis ? '已复核(可作依据)' : '待核对(大模型抽取)' }}
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
    </el-table>
  </div>
</template>

<style scoped>
.bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.sub {
  color: #606266;
  font-size: 12px;
}
.stat-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 10px;
}
.stat-card {
  border: 1.5px solid #e4e7ed;
  border-radius: 6px;
  padding: 10px 12px;
  background: #fafafa;
  cursor: pointer;
  transition: all 0.15s ease;
  user-select: none;
}
.stat-card:hover {
  transform: translateY(-1px);
  box-shadow: 0 2px 8px rgba(15, 23, 42, 0.08);
}
.stat-card-hidden {
  opacity: 0.48;
  background: #f4f4f5;
}
.stat-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 4px;
}
.stat-label {
  font-size: 12px;
  font-weight: 600;
}
.stat-num {
  font-size: 22px;
  font-weight: 700;
  margin-top: 4px;
}
.quality-bar {
  margin: 10px 0 6px;
  padding: 8px 12px;
  background: #f8fafc;
  border: 1px dashed #cbd5e1;
  border-radius: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.filter-row {
  margin: 8px 0;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.graph-tools {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 4px 6px 8px;
}
.graph-canvas {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #f8fafc;
  padding: 6px;
}
.attr-panel {
  margin-top: 12px;
  border: 1px solid #dbeafe;
  background: #f8fafc;
  border-radius: 8px;
  padding: 10px 14px;
}
.attr-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.attr-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}
.attr-item {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 8px 10px;
}
.attr-key {
  font-size: 12px;
  font-weight: 700;
  color: #1e40af;
  margin-bottom: 3px;
}
.attr-val {
  font-size: 12px;
  color: #334155;
  line-height: 1.45;
}
.quote-box {
  font-size: 12px;
  color: #303133;
  max-height: 75px;
  overflow-y: auto;
  white-space: pre-wrap;
}
</style>
