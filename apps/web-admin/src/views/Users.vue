<script setup lang="ts">
import { ROLE_LABEL, type OrgOut, type Role, type UserOut } from '@cyshuhe/shared'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../api'

const users = ref<UserOut[]>([])
const orgs = ref<OrgOut[]>([])
const dialog = ref(false)
const form = reactive({
  email: '',
  display_name: '',
  password: '',
  role: 'org_admin' as Role,
  org_id: '' as string,
})

const needsOrg = computed(() => form.role === 'org_admin' || form.role === 'member')
const orgName = (id: string | null) => orgs.value.find((o) => o.id === id)?.name ?? '—'

async function load() {
  ;[users.value, orgs.value] = await Promise.all([
    api.get<UserOut[]>('/admin/users'),
    api.get<OrgOut[]>('/admin/orgs'),
  ])
}

async function submit() {
  try {
    await api.post('/admin/users', {
      email: form.email.trim(),
      display_name: form.display_name.trim(),
      password: form.password,
      role: form.role,
      org_id: needsOrg.value ? form.org_id || null : null,
    })
    ElMessage.success('已创建')
    dialog.value = false
    Object.assign(form, {
      email: '',
      display_name: '',
      password: '',
      org_id: '',
    })
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

onMounted(load)
</script>

<template>
  <div class="management-page">
    <div class="page-heading">
      <div>
        <h1>账号管理</h1>
        <p>维护平台与企业账号，按职责分配访问角色。</p>
      </div>
      <el-button type="primary" @click="dialog = true">创建账号</el-button>
    </div>
    <el-table :data="users" border>
      <el-table-column prop="display_name" label="姓名" />
      <el-table-column prop="email" label="邮箱" min-width="230" />
      <el-table-column label="角色">
        <template #default="{ row }">{{ ROLE_LABEL[row.role as Role] }}</template>
      </el-table-column>
      <el-table-column label="所属企业">
        <template #default="{ row }">{{ orgName(row.org_id) }}</template>
      </el-table-column>
      <el-table-column label="状态">
        <template #default="{ row }"
          ><el-tag :type="row.is_active ? 'success' : 'info'" effect="plain">{{
            row.is_active ? '正常' : '已停用'
          }}</el-tag></template
        >
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialog" title="创建账号" width="440px">
      <el-form label-position="top">
        <el-form-item label="姓名"><el-input v-model="form.display_name" /></el-form-item>
        <el-form-item label="邮箱"><el-input v-model="form.email" /></el-form-item>
        <el-form-item label="初始密码(至少 12 位)">
          <el-input v-model="form.password" type="password" show-password />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" style="width: 100%">
            <el-option v-for="(label, key) in ROLE_LABEL" :key="key" :label="label" :value="key" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="needsOrg" label="所属企业">
          <el-select v-model="form.org_id" style="width: 100%">
            <el-option v-for="o in orgs" :key="o.id" :label="o.name" :value="o.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" @click="submit">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>
