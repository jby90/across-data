<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import { useAuth } from '../auth'
const router = useRouter()
const auth = useAuth()
const cards = [
  {
    path: '/documents',
    icon: 'book',
    title: '法规文档',
    description: '查看官方原文、版本与条款，维护可靠的法规资料。',
    admin: false,
  },
  {
    path: '/review-queue',
    icon: 'check',
    title: '翻译与复核',
    description: '对照原文审定译文，管理术语与律所确认意见。',
    admin: false,
  },
  {
    path: '/knowledge-graph',
    icon: 'graph',
    title: '知识图谱',
    description: '探索法规、义务与数据类型之间的关系，追溯条款证据。',
    admin: false,
  },
  {
    path: '/agent-studio',
    icon: 'flow',
    title: '智能体工作台',
    description: '查看任务交接、执行过程和各环节产出。',
    admin: false,
  },
  {
    path: '/sites',
    icon: 'globe',
    title: '采集站点',
    description: '管理官方来源与更新计划，检查采集运行记录。',
    admin: true,
  },
  {
    path: '/orgs',
    icon: 'building',
    title: '企业管理',
    description: '管理企业组织，并在账号管理中维护人员与角色。',
    admin: true,
  },
]
const visibleCards = computed(() =>
  cards.filter((card) => !card.admin || auth.user?.role === 'platform_admin'),
)
</script>
<template>
  <div>
    <section class="welcome-hero">
      <div class="welcome-label">内容与平台运行</div>
      <h1>{{ auth.user?.display_name || '您好' }}，欢迎回来。</h1>
      <p>从官方资料到可追溯的合规依据，在一个工作空间内完成资料管理、内容复核与任务跟踪。</p>
      <el-button type="primary" @click="router.push('/review-queue')"
        ><AppIcon name="check" />进入复核工作台</el-button
      >
    </section>
    <div class="section-heading">
      <h2>从这里开始</h2>
      <span class="section-description">聚焦当前工作，快速进入常用功能</span>
    </div>
    <div class="feature-grid">
      <article v-for="card in visibleCards" :key="card.path" class="feature-card">
        <span class="feature-icon"><AppIcon :name="card.icon" /></span>
        <h3>{{ card.title }}</h3>
        <p>{{ card.description }}</p>
        <RouterLink :to="card.path">进入{{ card.title }}<AppIcon name="arrow" /></RouterLink>
      </article>
    </div>
    <div class="workflow-note">
      <strong>资料处理流程</strong>官方采集 → 解析与翻译 → 质量检查与重点确认 → 知识关联 → 分析与报告
    </div>
  </div>
</template>
