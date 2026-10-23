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
    </el-table>
  </div>
</template>
