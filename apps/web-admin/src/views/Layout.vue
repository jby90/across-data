<script setup lang="ts">
import { ROLE_LABEL } from '@cyshuhe/shared'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import { useAuth } from '../auth'
const auth = useAuth()
const route = useRoute()
const router = useRouter()
const navOpen = ref(false)
const groups = [
  {
    label: '工作空间',
    items: [{ path: '/', title: '工作概览', icon: 'grid' }],
  },
  {
    label: '法规与知识',
    items: [
      { path: '/documents', title: '法规文档', icon: 'book' },
      { path: '/review-queue', title: '翻译与复核', icon: 'check' },
      { path: '/knowledge-graph', title: '知识图谱', icon: 'graph' },
    ],
  },
  {
    label: '运行与管理',
    items: [
      { path: '/agent-studio', title: '智能体工作台', icon: 'flow' },
      { path: '/updates', title: '更新与时效', icon: 'check' },
      { path: '/sites', title: '采集站点', icon: 'globe', admin: true },
      { path: '/orgs', title: '企业管理', icon: 'building', admin: true },
      { path: '/users', title: '账号管理', icon: 'users', admin: true },
    ],
  },
]
const canManage = computed(() => auth.user?.role === 'platform_admin')
const navigation = computed(() =>
  groups
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => !('admin' in item) || canManage.value),
    }))
    .filter((group) => group.items.length),
)
const current = computed(() =>
  navigation.value.flatMap((group) => group.items).find((item) => item.path === route.path),
)
watch(
  () => route.path,
  () => {
    navOpen.value = false
  },
)
function logout() {
  auth.logout()
  router.replace({ name: 'login' })
}
</script>
<template>
  <div class="app-shell" :class="{ 'is-nav-open': navOpen }" @keydown.esc="navOpen = false">
    <a class="skip-link" href="#main-content">跳转至主要内容</a>
    <button v-if="navOpen" class="nav-scrim" aria-label="关闭导航" @click="navOpen = false" />
    <aside class="app-sidebar" aria-label="主导航">
      <div class="app-brand">
        <span class="brand-mark"
          ><svg viewBox="0 0 28 28" fill="none" aria-hidden="true">
            <path
              d="M5 9l9-5 9 5v10l-9 5-9-5V9z M5 9l9 5 9-5 M14 14v10"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linejoin="round"
            /></svg
        ></span>
        <div>
          <div class="brand-name">车研数合</div>
          <div class="brand-caption">DATA COMPLIANCE</div>
        </div>
      </div>
      <nav>
        <section v-for="group in navigation" :key="group.label" class="nav-group">
          <div class="nav-group-label">{{ group.label }}</div>
          <RouterLink
            v-for="item in group.items"
            :key="item.path"
            :to="item.path"
            class="nav-link"
            :class="{ 'is-active': route.path === item.path }"
            :aria-current="route.path === item.path ? 'page' : undefined"
            ><AppIcon :name="item.icon" /><span>{{ item.title }}</span></RouterLink
          >
        </section>
      </nav>
      <div class="sidebar-footer"><strong>管理工作台</strong>汽车数据跨境合规平台<br /><RouterLink to="/licenses">开源许可声明</RouterLink></div>
    </aside>
    <div class="app-body">
      <header class="app-topbar">
        <div class="topbar-context">
          <button
            class="mobile-nav-button"
            :aria-expanded="navOpen"
            aria-label="展开导航"
            @click="navOpen = !navOpen"
          >
            <AppIcon name="menu" /></button
          ><span>管理工作台</span><span class="topbar-divider">/</span
          ><strong>{{ current?.title || '工作空间' }}</strong>
        </div>
        <div class="topbar-account" v-if="auth.user">
          <span class="account-avatar">{{ auth.user.display_name.slice(0, 1) }}</span
          ><span>{{ auth.user.display_name }}</span
          ><span
            v-if="auth.user.display_name !== ROLE_LABEL[auth.user.role]"
            class="account-role"
            >{{ ROLE_LABEL[auth.user.role] }}</span
          ><el-dropdown trigger="click"
            ><el-button text aria-label="账号操作">···</el-button
            ><template #dropdown
              ><el-dropdown-menu
                ><el-dropdown-item @click="logout"
                  ><AppIcon name="logout" />退出登录</el-dropdown-item
                ></el-dropdown-menu
              ></template
            ></el-dropdown
          >
        </div>
      </header>
      <main id="main-content" class="app-content" tabindex="-1">
        <p class="demo-notice">演示环境，数据仅供参考；机器内容及人工复核状态请逐条核对。</p>
        <RouterView v-slot="{ Component }"
          ><div :key="route.path" class="app-page">
            <component :is="Component" /></div
        ></RouterView>
      </main>
    </div>
  </div>
</template>
<style scoped>
.skip-link {
  position: fixed;
  left: 250px;
  top: -60px;
  z-index: 100;
  padding: 10px 18px;
  background: white;
  border: 2px solid var(--cy-blue);
  border-radius: 8px;
}
.skip-link:focus {
  top: 10px;
}
</style>
