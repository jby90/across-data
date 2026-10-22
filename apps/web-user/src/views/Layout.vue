<script setup lang="ts">
import { ROLE_LABEL } from '@cyshuhe/shared'
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuth } from '../auth'

const auth = useAuth()
const route = useRoute()
const router = useRouter()
const isOrgAdmin = computed(() => auth.user?.role === 'org_admin')

function logout() {
  auth.logout()
  router.replace({ name: 'login' })
}
</script>

<template>
  <el-container style="height: 100%">
    <el-aside width="200px" style="border-right: 1px solid #e4e7ed">
      <div class="brand">车研数合</div>
      <el-menu :default-active="route.path" router>
        <el-menu-item index="/">概览</el-menu-item>
        <el-menu-item index="/knowledge">法规知识库</el-menu-item>
        <el-menu-item v-if="isOrgAdmin" index="/members">成员管理</el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <span v-if="auth.user">
          {{ auth.user.display_name }}({{ ROLE_LABEL[auth.user.role] }})
        </span>
        <el-button link @click="logout">退出登录</el-button>
      </el-header>
      <el-main><router-view /></el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.brand {
  padding: 16px 20px;
  font-weight: 600;
  font-size: 18px;
}
.header {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  border-bottom: 1px solid #e4e7ed;
}
</style>
