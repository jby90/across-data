<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { api } from '../api'

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

interface KgEdge {
  id: string
  source_name_zh: string
  source_node_type: string
  target_name_zh: string
  target_node_type: string
  relation_type: string
  evidence_quote: string
  clause_number: string | null
  document_title: string | null
  usable_as_legal_basis: boolean
}

interface GraphPayload {
  country_code: string
  node_counts_by_type: Record<string, number>
  edge_counts_by_relation: Record<string, number>
  edges: KgEdge[]
}

const overviews = ref<CountryOverview[]>([])
const selectedCountry = ref('SG')
const keyword = ref('')
const onlyApproved = ref(false)
const clauses = ref<ClauseTranslation[]>([])
const graph = ref<GraphPayload>({
  country_code: 'SG',
  node_counts_by_type: {},
  edge_counts_by_relation: {},
  edges: [],
})
const loading = ref(false)

async function loadData() {
  loading.value = true
  try {
    const q = new URLSearchParams()
    q.set('country_code', selectedCountry.value)
    q.set('only_approved', String(onlyApproved.value))
    if (keyword.value.trim()) q.set('keyword', keyword.value.trim())

    const [ov, cl, gr] = await Promise.all([
      api.get<CountryOverview[]>('/api/knowledge/overview'),
      api.get<ClauseTranslation[]>(`/api/knowledge/clauses?${q.toString()}`),
      api.get<GraphPayload>(
        `/api/knowledge/graph?country_code=${selectedCountry.value}&only_approved=${onlyApproved.value}`,
      ),
    ])
    overviews.value = ov
    clauses.value = cl
    graph.value = gr
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function selectCountry(cc: string) {
  selectedCountry.value = cc
  loadData()
}

onMounted(loadData)
</script>

<template>
  <div>
    <h2 style="margin-top: 0">五国官方法规知识库与合规关系图谱</h2>
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
        style="width: 320px"
        clearable
        placeholder="输入英文/中文关键词检索条款（如 transfer、autonomous、个人数据）"
        @keyup.enter="loadData"
      />
      <el-switch v-model="onlyApproved" active-text="只看律所已复核依据" @change="loadData" />
      <el-button type="primary" :loading="loading" @click="loadData">检索</el-button>
    </div>

    <el-tabs>
      <el-tab-pane :label="`法规条款与受控中译 (${clauses.length})`">
        <el-table :data="clauses" v-loading="loading" border stripe>
          <el-table-column prop="document_title" label="法规文件" width="230" />
          <el-table-column prop="clause_number" label="条号" width="90" />
          <el-table-column label="官方原文片段" min-width="260">
            <template #default="{ row }">
              <div class="text-box">{{ row.original_text }}</div>
            </template>
          </el-table-column>
          <el-table-column label="受术语表约束中译 (悬停查看法定定义)" min-width="300">
            <template #default="{ row }">
              <div class="text-box">{{ row.translated_text }}</div>
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

      <el-tab-pane :label="`六要素 · 十二关系合规图谱 (${graph.edges.length})`">
        <el-table :data="graph.edges" v-loading="loading" border stripe>
          <el-table-column prop="source_name_zh" label="源要素" min-width="180" />
          <el-table-column prop="relation_type" label="关系类型" width="160" />
          <el-table-column prop="target_name_zh" label="目标要素" min-width="180" />
          <el-table-column prop="evidence_quote" label="证据条款原文摘录" min-width="280" />
          <el-table-column label="状态" width="150">
            <template #default="{ row }">
              <el-tag :type="row.usable_as_legal_basis ? 'success' : 'warning'" size="small">
                {{ row.usable_as_legal_basis ? '已复核(可作依据)' : '待律所核对' }}
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
.text-box {
  max-height: 110px;
  overflow-y: auto;
  white-space: pre-wrap;
  font-size: 13px;
}
</style>
