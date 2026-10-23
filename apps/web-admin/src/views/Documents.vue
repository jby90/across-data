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
  type SiteOut,
} from '@cyshuhe/shared'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'

const route = useRoute()
const router = useRouter()

const docs = ref<DocumentOut[]>([])
const sites = ref<SiteOut[]>([])
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

const RELEVANCE_META: Record<
  string,
  { label: string; type: 'success' | 'primary' | 'info' | 'warning' }
> = {
  relevant: { label: '核心(参与抽取)', type: 'success' },
  related: { label: '相关(参与抽取)', type: 'primary' },
  out_of_scope: { label: '范围外(仅归档/不抽取)', type: 'info' },
  irrelevant: { label: '非法规/待过滤', type: 'info' },
  needs_review: { label: '待判定', type: 'warning' },
}

async function overrideRelevance(doc: DocumentOut, relevance: string) {
  try {
    const updated = await api.patch<DocumentOut>(`/admin/documents/${doc.id}/relevance`, {
      relevance,
      note: '管理员人工覆盖相关性判定',
    })
    doc.relevance = updated.relevance
    doc.relevance_note = updated.relevance_note
    doc.relevance_source = updated.relevance_source
    ElMessage.success('已调整相关性,下次重新抽取图谱时生效(人工判定不会被自动流程改写)')
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

function clearSiteFilter() {
  siteId.value = ''
  sitePublisher.value = ''
  router.replace({
    query: country.value.trim() ? { country_code: country.value.trim() } : {},
  })
  load()
}

const drawer = ref(false)
const detail = ref<DocumentDetail | null>(null)
const versionId = ref<string>('')
const clauses = ref<ClauseOut[]>([])
const events = ref<EventOut[]>([])
const translatingDocId = ref<string>('')

async function downloadOriginal() {
  if (!versionId.value) return
  try {
    const response = await fetch(`/api/admin/versions/${versionId.value}/original`, {
      headers: { Authorization: `Bearer ${api.token()}` },
    })
    if (!response.ok) throw new Error('归档下载失败，请检查原文完整性及登录状态')
    const url = URL.createObjectURL(await response.blob())
    const anchor = document.createElement('a')
    anchor.href = url
    const extension =
      detail.value?.versions.find((v) => v.id === versionId.value)?.mime_type === 'application/pdf'
        ? 'pdf'
        : 'bin'
    anchor.download = `${versionId.value}.${extension}`
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}
const provenance = ref<{
  source_integrity_holds?: { reason: string }[]
  versions: {
    version_id: string
    acquisition: string
    integrity_ok: boolean
    pipeline_refetch_verified: boolean
  }[]
} | null>(null)

async function translateDocument(doc: DocumentOut) {
  translatingDocId.value = doc.id
  try {
    const res = await api.post<{
      document_id: string
      translated_count: number
    }>(`/admin/knowledge/documents/${doc.id}/translate`)
    ElMessage.success(`已完成文档批量翻译（共翻译 ${res.translated_count} 条条款）`)
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    translatingDocId.value = ''
  }
}

async function open(doc: DocumentOut) {
  provenance.value = null
  detail.value = await api.get<DocumentDetail>(`/admin/documents/${doc.id}`)
  provenance.value = await api.get(`/admin/documents/${doc.id}/provenance`)
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

// ---- 手动上传官方原文(WAF 拦截时的兜底入口) ----
const uploadDialog = ref(false)
const uploading = ref(false)
const uploadForm = reactive({
  document_id: '' as string,
  site_id: '' as string,
  source_url: '',
  title: '',
  version_label: '',
  published_date: '',
  file_name: '',
  mime_type: 'application/pdf',
  content_base64: '',
})

async function openUploadDialog(doc?: DocumentOut) {
  if (!sites.value.length) {
    try {
      sites.value = await api.get<SiteOut[]>('/admin/sites')
    } catch {
      /* 复核员无站点列表权限时忽略 */
    }
  }
  uploadForm.document_id = doc?.id ?? ''
  uploadForm.site_id = doc?.site_id ?? siteId.value ?? sites.value[0]?.id ?? ''
  uploadForm.source_url = doc?.source_url ?? ''
  uploadForm.title = doc?.title ?? ''
  uploadForm.version_label = ''
  uploadForm.published_date = ''
  uploadForm.file_name = ''
  uploadForm.content_base64 = ''
  uploadDialog.value = true
}

function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploadForm.file_name = file.name
  uploadForm.mime_type =
    file.type || (file.name.toLowerCase().endsWith('.html') ? 'text/html' : 'application/pdf')
  const reader = new FileReader()
  reader.onload = () => {
    const res = String(reader.result || '')
    const idx = res.indexOf('base64,')
    uploadForm.content_base64 = idx >= 0 ? res.slice(idx + 7) : res
  }
  reader.readAsDataURL(file)
}

async function submitUpload() {
  if (!uploadForm.content_base64) {
    ElMessage.warning('请先选择要上传的官方 PDF 或 HTML 文件')
    return
  }
  uploading.value = true
  try {
    const res = await api.post<DocumentDetail>('/admin/documents/upload', {
      document_id: uploadForm.document_id || null,
      site_id: uploadForm.document_id ? null : uploadForm.site_id || null,
      source_url: uploadForm.document_id ? null : uploadForm.source_url.trim() || null,
      title: uploadForm.title.trim() || null,
      mime_type: uploadForm.mime_type,
      content_base64: uploadForm.content_base64,
      version_label: uploadForm.version_label.trim() || null,
      published_date: uploadForm.published_date.trim() || null,
    })
    ElMessage.success('官方原文已归档并完成条款解析')
    uploadDialog.value = false
    await load()
    detail.value = res
    drawer.value = true
    if (res.versions.length) await pick(res.versions[0].id)
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    uploading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="management-page">
    <div class="page-heading">
      <div style="display: flex; align-items: center; gap: 10px">
        <div>
          <h1>法规文档</h1>
          <p>管理官方原文、历史版本与条款解析结果。</p>
        </div>
        <el-tag v-if="siteId" closable type="info" @close="clearSiteFilter">
          来源站点：{{ sitePublisher || siteId.slice(0, 8) }}
        </el-tag>
      </div>
      <div class="page-actions">
        <el-input
          v-model="country"
          placeholder="国家代码，例如 SG"
          aria-label="筛选国家"
          style="width: 160px"
          clearable
          @keyup.enter="load"
        />
        <el-button @click="load">查询</el-button>
        <el-button type="primary" @click="openUploadDialog()">上传官方原文</el-button>
      </div>
    </div>
    <el-table :data="docs" v-loading="loading" border @row-click="open" style="cursor: pointer">
      <el-table-column prop="country_code" label="国家" width="70" />
      <el-table-column label="法规文件 / 官方来源" min-width="330" show-overflow-tooltip>
        <template #default="{ row }"
          ><button type="button" class="document-title" @click.stop="open(row)">
            {{ row.title }}
          </button>
          <div class="document-source">{{ row.source_url }}</div></template
        >
      </el-table-column>
      <el-table-column label="类型" width="100">
        <template #default="{ row }">{{ DOC_TYPE_LABEL[row.document_type] }}</template>
      </el-table-column>
      <el-table-column label="相关性(语义抽取范围)" width="230">
        <template #default="{ row }">
          <el-tooltip
            :content="row.relevance_note || ''"
            placement="top"
            :disabled="!row.relevance_note"
          >
            <el-tag :type="RELEVANCE_META[row.relevance]?.type ?? 'info'" size="small">
              {{ RELEVANCE_META[row.relevance]?.label ?? row.relevance }}
            </el-tag>
          </el-tooltip>
          <el-tag
            v-if="row.relevance_source === 'manual'"
            type="warning"
            size="small"
            style="margin-left: 4px"
          >
            人工
          </el-tag>
          <el-dropdown
            trigger="click"
            style="margin-left: 6px"
            @command="(c: string) => overrideRelevance(row, c)"
          >
            <el-button size="small" link type="primary" @click.stop>调整</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="relevant">设为核心(参与抽取)</el-dropdown-item>
                <el-dropdown-item command="related">设为相关(参与抽取)</el-dropdown-item>
                <el-dropdown-item command="out_of_scope">设为范围外(不抽取)</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </el-table-column>
      <el-table-column label="最新版本" width="90">
        <template #default="{ row }">{{
          row.latest_version ? `第 ${row.latest_version.version_no} 版` : '—'
        }}</template>
      </el-table-column>
      <el-table-column label="状态" width="180">
        <template #default="{ row }">
          <el-tag v-if="row.safety_hold" type="danger">来源绑定异常 · 已隔离</el-tag>
          <div v-if="row.latest_version" class="document-status" data-testid="document-status">
            <el-tag
              :type="
                row.latest_version.state === 'parsed'
                  ? 'success'
                  : row.latest_version.state === 'failed'
                    ? 'danger'
                    : 'warning'
              "
              size="small"
            >
              {{ VERSION_STATE_LABEL[row.latest_version.state] }}
            </el-tag>
            <el-tag v-if="row.latest_version.ocr_required" type="warning" size="small">
              OCR
            </el-tag>
            <el-tag type="info" size="small">
              {{ row.latest_version.review_status === 'approved' ? '已复核' : '待复核' }}
            </el-tag>
          </div>
          <el-tag v-else type="info">未取得原文</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近检查" width="170">
        <template #default="{ row }">{{ fmt(row.last_checked_at) }}</template>
      </el-table-column>
      <el-table-column prop="last_error" label="最近错误" min-width="140" show-overflow-tooltip />
      <el-table-column label="操作" width="140" fixed="right">
        <template #default="{ row }">
          <el-button
            size="small"
            type="primary"
            plain
            :loading="translatingDocId === row.id"
            @click.stop="translateDocument(row)"
          >
            按文档批量翻译
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="uploadDialog"
      :title="uploadForm.document_id ? '上传该法规的新版本原文' : '手动上传官方原文(WAF 拦截兜底)'"
      width="560px"
    >
      <el-alert
        type="info"
        :closable="false"
        style="margin-bottom: 12px"
        title="当目标国家官网启用了 AWS WAF / CloudFront 人机验证导致自动抓取受阻时,可在浏览器下载官方 PDF 后在此上传。系统将同样计算 SHA-256 归档并自动执行条款切分或 OCR。"
      />
      <el-form label-width="110px">
        <template v-if="!uploadForm.document_id">
          <el-form-item label="所属采集站点">
            <el-select
              v-model="uploadForm.site_id"
              style="width: 100%"
              placeholder="选择归属的官方站点"
            >
              <el-option
                v-for="s in sites"
                :key="s.id"
                :label="`[${s.country_code}] ${s.publisher} (${s.entry_url.slice(0, 45)}...)`"
                :value="s.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="官方原文地址">
            <el-input v-model="uploadForm.source_url" placeholder="https://..." />
          </el-form-item>
        </template>
        <el-form-item label="法规标题">
          <el-input v-model="uploadForm.title" placeholder="官方法规名称" />
        </el-form-item>
        <el-form-item label="发布日期(选填)">
          <el-input v-model="uploadForm.published_date" placeholder="YYYY-MM-DD" />
        </el-form-item>
        <el-form-item label="版本说明(选填)">
          <el-input v-model="uploadForm.version_label" placeholder="如 Revised Edition 2020" />
        </el-form-item>
        <el-form-item label="原文文件">
          <input type="file" accept=".pdf,.html,.htm" @change="onFileChange" />
          <div v-if="uploadForm.file_name" style="font-size: 12px; color: #67c23a; margin-top: 4px">
            已选择：{{ uploadForm.file_name }}
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="uploadDialog = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="submitUpload">上传并解析</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="drawer" :title="detail?.title" size="880px">
      <template v-if="detail">
        <el-alert
          v-for="(hold, index) in provenance?.source_integrity_holds"
          :key="index"
          type="error"
          :closable="false"
          show-icon
          :title="hold.reason"
          style="margin-bottom: 16px"
        />
        <el-alert
          v-if="provenance"
          :closable="false"
          type="info"
          style="margin-bottom: 16px"
          title="归档完整性检查不等于官方真实性或法律有效性审批；后两项须人工核对。"
        />
        <div
          v-for="p in provenance?.versions.filter((x) => x.version_id === versionId)"
          :key="p.version_id"
          style="margin-bottom: 16px"
        >
          <el-tag :type="p.integrity_ok ? 'success' : 'danger'">{{
            p.integrity_ok ? '原文哈希一致' : '原文完整性异常'
          }}</el-tag>
          <span style="margin-left: 12px"
            >来源路径：{{
              p.acquisition === 'pipeline_fetch'
                ? '采集流水线下载'
                : p.acquisition === 'manual_upload'
                  ? '手工上传'
                  : '历史迁移 / 记录不足'
            }}{{ p.pipeline_refetch_verified ? ' · 已重新下载核对' : '' }}</span
          >
        </div>
        <div
          style="
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
          "
        >
          <span style="word-break: break-all; color: #909399"
            >原文地址:{{ detail.source_url }}</span
          >
          <el-button size="small" type="primary" plain @click="openUploadDialog(detail)"
            >上传新版本原文</el-button
          >
          <el-button :disabled="!versionId" @click="downloadOriginal">下载归档原文</el-button>
        </div>
        <el-radio-group
          :model-value="versionId"
          @change="(v: string | number | boolean | undefined) => pick(String(v))"
          style="margin-bottom: 12px"
        >
          <el-radio-button v-for="v in detail.versions" :key="v.id" :value="v.id">
            第 {{ v.version_no }} 版 · {{ fmt(v.fetched_at) }}
          </el-radio-button>
        </el-radio-group>

        <template v-for="v in detail.versions.filter((x) => x.id === versionId)" :key="v.id">
          <el-descriptions :column="2" border size="small" style="margin-bottom: 12px">
            <el-descriptions-item label="状态">{{
              VERSION_STATE_LABEL[v.state]
            }}</el-descriptions-item>
            <el-descriptions-item label="条款切分">
              {{ v.parse_strategy ? PARSE_STRATEGY_LABEL[v.parse_strategy] : '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="现行状态">{{
              LEGAL_STATUS_LABEL[v.legal_status]
            }}</el-descriptions-item>
            <el-descriptions-item label="官方版本说明">{{
              v.version_label ?? '—'
            }}</el-descriptions-item>
            <el-descriptions-item label="发布日期">{{
              v.published_date ?? '—'
            }}</el-descriptions-item>
            <el-descriptions-item label="生效日期">{{
              v.effective_date ?? '—'
            }}</el-descriptions-item>
            <el-descriptions-item label="页数">{{ v.page_count ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="文本字数">{{ v.char_count ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="内容指纹(SHA-256)" :span="2">
              <code style="word-break: break-all">{{ v.content_hash }}</code>
            </el-descriptions-item>
            <el-descriptions-item v-if="v.error" label="说明" :span="2">{{
              v.error
            }}</el-descriptions-item>
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
              <div style="font-size: 12px; color: #909399">
                {{ row.section_path ?? '' }}
              </div>
              <div v-if="row.heading" style="font-weight: 600">
                {{ row.heading }}
              </div>
            </template>
          </el-table-column>
          <el-table-column label="页码" width="80">
            <template #default="{ row }">
              {{
                row.page_start === row.page_end
                  ? row.page_start
                  : `${row.page_start}–${row.page_end}`
              }}
            </template>
          </el-table-column>
          <el-table-column label="原文">
            <template #default="{ row }"
              ><div style="white-space: pre-wrap">
                {{ row.original_text }}
              </div></template
            >
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
            {{
              e.step === 'download'
                ? '下载归档'
                : e.step === 'upload'
                  ? '手动上传'
                  : e.step === 'parse'
                    ? '解析条款'
                    : e.step
            }}:{{ e.message }}
          </el-timeline-item>
        </el-timeline>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.document-status {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 2px 0;
}
.document-status :deep(.el-tag) {
  flex: 0 0 auto;
  margin: 0;
  height: 24px;
  line-height: 22px;
}
.document-title {
  font-weight: 550;
  color: #263c4b;
}
.document-source {
  color: #8796a2;
  font-size: 12px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-top: 5px;
}
.document-title {
  display: block;
  padding: 0;
  border: 0;
  background: none;
  text-align: left;
  font: inherit;
  font-weight: 550;
  line-height: 1.7;
}
.document-title:hover {
  color: var(--cy-blue);
}
</style>
