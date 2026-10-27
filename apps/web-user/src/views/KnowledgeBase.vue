<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'
import RegulationDirectory from '../components/RegulationDirectory.vue'
import LegalText from '../components/LegalText.vue'
import type { LegalTerm } from '@cyshuhe/shared/src/legalText'
const RegulationRelations = defineAsyncComponent(
  () => import('../components/RegulationRelations.vue'),
)
const readingView = ref('clauses')
async function readRelationBasis(clauseId: string) {
  if (!detail.value) return
  readingView.value = 'clauses'
  await openDocument(detail.value.regulation.id, clauseId)
}
interface Country {
  country_code: string
  country_name_zh: string
  documents_count: number
}
interface Regulation {
  id: string
  country_code: string
  title: string
  document_type: string
  document_type_zh: string
  publisher: string
  language: string
  source_url: string
  version_no: number
  legal_status: string
  clauses_count: number
}
interface Clause {
  clause_id: string
  ordinal: number
  clause_number: string | null
  heading: string | null
  section_path?: string | null
  original_text: string
  translated_text: string | null
}
interface Detail {
  version_id: string | null
  clauses_total: number
  clause_offset: number
  regulation: Regulation
  clauses: Clause[]
  versions: Array<{
    version_no: number
    legal_status: string
    fetched_at: string | null
  }>
}
interface Topic {
  id: string
  topic: string
  topic_label: string
  title: string
  document_id: string
  document_title: string
  clause_id: string
  clause_number: string
  version_no: number
  quote: string
  source_url: string
}
const countries = ref<Country[]>([]),
  regulations = ref<Regulation[]>([]),
  detail = ref<Detail | null>(null)
const country = ref(''),
  tab = ref('library'),
  search = ref(''),
  clauseSearch = ref(''),
  topic = ref('')
const terms = ref<LegalTerm[]>([]),
  docTerms = ref<LegalTerm[]>([]),
  topics = ref<Topic[]>([])
const busy = ref(false),
  detailBusy = ref(false),
  page = ref(1),
  topicPage = ref(1),
  termPage = ref(1),
  selectedClause = ref(''),
  showVersions = ref(false)
let requestNo = 0,
  docRequest = 0
