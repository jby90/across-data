<template>
  <h3>管理端</h3>
  <el-row :gutter="16">
    <el-col :span="8">
      <el-card shadow="never">
        <template #header>采集与知识库</template>
        站点配置、定时抓取、解析翻译流水线、知识图谱。(建设中)
      </el-card>
    </el-col>
    <el-col :span="8">
      <el-card shadow="never">
        <template #header>复核</template>
        术语库与解读要点确认单,供律所复核。(建设中)
      </el-card>
    </el-col>
    <el-col :span="8">
      <el-card shadow="never">
        <template #header>模板与规则</template>
        国别申请表模板库与适用规则。(建设中)
      </el-card>
    </el-col>
  </el-row>
</template>