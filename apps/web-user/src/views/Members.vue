<script setup lang="ts">
import { ROLE_LABEL, type Role, type UserOut } from '@cyshuhe/shared'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { api } from '../api'

const members = ref<UserOut[]>([])
const dialog = ref(false)
const form = reactive({ email: '', display_name: '', password: '', role: 'member' as Role })

async function load() {
  members.value = await api.get<UserOut[]>('/user/members')
}

async function submit() {
  try {
    await api.post('/user/members', { ...form })
    ElMessage.success('已添加')
    dialog.value = false
    Object.assign(form, { email: '', display_name: '', password: '', role: 'member' })
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

onMounted(load)
</script>

<template>
  <div style="display: flex; justify-content: space-between; margin-bottom: 12px">
    <h3 style="margin: 0">成员管理</h3>
    <el-button type="primary" @click="dialog = true">添加成员</el-button>
  </div>
  <el-table :data="members" border>
    <el-table-column prop="display_name" label="姓名" />
    <el-table-column prop="email" label="邮箱" />
    <el-table-column label="角色">
      <template #default="{ row }">{{ ROLE_LABEL[row.role as Role] }}</template>
    </el-table-column>
    <el-table-column label="状态">
      <template #default="{ row }">{{ row.is_active ? '正常' : '已停用' }}</template>
    </el-table-column>
  </el-table>

  <el-dialog v-model="dialog" title="添加成员" width="420px">
    <el-form label-position="top">
      <el-form-item label="姓名"><el-input v-model="form.display_name" /></el-form-item>
      <el-form-item label="邮箱"><el-input v-model="form.email" /></el-form-item>
      <el-form-item label="初始密码(至少 12 位)">
        <el-input v-model="form.password" type="password" show-password />
      </el-form-item>
      <el-form-item label="角色">
        <el-radio-group v-model="form.role">
          <el-radio value="member">企业成员</el-radio>
          <el-radio value="org_admin">企业管理员</el-radio>
        </el-radio-group>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialog = false">取消</el-button>
      <el-button type="primary" @click="submit">确定</el-button>
    </template>
  </el-dialog>
</template>