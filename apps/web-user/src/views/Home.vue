<template>
  <h3>欢迎使用车研数合</h3>
  <el-row :gutter="16">
    <el-col :span="8">
      <el-card shadow="never">
        <template #header>安全报告</template>
        回答几个问题,由多个智能体协作生成八维度合规报告。(建设中)
      </el-card>
    </el-col>
    <el-col :span="8">
      <el-card shadow="never">
        <template #header>申请表</template>
        选择出发国与目的国,自动判定所需申报表并引导填写。(建设中)
      </el-card>
    </el-col>
    <el-col :span="8">
      <el-card shadow="never">
        <template #header>法规查询</template>
        查看各国法规原文、中文译文与适用关系。(建设中)
      </el-card>
    </el-col>
  </el-row>
</template>