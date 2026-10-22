<script setup lang="ts">
import {
  DOC_TYPE_LABEL,
  LEGAL_STATUS_LABEL,
  PARSE_STRATEGY_LABEL,
  VERSION_STATE_LABEL,
  type ClauseOut,
  type DocumentDetail,
  type DocumentOut,
  type EventOut,
} from '@cyshuhe/shared'
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'

const route = useRoute()
const router = useRouter()

const docs = ref<DocumentOut[]>([])
const country = ref((route.query.country_code as string) || '')
const siteId = ref((route.query.site_id as string) || '')
const sitePublisher = ref((route.query.publisher as string) || '')
const loading = ref(false)
const fmt = (s: string | null) => (s ? new Date(s).toLocaleString('zh-CN') : '—')

async function load() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    if (country.value.trim()) params.set('country_code', country.value.trim())
    if (siteId.value.trim()) params.set('site_id', siteId.value.trim())
    params.set('limit', '200')
    const q = params.toString() ? `?${params.toString()}` : ''
    docs.value = await api.get<DocumentOut[]>(`/admin/documents${q}`)
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function clearSiteFilter() {
  siteId.value = ''
  sitePublisher.value = ''
  router.replace({ query: country.value.trim() ? { country_code: country.value.trim() } : {} })
  load()
}

const drawer = ref(false)
const detail = ref<DocumentDetail | null>(null)
const versionId = ref<string>('')
const clauses = ref<ClauseOut[]>([])
const events = ref<EventOut[]>([])

async function open(doc: DocumentOut) {
  detail.value = await api.get<DocumentDetail>(`/admin/documents/${doc.id}`)
  drawer.value = true
  if (detail.value.versions.length) await pick(detail.value.versions[0].id)
}

async function pick(id: string) {
  versionId.value = id
  ;[clauses.value, events.value] = await Promise.all([
    api.get<ClauseOut[]>(`/admin/versions/${id}/clauses?limit=200`),
    api.get<EventOut[]>(`/admin/versions/${id}/events`),
  ])
}

onMounted(load)
</script>

