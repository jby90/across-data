<script setup lang="ts">
import { useRouter } from 'vue-router'

const router = useRouter()
</script>

<template>
  <div>
    <h3>车研数合 · 平台管理与内容运维中心</h3>
    <el-row :gutter="16">
      <el-col :span="8">
        <el-card shadow="hover">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <strong>法规采集与知识图谱 (P1/P2)</strong>
              <el-tag size="small" type="success">已上线</el-tag>
            </div>
          </template>
          <p style="color: #475569; font-size: 13px; line-height: 1.6">
            五国官方站点定时抓取、PDF/OCR 解析、六要素十二关系 ECharts 力导向图谱、质量体检视图与八项结构化属性管理。
          </p>
          <el-button type="primary" plain size="small" @click="router.push('/knowledge-graph')">
            查看法规知识图谱
          </el-button>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <strong>术语库与律所三层复核 (P2)</strong>
              <el-tag size="small" type="success">已上线</el-tag>
            </div>
          </template>
          <p style="color: #475569; font-size: 13px; line-height: 1.6">
            两层法律术语表、机器翻译质量门禁、解读要点确认单及面向外部合作律所的离线复核包一键导出/回写。
          </p>
          <el-button type="primary" plain size="small" @click="router.push('/review-queue')">
            进入术语与律所复核
          </el-button>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <strong>Dify 风格多 Agent 编排工作台 (P3)</strong>
              <el-tag size="small" type="success">已上线 · 只读锁定</el-tag>
            </div>
          </template>
          <p style="color: #475569; font-size: 13px; line-height: 1.6">
            可视化查看 7 节点多智能体工作流拓扑、连接关系、只读参数与代码入口、任务执行轨迹，并支持编排配置 JSON DSL 导入导出。
          </p>
          <el-button type="primary" size="small" @click="router.push('/agent-studio')">
            进入多Agent编排工作台
          </el-button>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