const directoryCountries = computed(() =>
  countries.value.map((c) => ({
    code: c.country_code,
    name: c.country_name_zh,
    count: c.documents_count,
  })),
)
const filteredDocs = computed(() =>
  regulations.value.filter((d) => d.title.toLowerCase().includes(search.value.toLowerCase())),
)
const filteredTerms = computed(() =>
  terms.value.filter((t) =>
    `${t.source_term} ${t.target_zh} ${t.definition_original || ''}`
      .toLowerCase()
      .includes(search.value.toLowerCase()),
  ),
)
const topicOptions = computed(() => [
  ...new Map(topics.value.map((t) => [t.topic, t.topic_label])).entries(),
])
const topicGroups = computed(() =>
  topicOptions.value.map(([key, label]) => {
    const rows = topics.value.filter((t) => t.topic === key)
    return {
      key,
      label,
      count: rows.length,
      documents: new Set(rows.map((t) => t.document_id)).size,
    }
  }),
)
function resetSearchPages() {
  topicPage.value = 1
  termPage.value = 1
}
function chooseTopic(key: string) {
  topic.value = key
  topicPage.value = 1
}
function clauseLabel(value: string | null | undefined) {
  if (!value) return ''
  return value.length > 70
    ? value.match(/^(?:[A-Za-zÀ-ÿ.]+\s*)?\d+(?:\.\d+)*(?:\([a-z0-9]+\))*/)?.[0] ||
        value.slice(0, 65) + '…'
    : value
}
const filteredTopics = computed(() =>
  topics.value.filter(
    (t) =>
      (!topic.value || t.topic === topic.value) &&
      `${t.title} ${t.document_title} ${t.quote}`
        .toLowerCase()
        .includes(search.value.toLowerCase()),
  ),
)
const pageClauses = computed(() => detail.value?.clauses || [])
const legalStatus: Record<string, string> = {
  in_force: '现行',
  amended: '已修订',
  repealed: '已废止',
  unknown: '有效状态未记录',
}
async function chooseCountry(code: string) {
  const id = ++requestNo
  ++docRequest
  country.value = code
  busy.value = true
  detail.value = null
  search.value = ''
  topic.value = ''
  topicPage.value = 1
  termPage.value = 1
  try {
    const [docs, ts, tp] = await Promise.all([
      api.get<Regulation[]>(`/api/knowledge/regulations?country_code=${code}`),
      api.get<{ items: LegalTerm[] }>(`/api/knowledge/terminology?country_code=${code}`),
      api.get<{ items: Topic[] }>(`/api/knowledge/topics?country_code=${code}`),
    ])
    if (id !== requestNo) return
    regulations.value = docs
    terms.value = ts.items
    topics.value = tp.items
    if (docs[0]) await openDocument(docs[0].id)
  } catch (e) {
    if (id === requestNo) {
      regulations.value = []
      terms.value = []
      topics.value = []
      ElMessage.error((e as Error).message)
    }
  } finally {
    if (id === requestNo) busy.value = false
  }
}
async function openDocument(id: string, clauseId = '') {
  clearTimeout(clauseSearchTimer)
  if (id !== detail.value?.regulation.id) readingView.value = 'clauses'
  const req = ++docRequest
  detailBusy.value = true
  clauseSearch.value = ''
  page.value = 1
  selectedClause.value = clauseId
  try {
    const d = await api.get<Detail>(
      `/api/knowledge/regulations/${id}${clauseId ? `?focus_clause_id=${clauseId}` : ''}`,
    )
    if (req !== docRequest) return
    const t = await api.get<{ items: LegalTerm[] }>(
      `/api/knowledge/terminology?country_code=${country.value}&document_id=${id}${d.version_id ? `&version_id=${d.version_id}` : ''}`,
    )
    if (req !== docRequest) return
    detail.value = d
    docTerms.value = t.items
    page.value = Math.floor(d.clause_offset / 12) + 1
    if (clauseId) {
      await nextTick()
      if (req === docRequest)
        document.getElementById(`clause-${clauseId}`)?.scrollIntoView({
          block: 'start',
          behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches
            ? 'instant'
            : 'smooth',
        })
    }
  } catch (e) {
    if (req === docRequest) {
      detail.value = null
      ElMessage.error((e as Error).message)
    }
  } finally {
    if (req === docRequest) detailBusy.value = false
  }
}
function turnClausePage(value: number) {
  if (value === page.value) return
  page.value = value
  void loadClausePage()
}
async function loadClausePage() {
  const doc = detail.value
  if (!doc) return
  const req = ++docRequest
  detailBusy.value = true
  try {
    const q = new URLSearchParams({
      clause_offset: String((page.value - 1) * 12),
      clause_limit: '12',
      keyword: clauseSearch.value,
    })
    if (doc.version_id) q.set('version_id', doc.version_id)
    const d = await api.get<Detail>(`/api/knowledge/regulations/${doc.regulation.id}?${q}`)
    if (req === docRequest) detail.value = d
  } catch (e) {
    if (req === docRequest) ElMessage.error((e as Error).message)
  } finally {
    if (req === docRequest) detailBusy.value = false
  }
}
let clauseSearchTimer: ReturnType<typeof setTimeout> | undefined
function searchClauses() {
  page.value = 1
  clearTimeout(clauseSearchTimer)
  clauseSearchTimer = setTimeout(loadClausePage, 250)
}
async function seeBasis(t: Topic) {
  tab.value = 'library'
  await openDocument(t.document_id, t.clause_id)
}
onUnmounted(() => {
  clearTimeout(clauseSearchTimer)
  ++docRequest
  ++requestNo
})
onMounted(async () => {
  try {
    countries.value = await api.get<Country[]>('/api/knowledge/overview')
    if (countries.value[0]) await chooseCountry(countries.value[0].country_code)
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
})
</script>
<template>
  <div class="knowledge-page">
    <header class="page-heading">
      <div>
        <h1>法规与合规知识</h1>
        <p>从国别法规找到合规要求，查看对应条款与术语释义。</p>
      </div>
      <el-select
        v-model="country"
        aria-label="选择国家"
        style="width: 180px"
        @change="chooseCountry"
        ><el-option
          v-for="c in countries"
          :key="c.country_code"
          :value="c.country_code"
          :label="c.country_name_zh"
      /></el-select>
    </header>
    <div class="knowledge-search">
      <el-input
        v-model="search"
        clearable
        placeholder="搜索法规、合规主题或法律术语"
        aria-label="搜索法规知识"
        @input="resetSearchPages"
      />
    </div>
    <el-tabs v-model="tab">
      <el-tab-pane label="法规浏览" name="library"
        ><div class="library-layout" v-loading="busy">
          <RegulationDirectory
            :countries="directoryCountries"
            :documents="filteredDocs"
            :country="country"
            :selected-id="detail?.regulation.id"
            :loading="busy"
            @country="chooseCountry"
            @document="openDocument"
          />
          <main class="reading-panel" v-loading="detailBusy">
            <template v-if="detail">
              <header class="document-heading">
                <div>
                  <div class="eyebrow">
                    {{ detail.regulation.document_type_zh }} ·
                    {{ detail.regulation.publisher }}
                  </div>
                  <h2>{{ detail.regulation.title }}</h2>
                  <p>
                    {{
                      legalStatus[detail.regulation.legal_status] || detail.regulation.legal_status
                    }}
                    · 归档版本 {{ detail.regulation.version_no }} ·
                    {{ detail.regulation.clauses_count }} 条
                  </p>
                </div>
                <a
                  :href="detail.regulation.source_url"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="source-link"
                  >官方原文 ↗</a
                >
              </header>
              <el-radio-group v-model="readingView" class="reading-view" aria-label="法规阅读方式">
                <el-radio-button value="clauses">条款阅读</el-radio-button>
                <el-radio-button value="relations">关系解读</el-radio-button>
              </el-radio-group>
              <RegulationRelations
                v-if="readingView === 'relations'"
                :key="`${detail.regulation.id}:${detail.version_id}`"
                :document-id="detail.regulation.id"
                :version-id="detail.version_id"
                @basis="readRelationBasis"
              />
              <template v-else>
                <div class="reading-tools">
                  <el-input
                    v-model="clauseSearch"
                    clearable
                    placeholder="查找条号或正文"
                    aria-label="查找条款"
                    @input="searchClauses"
                  /><el-button text @click="showVersions = !showVersions">版本记录</el-button>
                </div>
                <div v-if="showVersions" class="versions">
                  <div v-for="v in detail.versions" :key="v.version_no">
                    归档版本 {{ v.version_no }} ·
                    {{ legalStatus[v.legal_status] || v.legal_status }} ·
                    {{ v.fetched_at?.slice(0, 10) || '日期未记录' }}
                  </div>
                </div>
                <article
                  v-for="c in pageClauses"
                  :key="c.clause_id"
                  :id="`clause-${c.clause_id}`"
                  class="clause"
                  :class="{ selected: selectedClause === c.clause_id }"
                >
                  <h3>
                    {{ clauseLabel(c.clause_number) || c.ordinal
                    }}<template v-if="c.heading"> · {{ c.heading }}</template>
                  </h3>
                  <div class="parallel">
                    <section>
                      <h4>原文</h4>
                      <LegalText
                        :text="c.original_text"
                        :terms="docTerms"
                        :clause-id="c.clause_id"
                        :section-path="c.section_path"
                      />
                    </section>
                    <section>
                      <h4>中文对照</h4>
                      <LegalText
                        v-if="c.translated_text"
                        :text="c.translated_text"
                        :terms="docTerms"
                        :clause-id="c.clause_id"
                        :section-path="c.section_path"
                      />
                      <p v-else class="muted">暂无中文对照</p>
                    </section>
                  </div>
                </article>
                <el-empty v-if="!pageClauses.length" description="没有匹配的条款" /><el-pagination
                  v-if="detail.clauses_total > 12"
                  :current-page="page"
                  @update:current-page="turnClausePage"
                  :page-size="12"
                  :total="detail.clauses_total"
                  layout="prev, pager, next, total"
                />
              </template> </template
            ><el-empty v-else-if="!detailBusy" description="选择一份法规开始阅读" />
          </main></div
      ></el-tab-pane>
      <el-tab-pane label="合规主题" name="topics"
        ><div class="topic-filters">
          <el-radio-group v-model="topic" @change="topicPage = 1"
            ><el-radio-button value="">主题总览</el-radio-button
            ><el-radio-button v-for="[key, label] in topicOptions" :key="key" :value="key">{{
              label
            }}</el-radio-button></el-radio-group
          ><span class="muted">{{ filteredTopics.length }} 个关联要点</span>
        </div>
        <div v-if="!topic && !search" class="topic-overview">
          <button
            v-for="g in topicGroups"
            :key="g.key"
            class="topic-overview-card"
            @click="chooseTopic(g.key)"
          >
            <h2>{{ g.label }}</h2>
            <p>{{ g.documents }} 份法规与指南 · {{ g.count }} 个要点</p>
            <span>查看适用条款 →</span>
          </button>
        </div>
        <div v-else class="topic-grid" v-loading="busy">
          <article
            v-for="t in filteredTopics.slice((topicPage - 1) * 18, topicPage * 18)"
            :key="t.id"
            class="topic-card"
          >
            <div class="eyebrow">{{ t.topic_label }}</div>
            <h2>{{ t.title }}</h2>
            <p class="topic-document">
              {{ t.document_title }} · {{ clauseLabel(t.clause_number) }}
            </p>
            <blockquote dir="auto">{{ t.quote }}</blockquote>
            <el-button link type="primary" @click="seeBasis(t)">阅读条款与中文对照 →</el-button>
          </article>
        </div>
        <el-empty
          v-if="!busy && !filteredTopics.length"
          description="该国当前没有匹配的主题要点，可从法规浏览查阅原文"
        /><el-pagination
          v-if="(topic || search) && filteredTopics.length > 18"
          v-model:current-page="topicPage"
          :page-size="18"
          :total="filteredTopics.length"
          layout="prev, pager, next, total"
        />
      </el-tab-pane>
      <el-tab-pane label="法律术语" name="terms"
        ><div class="term-grid" v-loading="busy">
          <article
            v-for="t in filteredTerms.slice((termPage - 1) * 24, termPage * 24)"
            :key="t.id"
            class="term-card"
          >
            <div class="eyebrow">{{ t.source_label }}</div>
            <h2>
              {{ t.target_zh }}
              <small v-if="t.rendering_label === '暂译'" class="muted">暂译</small>
            </h2>
            <p class="original-term" dir="auto">{{ t.source_term }}</p>
            <p v-if="t.definition_zh">{{ t.definition_zh }}</p>
            <details v-if="t.definition_original">
              <summary>查看来源定义</summary>
              <p dir="auto">{{ t.definition_original }}</p>
            </details>
            <div class="term-source">
              {{ t.source_reference
              }}<a
                v-if="t.source_url"
                :href="t.source_url"
                target="_blank"
                rel="noopener noreferrer"
              >
                官方来源 ↗</a
              >
            </div>
          </article>
        </div>
        <el-empty
          v-if="!busy && !filteredTerms.length"
          description="没有匹配的术语" /><el-pagination
          v-if="filteredTerms.length > 24"
          v-model:current-page="termPage"
          :page-size="24"
          :total="filteredTerms.length"
          layout="prev, pager, next, total"
      /></el-tab-pane>
    </el-tabs>
  </div>
</template>
<style scoped>
.knowledge-page {
  max-width: 1700px;
  margin: auto;
}
.knowledge-search {
  max-width: 650px;
  margin: 0 0 20px;
}
.library-layout {
  display: grid;
  grid-template-columns: 270px minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}
.reading-panel {
  background: white;
  border: 1px solid #e3eaf1;
  border-radius: 14px;
  padding: 28px;
  min-height: 480px;
  min-width: 0;
}
.document-heading {
  display: flex;
  gap: 20px;
  align-items: start;
  justify-content: space-between;
  border-bottom: 1px solid #e8edf3;
  padding-bottom: 20px;
}
.eyebrow {
  font-size: 12px;
  letter-spacing: 0.02em;
  color: #5a7792;
}
.document-heading h2 {
  font-size: 21px;
  line-height: 1.5;
  margin: 8px 0 12px;
}
.document-heading p,
.muted {
  font-size: 13px;
  color: #77869a;
}
.source-link {
  flex-shrink: 0;
  font-size: 14px;
  color: #245c9a;
}
.reading-tools {
  display: flex;
  gap: 12px;
  margin: 22px 0;
}
.reading-view {
  margin-top: 22px;
}
.reading-tools .el-input {
  max-width: 410px;
}
.versions {
  padding: 14px 18px;
  background: #f5f8fc;
  font-size: 13px;
  line-height: 2;
}
.clause {
  scroll-margin-top: 24px;
  padding: 24px 0;
  border-top: 1px solid #ecf0f4;
}
.clause.selected {
  background: #f1f7ff;
  box-shadow: 0 0 0 12px #f1f7ff;
}
.clause h3 {
  font-size: 16px;
  line-height: 1.6;
  margin: 0 0 18px;
}
.parallel {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 28px;
  font-size: 14px;
}
.parallel section {
  min-width: 0;
}
.parallel section + section {
  border-left: 1px solid #e8edf3;
  padding-left: 28px;
}
.parallel h4 {
  font-size: 12px;
  font-weight: 500;
  color: #8492a3;
  margin: 0 0 10px;
}
.topic-overview {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(235px, 1fr));
  gap: 20px;
  margin: 16px 0 30px;
}
.topic-overview-card {
  font: inherit;
  text-align: left;
  border: 1px solid #e3eaf1;
  border-radius: 14px;
  background: white;
  padding: 28px;
  cursor: pointer;
  transition: box-shadow 0.2s;
}
.topic-overview-card:hover {
  box-shadow: 0 8px 24px #263c4b0d;
}
.topic-overview-card h2 {
  font-size: 18px;
  margin: 0 0 16px;
  color: #263c4b;
}
.topic-overview-card p {
  font-size: 13px;
  color: #77869a;
}
.topic-overview-card span {
  display: block;
  color: #245c9a;
  font-size: 14px;
  margin-top: 24px;
}
.topic-filters {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
  flex-wrap: wrap;
  margin: 8px 0 24px;
}
.topic-grid,
.term-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 20px;
  margin-bottom: 24px;
}
.topic-card,
.term-card {
  background: white;
  border: 1px solid #e3eaf1;
  border-radius: 14px;
  padding: 24px;
  min-width: 0;
  transition:
    box-shadow 0.2s,
    transform 0.2s;
}
.topic-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 24px #263c4b0a;
}
.topic-card h2,
.term-card h2 {
  font-size: 17px;
  line-height: 1.6;
  margin: 10px 0;
}
.topic-document {
  font-size: 12px;
  color: #74869a;
  line-height: 1.6;
}
.topic-card blockquote {
  margin: 18px 0;
  padding-left: 14px;
  border-left: 2px solid #d8e5f2;
  font-size: 14px;
  line-height: 1.8;
  max-height: 145px;
  overflow: auto;
}
.term-card p,
.term-card details {
  font-size: 14px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.original-term {
  color: #617990;
}
.term-source {
  margin-top: 18px;
  font-size: 12px;
  color: #78899b;
  line-height: 1.8;
}
.term-source a {
  color: #245c9a;
}
summary {
  cursor: pointer;
}
.el-pagination {
  margin-top: 24px;
  justify-content: center;
}
@media (max-width: 1100px) {
  .topic-grid,
  .term-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .library-layout {
    grid-template-columns: 220px minmax(0, 1fr);
    gap: 16px;
  }
  .parallel {
    grid-template-columns: 1fr;
  }
  .parallel section + section {
    border: 0;
    padding: 16px 0 0;
  }
}
@media (max-width: 700px) {
  .library-layout,
  .topic-grid,
  .term-grid {
    grid-template-columns: 1fr;
  }
  .reading-panel {
    padding: 20px;
  }
  .document-heading {
    flex-direction: column;
  }
}
@media (prefers-reduced-motion: reduce) {
  .topic-card,
  .term-card {
    transition: none;
  }
  .topic-card:hover {
    transform: none;
  }
}
</style>