<template>
  <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px">
    <div style="display: flex; align-items: center; gap: 10px">
      <h3 style="margin: 0">法规文档库</h3>
      <el-tag v-if="siteId" closable type="info" @close="clearSiteFilter">
        来源站点：{{ sitePublisher || siteId.slice(0, 8) }}
      </el-tag>
    </div>
    <div style="display: flex; gap: 8px">
      <el-input v-model="country" placeholder="国家代码,如 SG" style="width: 160px" clearable @keyup.enter="load" />
      <el-button @click="load">查询</el-button>
    </div>
  </div>
  <el-table :data="docs" v-loading="loading" border @row-click="open" style="cursor: pointer">
    <el-table-column prop="country_code" label="国家" width="70" />
    <el-table-column prop="title" label="标题" min-width="260" show-overflow-tooltip />
    <el-table-column label="原文地址" min-width="220" show-overflow-tooltip>
      <template #default="{ row }">
        <span style="color: #606266; font-size: 12px">{{ row.source_url }}</span>
      </template>
    </el-table-column>
    <el-table-column label="类型" width="100">
      <template #default="{ row }">{{ DOC_TYPE_LABEL[row.document_type] }}</template>
    </el-table-column>
    <el-table-column label="最新版本" width="90">
      <template #default="{ row }">{{ row.latest_version ? `第 ${row.latest_version.version_no} 版` : '—' }}</template>
    </el-table-column>
    <el-table-column label="状态" width="150">
      <template #default="{ row }">
        <el-tag
          v-if="row.latest_version"
          :type="row.latest_version.state === 'parsed' ? 'success' : row.latest_version.state === 'failed' ? 'danger' : 'warning'"
        >
          {{ VERSION_STATE_LABEL[row.latest_version.state] }}
        </el-tag>
        <el-tag v-else type="info">未取得原文</el-tag>
      </template>
    </el-table-column>
    <el-table-column label="最近检查" width="170">
      <template #default="{ row }">{{ fmt(row.last_checked_at) }}</template>
    </el-table-column>
    <el-table-column prop="last_error" label="最近错误" min-width="140" show-overflow-tooltip />
  </el-table>

  <el-drawer v-model="drawer" :title="detail?.title" size="880px">
    <template v-if="detail">
      <p style="word-break: break-all; color: #909399">原文地址:{{ detail.source_url }}</p>
      <el-radio-group :model-value="versionId" @change="(v: string | number | boolean | undefined) => pick(String(v))" style="margin-bottom: 12px">
        <el-radio-button v-for="v in detail.versions" :key="v.id" :value="v.id">
          第 {{ v.version_no }} 版 · {{ fmt(v.fetched_at) }}
        </el-radio-button>
      </el-radio-group>

      <template v-for="v in detail.versions.filter((x) => x.id === versionId)" :key="v.id">
        <el-descriptions :column="2" border size="small" style="margin-bottom: 12px">
          <el-descriptions-item label="状态">{{ VERSION_STATE_LABEL[v.state] }}</el-descriptions-item>
          <el-descriptions-item label="条款切分">
            {{ v.parse_strategy ? PARSE_STRATEGY_LABEL[v.parse_strategy] : '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="现行状态">{{ LEGAL_STATUS_LABEL[v.legal_status] }}</el-descriptions-item>
          <el-descriptions-item label="官方版本说明">{{ v.version_label ?? '—' }}</el-descriptions-item>
          <el-descriptions-item label="发布日期">{{ v.published_date ?? '—' }}</el-descriptions-item>
          <el-descriptions-item label="生效日期">{{ v.effective_date ?? '—' }}</el-descriptions-item>
          <el-descriptions-item label="页数">{{ v.page_count ?? '—' }}</el-descriptions-item>
          <el-descriptions-item label="文本字数">{{ v.char_count ?? '—' }}</el-descriptions-item>
          <el-descriptions-item label="内容指纹(SHA-256)" :span="2">
            <code style="word-break: break-all">{{ v.content_hash }}</code>
          </el-descriptions-item>
          <el-descriptions-item v-if="v.error" label="说明" :span="2">{{ v.error }}</el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="v.text_source === 'ocr'"
          type="error"
          :closable="false"
          style="margin-bottom: 12px"
          :title="`本版本的文字是 OCR 机器识别结果(平均置信度 ${v.ocr_confidence ?? '—'}),可能有错字、错号,必须人工对照原文核对。`"
        />
        <el-alert
          v-if="v.legal_status === 'unknown'"
          type="info"
          :closable="false"
          style="margin-bottom: 12px"
          title="该文件的现行有效状态尚未核实,请勿直接当作现行法律使用。"
        />
        <el-alert
          v-if="v.parse_strategy && v.parse_strategy !== 'numbered'"
          type="warning"
          :closable="false"
          style="margin-bottom: 12px"
          title="未识别到条款编号,已按段落切分。条款定位可能不准,请对照原文核对。"
        />
      </template>

      <h4>条款(共 {{ clauses.length }} 条,原文逐字保留)</h4>
      <el-table :data="clauses" border size="small" max-height="420">
        <el-table-column prop="clause_number" label="编号" width="80" />
        <el-table-column label="所在章节" width="220">
          <template #default="{ row }">
            <div style="font-size: 12px; color: #909399">{{ row.section_path ?? '' }}</div>
            <div v-if="row.heading" style="font-weight: 600">{{ row.heading }}</div>
          </template>
        </el-table-column>
        <el-table-column label="页码" width="80">
          <template #default="{ row }">
            {{ row.page_start === row.page_end ? row.page_start : `${row.page_start}–${row.page_end}` }}
          </template>
        </el-table-column>
        <el-table-column label="原文">
          <template #default="{ row }"><div style="white-space: pre-wrap">{{ row.original_text }}</div></template>
        </el-table-column>
      </el-table>

      <h4>处理过程</h4>
      <el-timeline>
        <el-timeline-item
          v-for="(e, i) in events"
          :key="i"
          :type="e.status === 'ok' ? 'success' : 'danger'"
          :timestamp="fmt(e.created_at)"
        >
          {{ e.step === 'download' ? '下载归档' : e.step === 'parse' ? '解析条款' : e.step }}:{{ e.message }}
        </el-timeline-item>
      </el-timeline>
    </template>
  </el-drawer>
</template>
