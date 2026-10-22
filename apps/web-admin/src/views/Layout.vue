<script setup lang="ts">
import { ROLE_LABEL } from '@cyshuhe/shared'
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuth } from '../auth'

const auth = useAuth()
const route = useRoute()
const router = useRouter()
const isPlatformAdmin = computed(() => auth.user?.role === 'platform_admin')

function logout() {
  auth.logout()
  router.replace({ name: 'login' })
}
</script>

<template>
  <el-container style="height: 100%">
    <el-aside width="210px" style="border-right: 1px solid #e4e7ed">
      <div class="brand">车研数合 · 管理端</div>
      <el-menu :default-active="route.path" router>
        <el-menu-item index="/">概览</el-menu-item>
        <el-menu-item index="/documents">法规文档库</el-menu-item>
        <el-menu-item index="/review-queue">术语与律所复核</el-menu-item>
        <el-menu-item index="/knowledge-graph">法规知识图谱</el-menu-item>
        <template v-if="isPlatformAdmin">
          <el-menu-item index="/sites">采集站点</el-menu-item>
          <el-menu-item index="/orgs">企业管理</el-menu-item>
          <el-menu-item index="/users">账号管理</el-menu-item>
        </template>
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
