<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
interface Definition {
  id: string
  source_term: string
  target_zh: string
  source_label: string
  source_kind: string
  source_reference: string
  source_url: string | null
  definition_original: string | null
  definition_zh: string | null
  verification_status: string
}
interface Ambiguity {
  id: string
  country_code: string
  title: string
  question: string
  impact: string
  status: string
}
const props = defineProps<{ country: string }>()
const registerVisible = ref(false),
  saving = ref(false)
const registration = reactive({
  clause_id: '',
  source_term: '',
  target_zh: '',
  quote: '',
  source_kind: 'law_definition',
  applies_to_document_id: '',
})
async function register() {
  saving.value = true
  try {
    await api.post('/admin/knowledge/terminology/references', {
      ...registration,
      country_code: registration.source_kind === 'international_reference' ? null : props.country,
      applies_to_document_id: registration.applies_to_document_id || null,
    })
    ElMessage.success('已登记来源定义与译名')
    registerVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    saving.value = false
  }
}
const items = ref<Definition[]>([]),
  points = ref<Ambiguity[]>([]),
  busy = ref(false),
  search = ref(''),
  page = ref(1)
const visible = computed(() =>
  items.value.filter((t) =>
    `${t.source_term} ${t.target_zh} ${t.source_reference}`
      .toLowerCase()
      .includes(search.value.toLowerCase()),
  ),
)
async function load() {
  busy.value = true
  try {
    const [d, p] = await Promise.all([
      api.get<{ items: Definition[] }>(
        `/admin/knowledge/terminology?country_code=${props.country || 'SG'}`,
      ),
      api.get<Ambiguity[]>('/admin/knowledge/terminology/ambiguities'),
    ])
    items.value = d.items
    points.value = p.filter((x) => !props.country || x.country_code === props.country)
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    busy.value = false
  }
}
async function rebuild() {
  busy.value = true
  try {
    const r = await api.post<{ definitions_added: number }>(
      '/admin/knowledge/terminology/rebuild',
      { country_code: props.country || null },
    )
    ElMessage.success(`新增 ${r.definitions_added} 条来源定义`)
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    busy.value = false
  }
}
watch(
  () => props.country,
  () => {
    page.value = 1
    void load()
  },
  { immediate: true },
)
</script>
<template>
  <section v-loading="busy" class="terminology-library">
    <header>
      <div>
        <h3>来源定义库</h3>
        <p>法律自身定义 → 监管说明 → 官方语种对照 → 国际参考。来源校验与人工法律审批分别记录。</p>
      </div>
      <div>
        <el-button @click="registerVisible = true">补充权威定义</el-button
        ><el-button type="primary" @click="rebuild">更新来源定义</el-button>
      </div>
    </header>
    <el-input
      v-model="search"
      placeholder="搜索术语、译名或依据"
      clearable
      style="max-width: 460px; margin: 12px 0 20px"
      @input="page = 1"
    />
    <el-table :data="visible.slice((page - 1) * 20, page * 20)" border>
      <el-table-column label="术语与译名" min-width="200"
        ><template #default="{ row }"
          ><strong>{{ row.target_zh }}</strong>
          <div dir="auto">{{ row.source_term }}</div></template
        ></el-table-column
      >
      <el-table-column label="来源解释" min-width="300"
        ><template #default="{ row }"
          ><p v-if="row.definition_zh">{{ row.definition_zh }}</p>
          <details v-if="row.definition_original">
            <summary>来源定义原文</summary>
            <p dir="auto">{{ row.definition_original }}</p>
          </details>
          <span v-else>通用译名，不附加本国法律定义</span></template
        ></el-table-column
      >
      <el-table-column label="来源与适用范围" min-width="230"
        ><template #default="{ row }"
          ><el-tag size="small">{{ row.source_label }}</el-tag>
          <p>{{ row.source_reference }}</p>
          <a v-if="row.source_url" :href="row.source_url" target="_blank" rel="noopener noreferrer"
            >查看官方来源</a
          ></template
        ></el-table-column
      >
      <el-table-column label="自动检查" width="150"
        ><template #default="{ row }">{{
          row.verification_status === 'source_matched' ? '原文匹配' : '译名规范'
        }}</template></el-table-column
      >
    </el-table>
    <el-pagination
      v-if="visible.length > 20"
      v-model:current-page="page"
      :total="visible.length"
      :page-size="20"
      layout="prev, pager, next, total"
    />
    <h3>影响结论的术语歧义</h3>
    <el-empty v-if="!points.length" description="暂无单独登记的术语歧义" :image-size="60" />
    <el-table v-else :data="points" border
      ><el-table-column prop="title" label="术语" min-width="170" /><el-table-column
        prop="question"
        label="需要确认的问题"
        min-width="260"
      /><el-table-column prop="impact" label="对结论的影响" min-width="260" /><el-table-column
        label="状态"
        width="130"
        ><template #default="{ row }">{{
          row.status === 'confirmed' ? '已确认' : '待核对'
        }}</template></el-table-column
      ></el-table
    >
    <el-dialog v-model="registerVisible" title="登记来源定义与译名" width="650px" append-to-body>
      <el-form label-position="top">
        <el-form-item label="来源层级"
          ><el-select v-model="registration.source_kind"
            ><el-option label="法律自身定义" value="law_definition" /><el-option
              label="监管指南与问答"
              value="regulator_guidance" /><el-option
              label="官方其他语种版本"
              value="official_parallel" /><el-option
              label="国际参考"
              value="international_reference" /></el-select
        ></el-form-item>
        <el-form-item label="来源条款 ID"
          ><el-input v-model="registration.clause_id" placeholder="从已归档官方条款复制 ID"
        /></el-form-item>
        <el-form-item label="原词"><el-input v-model="registration.source_term" /></el-form-item>
        <el-form-item label="规定中文译法"
          ><el-input v-model="registration.target_zh"
        /></el-form-item>
        <el-form-item label="完整定义原文"
          ><el-input
            v-model="registration.quote"
            type="textarea"
            :rows="5"
            placeholder="逐字粘贴来源定义"
        /></el-form-item>
        <el-form-item
          v-if="registration.source_kind === 'official_parallel'"
          label="对应法规文件 ID"
          ><el-input v-model="registration.applies_to_document_id"
        /></el-form-item>
      </el-form>
      <template #footer
        ><el-button @click="registerVisible = false">取消</el-button
        ><el-button type="primary" :loading="saving" @click="register">登记</el-button></template
      >
    </el-dialog>
  </section>
</template>
<style scoped>
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  flex-wrap: wrap;
}
h3 {
  font-size: 17px;
  margin: 12px 0;
}
p {
  font-size: 13px;
  line-height: 1.8;
  color: #677b8e;
  margin: 8px 0;
}
details {
  font-size: 14px;
  line-height: 1.8;
}
summary {
  cursor: pointer;
}
.el-pagination {
  margin: 18px 0 28px;
  justify-content: center;
}
</style>
