<script setup lang="ts">
import type { OrgOut } from '@cyshuhe/shared'
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { api } from '../api'

const orgs = ref<OrgOut[]>([])
const name = ref('')

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
  <h3>企业管理</h3>
  <div style="display: flex; gap: 8px; margin-bottom: 12px; max-width: 420px">
    <el-input v-model="name" placeholder="新企业名称" @keyup.enter="create" />
    <el-button type="primary" @click="create">创建</el-button>
  </div>
  <el-table :data="orgs" border>
    <el-table-column prop="name" label="企业名称" />
    <el-table-column label="状态">
      <template #default="{ row }">{{ row.is_active ? '正常' : '已停用' }}</template>
    </el-table-column>
  </el-table>
</template>