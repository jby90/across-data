<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

interface KgNode {
  id: string
  country_code: string
  node_type: string
  node_key: string
  name_zh: string
  name_orig: string | null
  summary: string | null
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
  review_status: string
  usable_as_legal_basis: boolean
}

interface GraphPayload {
  country_code: string
  node_counts_by_type: Record<string, number>
  edge_counts_by_relation: Record<string, number>
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

const countryCode = ref('SG')
const onlyApproved = ref(false)
const relationFilter = ref('')
const loading = ref(false)
const graph = ref<GraphPayload>({
  country_code: 'SG',
  node_counts_by_type: {},
  edge_counts_by_relation: {},
  nodes: [],
  edges: [],
})

const filteredEdges = computed(() =>
  relationFilter.value
    ? graph.value.edges.filter((e) => e.relation_type === relationFilter.value)
    : graph.value.edges,
)

// 将六要素节点按列排布在交互式 SVG 画布中，直观呈现「国家 -> 监管机构 -> 法规 -> 义务 -> 数据类型 -> 罚则」拓扑
const layoutNodes = computed(() => {
  const byType: Record<string, KgNode[]> = {}
  for (const n of graph.value.nodes) {
    ;(byType[n.node_type] ||= []).push(n)
  }
  const positioned: Array<KgNode & { x: number; y: number; color: string }> = []
  for (const [ntype, meta] of Object.entries(NODE_TYPE_META)) {
    const list = (byType[ntype] || []).slice(0, 10)
    const x = 95 + meta.col * 175
    list.forEach((n, idx) => {
      const y = 65 + idx * (320 / Math.max(1, list.length))
      positioned.push({ ...n, x, y, color: meta.color })
    })
  }
  return positioned
})

const posById = computed(() => {
  const map: Record<string, { x: number; y: number }> = {}
  for (const n of layoutNodes.value) {
    map[n.id] = { x: n.x, y: n.y }
  }
  return map
})

const layoutEdges = computed(() => {
  const out: Array<KgEdge & { x1: number; y1: number; x2: number; y2: number }> = []
  for (const e of filteredEdges.value.slice(0, 60)) {
    const p1 = posById.value[e.source_node_id]
    const p2 = posById.value[e.target_node_id]
    if (p1 && p2) {
      out.push({ ...e, x1: p1.x, y1: p1.y, x2: p2.x, y2: p2.y })
    }
  }
  return out
})

async function loadGraph() {
  loading.value = true
  try {
    graph.value = await api.get<GraphPayload>(
      `/api/admin/knowledge/graph?country_code=${countryCode.value}&only_approved=${onlyApproved.value}`,
    )
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
      `/api/admin/knowledge/extract?country_code=${countryCode.value}&translate_sample_per_doc=6`,
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
    await api.post(`/api/admin/knowledge/edges/${edge.id}/review`, { decision })
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
          每条语义关系边均強制挂载证据条款原文摘录；结构包含关系（文件包含条款）独立存储，不混入语义边统计。
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

    <!-- 六要素统计卡片 -->
    <div class="stat-grid">
      <div v-for="(meta, ntype) in NODE_TYPE_META" :key="ntype" class="stat-card">
        <div class="stat-label" :style="{ color: meta.color }">{{ meta.label }} ({{ ntype }})</div>
        <div class="stat-num">{{ graph.node_counts_by_type[ntype] || 0 }}</div>
      </div>
    </div>

    <!-- 十二类关系筛选标签 -->
    <div style="margin: 12px 0; display: flex; flex-wrap: wrap; gap: 6px; align-items: center">
      <span class="sub">十二类语义关系筛选：</span>
      <el-tag
        :type="relationFilter === '' ? 'primary' : 'info'"
        style="cursor: pointer"
        @click="relationFilter = ''"
      >
        全部 ({{ graph.edges.length }})
      </el-tag>
      <el-tag
        v-for="(label, rtype) in RELATION_LABEL"
        :key="rtype"
        :type="relationFilter === rtype ? 'primary' : 'info'"
        style="cursor: pointer"
        @click="relationFilter = rtype"
      >
        {{ label }} ({{ graph.edge_counts_by_relation[rtype] || 0 }})
      </el-tag>
    </div>

    <!-- 交互式六要素分层拓扑图 -->
    <div class="graph-canvas">
      <svg viewBox="0 0 1080 390" style="width: 100%; height: 370px">
        <g v-for="(meta, ntype) in NODE_TYPE_META" :key="ntype">
          <text
            :x="95 + meta.col * 175"
            y="24"
            text-anchor="middle"
            font-size="12"
            font-weight="600"
            :fill="meta.color"
          >
            {{ meta.label }}
          </text>
        </g>
        <g v-for="e in layoutEdges" :key="e.id">
          <line
            :x1="e.x1"
            :y1="e.y1"
            :x2="e.x2"
            :y2="e.y2"
            :stroke="e.usable_as_legal_basis ? '#16a34a' : '#94a3b8'"
            :stroke-dasharray="e.usable_as_legal_basis ? 'none' : '4 3'"
            stroke-width="1.5"
            opacity="0.7"
          />
        </g>
        <g v-for="n in layoutNodes" :key="n.id">
          <circle :cx="n.x" :cy="n.y" r="10" :fill="n.color" />
          <text :x="n.x" :y="n.y + 22" text-anchor="middle" font-size="11" fill="#1e293b">
            {{ n.name_zh.length > 14 ? n.name_zh.slice(0, 14) + '…' : n.name_zh }}
          </text>
        </g>
      </svg>
    </div>

    <!-- 语义关系边与证据条款明细表 -->
    <el-table :data="filteredEdges" v-loading="loading" border stripe style="margin-top: 14px">
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
        </template>
      </el-table-column>
      <el-table-column label="复核状态" width="150">
        <template #default="{ row }">
          <el-tag :type="row.usable_as_legal_basis ? 'success' : 'warning'" size="small">
            {{ row.usable_as_legal_basis ? '已复核(可作依据)' : '待律所复核' }}
          </el-tag>
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
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 10px 12px;
  background: #fafafa;
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
.graph-canvas {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #f8fafc;
  padding: 6px;
}
.quote-box {
  font-size: 12px;
  color: #303133;
  max-height: 75px;
  overflow-y: auto;
  white-space: pre-wrap;
}
</style>
