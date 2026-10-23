<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { api } from '../api'

interface GlossaryHit {
  term_id?: string
  concept_code?: string
  source_term: string
  target_zh: string
  priority_level: number
  statutory_definition?: string | null
  source_reference?: string | null
  matched_in_translation?: boolean
}

interface UnmatchedTerm {
  source_term: string
  provisional_zh: string
  statutory_definition: string
  source_reference: string
}

interface ClauseTranslation {
  source_context_revision?: { id: string; review_status: string; quality_check_passed: boolean } | null
  id: string
  clause_id: string
  document_id: string
  country_code: string
  document_title: string
  clause_number: string | null
  heading: string | null
  section_path: string | null
  original_text: string
  translated_text: string
  engine: string
  model_name: string
  prompt_version: string
  tokens_in?: number
  tokens_out?: number
  duration_ms?: number
  glossary_hits: GlossaryHit[]
  unmatched_terms: UnmatchedTerm[]
  quality_check_passed: boolean
  quality_issues: string[]
  review_status: string
  status?: string
  usable_as_legal_basis: boolean
  status_badge: string
  reviewer_note: string | null
  reviewed_at: string | null
}

interface ReviewPoint {
  id: string
  country_code: string
  point_code: string
  title: string
  question: string
  impact_on_conclusion: string
  provisional_interpretation: string
  status: string
  lawyer_reply: string | null
  decided_at: string | null
}

interface GlossaryTerm {
  id: string
  concept_code: string
  country_code: string | null
  language: string
  source_term: string
  target_zh: string
  priority_level: number
  statutory_definition: string | null
  source_reference: string | null
  review_status: string
}

const activeTab = ref<'translations' | 'points' | 'glossary'>('translations')
const countryFilter = ref('SG')
const statusFilter = ref('')
const loading = ref(false)

const translations = ref<ClauseTranslation[]>([])
const translationPage = ref(1)
const translationPageSize = 50
const hasNextTranslations = ref(false)
const historyDialog = ref(false)
const historyResult = ref<{ history: unknown[]; proposals: unknown[] } | null>(null)

async function showHistory(id: string) {
  historyResult.value = await api.get(`/admin/knowledge/history/${id}`)
  historyDialog.value = true
}

function resetAndLoad() {
  translationPage.value = 1
  return loadAll()
}

function turnTranslationPage(delta: number) {
  translationPage.value = Math.max(1, translationPage.value + delta)
  return loadAll()
}
const reviewPoints = ref<ReviewPoint[]>([])
const glossaryTerms = ref<GlossaryTerm[]>([])

const editTranslationDialog = ref(false)
const currentTranslation = ref<ClauseTranslation | null>(null)
const editTranslatedText = ref('')
const editReviewerNote = ref('')

const exportDialog = ref(false)
const exportedMarkdown = ref('')
const exportedJson = ref('')

const newTermDialog = ref(false)
const newTermForm = ref({
  concept_code: '',
  country_code: 'SG',
  language: 'en',
  source_term: '',
  target_zh: '',
  priority_level: 1,
  statutory_definition: '',
  source_reference: '',
})

const PRIORITY_LABELS: Record<number, string> = {
  1: 'P1 法律定义条款',
  2: 'P2 监管机构指南',
  3: 'P3 官方多语版本',
  4: 'P4 国际通行概念',
}

