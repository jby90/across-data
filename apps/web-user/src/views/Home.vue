<script setup lang="ts">
import { useRouter } from 'vue-router'

const router = useRouter()
</script>

<template>
  <div>
    <h3>欢迎使用车研数合 · 汽车数据跨境合规平台</h3>
    <el-row :gutter="16">
      <el-col :span="8">
        <el-card shadow="hover">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <strong>多智能体八维度安全报告 (M2)</strong>
              <el-tag size="small" type="success">已上线</el-tag>
            </div>
          </template>
          <p style="color: #475569; font-size: 13px; line-height: 1.6">
            基于结构化信息采集追问规范，由多个智能体协作对照目标国有效法条（含质检退回补证），输出整份及可按维度拆分的八维度合规评估报告，支持 Word/PDF 导出。
          </p>
          <el-button type="primary" size="small" @click="router.push('/analysis')">
            进入合规分析与八维报告
          </el-button>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <strong>五国法规知识库与合规图谱 (M1)</strong>
              <el-tag size="small" type="success">已上线</el-tag>
            </div>
          </template>
          <p style="color: #475569; font-size: 13px; line-height: 1.6">
            浏览新加坡、埃及、突尼斯、摩洛哥、黎巴嫩官方原文与术语受控译文双栏对照，交互式探索六要素十二关系 ECharts 合规图谱与八项结构化属性。
          </p>
          <el-button type="primary" plain size="small" @click="router.push('/knowledge')">
            进入法规知识库与合规地图
          </el-button>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <strong>跨境申请表识别与制表 (M3)</strong>
              <el-tag size="small" type="info">下一阶段 (P4)</el-tag>
            </div>
          </template>
          <p style="color: #64748b; font-size: 13px; line-height: 1.6">
            根据出发国、目的国与业务场景动态判定所需申报表模板清单，通过问答收集字段并导出填报文件。
          </p>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
