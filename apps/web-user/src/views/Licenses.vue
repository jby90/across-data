<script setup lang="ts">
import { onMounted, ref } from 'vue'
const inventory = ref<{ scope: string; dependencies: Array<{ ecosystem: string; name: string; version: string; license: string; source: string }> } | null>(null)
const error = ref('')
onMounted(async () => {
  try {
    const response = await fetch(import.meta.env.BASE_URL + 'licenses.json')
    if (!response.ok) throw new Error('许可清单暂时无法读取')
    inventory.value = await response.json()
  } catch (e) { error.value = (e as Error).message }
})
</script>
<template>
  <main class="license-page">
    <RouterLink to="/">返回工作台</RouterLink>
    <h1>开源许可声明</h1>
    <p>本平台采用开源组件。组件及字体版权归各自作者所有，未修改的组件继续遵守原有许可证。</p>
    <p>{{ inventory?.scope }}</p>
    <p><a :href="`${$router.options.history.base || ''}/THIRD-PARTY-NOTICES.txt`">查看版权声明与许可全文</a></p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="inventory">已登记 {{ inventory.dependencies.length }} 项依赖；字体使用自托管 WOFF2。</p>
    <table v-if="inventory"><thead><tr><th>组件</th><th>版本</th><th>许可证</th></tr></thead>
      <tbody><tr v-for="item in inventory.dependencies" :key="item.ecosystem + item.name + item.version">
        <td><a :href="item.source" target="_blank" rel="noopener noreferrer">{{ item.name }}</a></td><td>{{ item.version }}</td><td>{{ item.license }}</td>
      </tr></tbody></table>
  </main>
</template>
<style scoped>
.license-page { max-width: 1000px; margin: 40px auto; padding: 24px; line-height: 1.7; }
h1 { font-size: 26px; } table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { padding: 10px; border-bottom: 1px solid #e7ebef; text-align: left; }
a { color: #315fe8; } p { color: #64748b; }
</style>