async function loadAll() {
  loading.value = true
  try {
    const qCountry = countryFilter.value ? `?country_code=${countryFilter.value}` : ''
    const qTr = new URLSearchParams()
    if (countryFilter.value) qTr.set('country_code', countryFilter.value)
    if (statusFilter.value) qTr.set('review_status', statusFilter.value)
    qTr.set('limit', String(translationPageSize + 1))
    qTr.set('offset', String((translationPage.value - 1) * translationPageSize))

    const [trList, rpList, glList] = await Promise.all([
      api.get<ClauseTranslation[]>(`/admin/knowledge/translations?${qTr.toString()}`),
      api.get<ReviewPoint[]>(`/admin/knowledge/review-points${qCountry}`),
      api.get<GlossaryTerm[]>(`/admin/knowledge/glossary${qCountry}`),
    ])
    hasNextTranslations.value = trList.length > translationPageSize
    translations.value = trList.slice(0, translationPageSize)
    reviewPoints.value = rpList
    glossaryTerms.value = glList
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function runCountryExtract() {
  const cc = countryFilter.value || 'SG'
  loading.value = true
  try {
    const res = await api.post<{
      status: string
      reason?: string
      nodes_total: number
      edges_total: number
      clauses_translated: number
    }>(`/admin/knowledge/extract?country_code=${cc}`)
    if (res.status !== 'completed') {
      ElMessage.warning(`图谱未替换，保留旧结果：${res.reason || res.status}`)
      return
    }
    ElMessage.success(
      `${cc} 抽取与受控翻译完成：新增译文 ${res.clauses_translated} 条，图谱节点 ${res.nodes_total} 个、语义边 ${res.edges_total} 条`,
    )
    await loadAll()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function openTranslationReview(row: ClauseTranslation) {
  currentTranslation.value = row
  editTranslatedText.value = row.translated_text
  editReviewerNote.value = row.reviewer_note || ''
  editTranslationDialog.value = true
}

async function submitTranslationReview(decision: 'approved' | 'rejected') {
  if (!currentTranslation.value) return
  try {
    await api.post(`/admin/knowledge/translations/${currentTranslation.value.id}/review`, {
      decision,
      translated_text: editTranslatedText.value,
      reviewer_note: editReviewerNote.value || null,
    })
    ElMessage.success(
      decision === 'approved' ? '已复核通过（可作为正式法律依据引用）' : '已驳回该条译文',
    )
    editTranslationDialog.value = false
    await loadAll()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  }
}

async function quickApproveTranslation(row: ClauseTranslation) {
  try {
    await api.post(`/admin/knowledge/translations/${row.id}/review`, {
      decision: 'approved',
      reviewer_note: '律所复核通过',
    })
    ElMessage.success('已复核通过')
    await loadAll()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  }
}

async function batchTranslateDoc(row: ClauseTranslation) {
  loading.value = true
  try {
    const res = await api.post<{
      document_id: string
      translated_count: number
    }>(`/admin/knowledge/documents/${row.document_id}/translate`)
    ElMessage.success(`按文档批量翻译完成：共处理 ${res.translated_count} 条条款`)
    await loadAll()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function decidePoint(point: ReviewPoint, status: 'confirmed' | 'needs_discussion') {
  const reply = point.lawyer_reply?.trim() || point.provisional_interpretation
  try {
    await api.post(`/admin/knowledge/review-points/${point.id}/decide`, {
      status,
      lawyer_reply: reply,
    })
    ElMessage.success('已保存律所确认意见')
    await loadAll()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  }
}

async function openExportSheet() {
  try {
    const q = countryFilter.value ? `?country_code=${countryFilter.value}` : ''
    const res = await api.get<{
      markdown: string
      items: Array<Record<string, unknown>>
    }>(`/admin/knowledge/review-points/export${q}`)
    exportedMarkdown.value = res.markdown
    const editableItems = (res.items || []).map((it) => ({
      ...it,
      status: it.status === 'open' ? 'confirmed' : it.status,
      lawyer_reply: it.lawyer_reply || it.provisional_interpretation,
    }))
    exportedJson.value = JSON.stringify({ items: editableItems }, null, 2)
    exportDialog.value = true
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  }
}

async function importJsonSheet() {
  try {
    const parsed = JSON.parse(exportedJson.value)
    const res = await api.post<{ updated: number }>('/admin/knowledge/review-points/import', {
      items: parsed.items || [],
    })
    ElMessage.success(`已批量导入 ${res.updated} 条律所确认单回执`)
    exportDialog.value = false
    await loadAll()
  } catch (e: unknown) {
    ElMessage.error(`导入失败: ${(e as Error).message}`)
  }
}

async function createTerm() {
  try {
    await api.post('/admin/knowledge/glossary', {
      ...newTermForm.value,
      country_code: newTermForm.value.country_code || null,
    })
    ElMessage.success('已新增受控法律术语')
    newTermDialog.value = false
    await loadAll()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message)
  }
}

onMounted(loadAll)
</script>

<template>
  <div>
    <div class="page-heading">
      <div>
        <h1>翻译与复核</h1>
        <div class="sub">
          只有标记为「已复核(可作法律依据)」的条款与关系才会被安全报告与申请表当作法律依据引用；未复核内容显式标注「机器翻译，待核对」。
        </div>
      </div>
      <div style="display: flex; gap: 8px; align-items: center">
        <el-select v-model="countryFilter" style="width: 140px" @change="resetAndLoad">
          <el-option value="SG" label="SG 新加坡" />
          <el-option value="EG" label="EG 埃及" />
          <el-option value="TN" label="TN 突尼斯" />
          <el-option value="MA" label="MA 摩洛哥" />
          <el-option value="LB" label="LB 黎巴嫩" />
        </el-select>
        <el-popover trigger="click" placement="bottom-end" :width="310"
          ><template #reference><el-button>批量处理</el-button></template>
          <div class="secondary-actions">
            <p class="page-description">
              对当前国家执行全量图谱抽取，此操作会调用模型；批量翻译单独执行。
            </p>
            <el-button :loading="loading" @click="runCountryExtract">运行本国图谱抽取</el-button>
          </div></el-popover
        >
        <el-button @click="loadAll">刷新</el-button>
      </div>
    </div>

    <el-dialog v-model="historyDialog" title="历史与审批记录" width="760px">
      <p>机器重试不会沿用旧审批。既有历史未补造审批记录；以下仅展示本次修复后留存的快照与提案。</p>
      <pre style="max-height: 480px; overflow: auto; white-space: pre-wrap">{{
        JSON.stringify(historyResult, null, 2)
      }}</pre>
    </el-dialog>
    <el-tabs v-model="activeTab">
      <!-- 第一页签：条款级机器翻译与复核 -->
      <el-tab-pane label="条款翻译" name="translations">
        <div style="margin-bottom: 12px; display: flex; gap: 10px; align-items: center">
          <span class="section-description">复核状态</span
          ><el-select
            v-model="statusFilter"
            placeholder="全部状态"
            style="width: 240px"
            aria-label="复核状态筛选"
            @change="resetAndLoad"
            ><el-option value="" label="全部状态" /><el-option
              value="machine_translated"
              label="机翻通过 · 待核对" /><el-option
              value="pending_review"
              label="待律所复核" /><el-option
              value="approved"
              label="已复核 · 可作依据" /><el-option
              value="translation_failed"
              label="翻译失败" /><el-option value="not_translated" label="未翻译" /><el-option
              value="stale"
              label="原文已更新 · 待重核"
          /></el-select>
        </div>

        <el-table :data="translations" v-loading="loading" border stripe>
          <el-table-column label="所属法规与条号" width="210">
            <template #default="{ row }">
              <div style="font-weight: 600">{{ row.document_title }}</div>
              <div class="sub">
                条号: {{ row.clause_number || '-' }}
                <span v-if="row.heading">· {{ row.heading }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="官方原文" min-width="220">
            <template #default="{ row }">
              <div class="clause-box">{{ row.original_text }}</div>
            </template>
          </el-table-column>
          <el-table-column label="中文译文与术语" min-width="260">
            <template #default="{ row }">
              <div class="clause-box">
                {{ row.translated_text || '未翻译' }}
              </div>
              <div
                v-if="row.glossary_hits?.length || row.unmatched_terms?.length"
                style="margin-top: 6px; display: flex; flex-wrap: wrap; gap: 4px"
              >
                <el-tooltip
                  v-for="h in row.glossary_hits"
                  :key="h.source_term"
                  placement="top"
                  :content="`法定定义(P${h.priority_level}): ${h.statutory_definition || '见法规定义条款'} | 依据: ${h.source_reference || '-'}`"
                >
                  <el-tag size="small" type="success">
                    术语: {{ h.source_term }} → {{ h.target_zh }}
                  </el-tag>
                </el-tooltip>
                <el-tooltip
                  v-for="u in row.unmatched_terms"
                  :key="u.source_term"
                  placement="top"
                  :content="`暂译解释: ${u.statutory_definition} (参考: ${u.source_reference})`"
                >
                  <el-tag size="small" type="warning">
                    原词保留: {{ u.source_term }} ({{ u.provisional_zh }})
                  </el-tag>
                </el-tooltip>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="质量与状态" width="170">
            <template #default="{ row }">
              <div>
                <el-tag :type="row.usable_as_legal_basis ? 'success' : 'warning'" size="small">
                  {{ row.status_badge }}
                </el-tag>
              </div>
              <div style="margin-top: 4px">
                <el-tag
                  :type="row.quality_check_passed ? 'info' : 'danger'"
                  size="small"
                  effect="plain"
                >
                  {{ row.quality_check_passed ? '四项程序校验通过' : '译后检查未通过' }}
                </el-tag>
              </div>
              <div class="sub" style="margin-top: 4px">
                {{ row.engine }}/{{ row.model_name }} · {{ row.prompt_version }}
                <span v-if="row.tokens_in || row.tokens_out">
                  ({{ row.tokens_in }}/{{ row.tokens_out }} tok, {{ row.duration_ms }}ms)
                </span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="160" fixed="right"
            ><template #default="{ row }"
              ><el-button size="small" type="primary" link @click="openTranslationReview(row)"
                >校订与复核</el-button
              ><el-dropdown trigger="click"
                ><el-button size="small" text>更多</el-button
                ><template #dropdown
                  ><el-dropdown-menu
                    ><el-dropdown-item @click="quickApproveTranslation(row)"
                      >直接通过复核</el-dropdown-item
                    ><el-dropdown-item @click="batchTranslateDoc(row)"
                      >按文档批量翻译</el-dropdown-item
                    ><el-dropdown-item @click="showHistory(row.id)"
                      >历史与审批记录</el-dropdown-item
                    ><el-dropdown-item v-if="row.source_context_revision" @click="showHistory(row.clause_id)"
                      >原文上下文修订（待核对）</el-dropdown-item
                    ></el-dropdown-menu
                  ></template
                ></el-dropdown
              ></template
            ></el-table-column
          >
        </el-table>
        <div
          style="
            display: flex;
            justify-content: flex-end;
            align-items: center;
            gap: 12px;
            margin-top: 16px;
          "
        >
          <el-button :disabled="translationPage === 1 || loading" @click="turnTranslationPage(-1)"
            >上一页</el-button
          >
          <span>第 {{ translationPage }} 页 · 每页 {{ translationPageSize }} 条</span>
          <el-button :disabled="!hasNextTranslations || loading" @click="turnTranslationPage(1)"
            >下一页</el-button
          >
        </div>
      </el-tab-pane>

      <!-- 第二页签：解读要点确认单 -->
      <el-tab-pane label="解读确认" name="points">
        <div style="margin-bottom: 12px; display: flex; justify-content: space-between">
          <div class="sub">
            把法规未明确定义、官方无解释、又直接影响结论的少数关键问题汇总成一份确认单，交由合作律所一次性确认。
          </div>
          <el-button type="primary" plain size="small" @click="openExportSheet">
            导出 / 批量回写确认单
          </el-button>
        </div>

        <el-table :data="reviewPoints" border stripe>
          <el-table-column prop="point_code" label="编号" width="110" />
          <el-table-column prop="title" label="核心争议议题" width="220" />
          <el-table-column prop="question" label="待律所确认问题" min-width="240" />
          <el-table-column
            prop="impact_on_conclusion"
            label="对车企合规结论的影响"
            min-width="200"
          />
          <el-table-column label="平台暂拟口径与律所复核意见" min-width="280">
            <template #default="{ row }">
              <div class="sub" style="margin-bottom: 6px">
                <strong>暂拟口径：</strong>{{ row.provisional_interpretation }}
              </div>
              <el-input
                v-model="row.lawyer_reply"
                type="textarea"
                :rows="2"
                placeholder="在此填写律所正式确认口径..."
              />
            </template>
          </el-table-column>
          <el-table-column label="状态与操作" width="150" fixed="right">
            <template #default="{ row }">
              <div style="margin-bottom: 6px">
                <el-tag :type="row.status === 'confirmed' ? 'success' : 'warning'" size="small">
                  {{ row.status === 'confirmed' ? '律所已确认' : '待律所确认' }}
                </el-tag>
              </div>
              <el-button size="small" type="primary" @click="decidePoint(row, 'confirmed')">
                确认口径
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- 第三页签：两层法律术语库 -->
      <el-tab-pane label="法律术语" name="glossary">
        <div style="margin-bottom: 12px; display: flex; justify-content: space-between">
          <div class="sub">
            按四级优先级管理：P1 法律自身定义条款 &gt; P2 监管指南 &gt; P3 官方多语版本 &gt; P4
            国际通行概念。本国覆盖自动优先于通用概念。
          </div>
          <el-button type="primary" size="small" @click="newTermDialog = true">
            新增术语词条
          </el-button>
        </div>

        <el-table :data="glossaryTerms" border stripe>
          <el-table-column prop="concept_code" label="概念编码" width="180" />
          <el-table-column label="层级范围" width="110">
            <template #default="{ row }">
              <el-tag :type="row.country_code ? 'primary' : 'info'" size="small">
                {{ row.country_code ? `${row.country_code} 国家覆盖` : '跨国通用基准' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="来源优先级" width="145">
            <template #default="{ row }">
              {{ PRIORITY_LABELS[row.priority_level] || `P${row.priority_level}` }}
            </template>
          </el-table-column>
          <el-table-column prop="source_term" label="外文原词" width="200" />
          <el-table-column prop="target_zh" label="强制规定译法" width="160" />
          <el-table-column prop="statutory_definition" label="法定定义与说明" min-width="260" />
          <el-table-column prop="source_reference" label="依据条款" width="220" />
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- 条款校订复核弹窗 -->
    <el-dialog v-model="editTranslationDialog" title="校订与复核法规条款译文" width="680px">
      <template v-if="currentTranslation">
        <div style="margin-bottom: 8px; font-weight: 600">
          {{ currentTranslation.document_title }} · 条号
          {{ currentTranslation.clause_number || '-' }}
        </div>
        <div class="clause-box" style="background: #f5f7fa; padding: 10px; margin-bottom: 12px">
          {{ currentTranslation.original_text }}
        </div>
        <el-form label-width="96px">
          <el-form-item label="中文审定译文">
            <el-input v-model="editTranslatedText" type="textarea" :rows="5" />
          </el-form-item>
          <el-form-item label="复核备注">
            <el-input v-model="editReviewerNote" placeholder="可填写律所审定说明或依据" />
          </el-form-item>
        </el-form>
      </template>
      <template #footer>
        <el-button @click="editTranslationDialog = false">取消</el-button>
        <el-button type="danger" plain @click="submitTranslationReview('rejected')">
          驳回重译
        </el-button>
        <el-button type="success" @click="submitTranslationReview('approved')">
          复核通过 (作为有效法律依据)
        </el-button>
      </template>
    </el-dialog>

    <!-- 导出/导入解读要点确认单弹窗 -->
    <el-dialog
      v-model="exportDialog"
      title="《解读要点确认单》导出与律所回执批量导入"
      width="760px"
    >
      <el-form label-position="top">
        <el-form-item label="Markdown 确认单 (可直接复制发给合作律所)">
          <el-input v-model="exportedMarkdown" type="textarea" :rows="7" readonly />
        </el-form-item>
        <el-form-item
          label="JSON 结构化回执 (修改 status='confirmed' 与 lawyer_reply 后点击下方批量回写)"
        >
          <el-input v-model="exportedJson" type="textarea" :rows="8" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="exportDialog = false">关闭</el-button>
        <el-button type="primary" @click="importJsonSheet">批量导入律所回执</el-button>
      </template>
    </el-dialog>

    <!-- 新增术语弹窗 -->
    <el-dialog v-model="newTermDialog" title="新增受控法律术语" width="560px">
      <el-form :model="newTermForm" label-width="110px">
        <el-form-item label="概念编码">
          <el-input v-model="newTermForm.concept_code" placeholder="如 vehicle_telematics_data" />
        </el-form-item>
        <el-form-item label="国家代码">
          <el-input v-model="newTermForm.country_code" placeholder="如 SG，留空表示跨国通用" />
        </el-form-item>
        <el-form-item label="语种">
          <el-select v-model="newTermForm.language">
            <el-option value="en" label="en 英语" />
            <el-option value="fr" label="fr 法语" />
            <el-option value="ar" label="ar 阿拉伯语" />
          </el-select>
        </el-form-item>
        <el-form-item label="外文原词">
          <el-input v-model="newTermForm.source_term" />
        </el-form-item>
        <el-form-item label="强制中文译法">
          <el-input v-model="newTermForm.target_zh" />
        </el-form-item>
        <el-form-item label="来源优先级">
          <el-select v-model="newTermForm.priority_level">
            <el-option :value="1" label="1 - 法律自身定义条款(最高)" />
            <el-option :value="2" label="2 - 监管机构指南与问答" />
            <el-option :value="3" label="3 - 官方多语种版本" />
            <el-option :value="4" label="4 - 国际通行概念" />
          </el-select>
        </el-form-item>
        <el-form-item label="法定定义">
          <el-input v-model="newTermForm.statutory_definition" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="依据条款出处">
          <el-input v-model="newTermForm.source_reference" placeholder="如 SG PDPA s 2(1)" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="newTermDialog = false">取消</el-button>
        <el-button type="primary" @click="createTerm">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.sub {
  color: #606266;
  font-size: 13px;
}
.clause-box {
  max-height: 110px;
  overflow-y: auto;
  white-space: pre-wrap;
  font-size: 13px;
  line-height: 1.5;
}
</style>
