<script setup lang="ts">
import type { OrgOut } from '@cyshuhe/shared'
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { api } from '../api'

const orgs = ref<OrgOut[]>([])
const name = ref('')
const editing = ref<OrgOut | null>(null)
const termsText = ref('')
const saving = ref(false)

function editTerms(org: OrgOut) {
  editing.value = org
  termsText.value = (org.llm_redaction_terms || []).join('\n')
}

async function saveTerms() {
  if (!editing.value || saving.value) return
  saving.value = true
  try {
    await api.patch(`/admin/orgs/${editing.value.id}/redaction`, {
      terms: termsText.value.split('\n').map(s => s.trim()).filter(Boolean),
    })
    editing.value = null
    await load()
    ElMessage.success('已保存企业脱敏词，新的模型调用将使用此配置')
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally { saving.value = false }
}

async function load() {
  orgs.value = await api.get<OrgOut[]>('/admin/orgs')
}

async function create() {
  if (!name.value.trim()) return
  try {
    await api.post('/admin/orgs', { name: name.value.trim() })
    name.value = ''
    ElMessage.success('已创建')
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

onMounted(load)
</script>

<template>
  <div class="management-page">
    <header class="page-heading">
      <div>
        <h1>企业管理</h1>
        <p>维护企业组织，账号与成员按企业隔离管理。</p>
      </div>
    </header>
    <div class="toolbar">
      <el-input
        v-model="name"
        placeholder="输入新企业名称"
        aria-label="新企业名称"
        @keyup.enter="create"
      />
      <el-button type="primary" @click="create">创建企业</el-button>
    </div>
    <el-table :data="orgs" border>
      <el-table-column prop="name" label="企业名称" />
      <el-table-column label="状态">
        <template #default="{ row }"
          ><el-tag :type="row.is_active ? 'success' : 'info'" effect="plain">{{
            row.is_active ? '正常' : '已停用'
          }}</el-tag></template
        >
      </el-table-column>
      <el-table-column label="模型出站脱敏" min-width="200">
        <template #default="{ row }">
          <span>企业名自动保护 · {{ (row.llm_redaction_terms || []).length }} 个自定义词</span>
          <el-button link type="primary" @click="editTerms(row)">配置脱敏词</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-dialog :model-value="!!editing" title="企业模型出站脱敏词" width="520px"
      @close="editing = null">
      <p>企业名称自动加入。项目代号、客户名称等每行一个；仅改变发给模型的内容，原始记录保留。</p>
      <el-input v-model="termsText" type="textarea" :rows="6" aria-label="企业脱敏词"
        placeholder="每行一个，2–200 个字符，最多 100 项" />
      <template #footer>
        <el-button @click="editing = null">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveTerms">保存脱敏词</el-button>
      </template>
    </el-dialog>
  </div>
</template>
