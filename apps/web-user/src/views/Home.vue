<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import { useAuth } from '../auth'
const router = useRouter()
const auth = useAuth()
const cards = [
  {
    path: '/analysis',
    icon: 'shield',
    title: '合规评估',
    description: '通过问答梳理业务场景，生成有条款依据的八维度评估报告。',
    admin: false,
  },
  {
    path: '/knowledge',
    icon: 'book',
    title: '法规知识库',
    description: '检索法规、对照原文和译文，查看知识关系与复核状态。',
    admin: false,
  },
]
const visibleCards = computed(() =>
  cards.filter((card) => !card.admin || auth.user?.role === 'platform_admin'),
)
</script>
<template>
  <div>
    <section class="welcome-hero">
      <div class="welcome-label">汽车数据跨境合规工作空间</div>
      <h1>{{ auth.user?.display_name || '您好' }}，欢迎回来。</h1>
      <p>把业务问题交给评估助手，让法规证据、风险判断与下一步行动清晰呈现。</p>
      <el-button type="primary" @click="router.push('/analysis')"
        ><AppIcon name="plus" />开始合规评估</el-button
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
      <article class="feature-card is-planned">
        <span class="feature-icon"><AppIcon name="file" /></span>
        <h3>跨境申请表</h3>
        <p>按业务场景识别申报材料、收集填报信息。此功能将在后续阶段提供。</p>
        <el-tag type="info" effect="plain">规划中</el-tag>
      </article>
    </div>
    <div class="workflow-note">
      <strong>评估流程</strong>描述业务 → 补充信息 → 对照法规 → 查看报告
    </div>
  </div>
</template>
