<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { LawGroup } from '@cyshuhe/shared/src/graphGrouping'

const props = defineProps<{
  groups: LawGroup[]
  selectedGroupId: string
  total: number
  filtered: number
  shown: number
  shownGroups: number
  page: number
  pages: number
}>()
const emit = defineEmits<{
  (e: 'select', id: string): void
  (e: 'page', page: number): void
}>()
const search = ref('')
const directoryPage = ref(1)
const matched = computed(() => props.groups.filter(g => g.name.toLowerCase().includes(search.value.trim().toLowerCase())))
const directoryPages = computed(() => Math.max(1, Math.ceil(matched.value.length / 12)))
const directory = computed(() => matched.value.slice((directoryPage.value - 1) * 12, directoryPage.value * 12))
const selected = computed(() => props.groups.find(g => g.id === props.selectedGroupId))
watch([search, () => props.groups], () => { directoryPage.value = 1 })
</script>

<template>
  <section class="law-browser" aria-label="按法规浏览图谱">
    <div class="law-counts" data-testid="kg-display-counts" aria-live="polite">
      <span>全部关系 <strong>{{ total }}</strong></span>
      <span>筛选后 <strong>{{ filtered }}</strong></span>
      <span>当前展示 <strong>{{ shown }}</strong> 条 · {{ shownGroups }} 组连线</span>
    </div>
    <p class="law-hint">关系数按原始证据记录计数；同起点、终点及类型的记录合并为连线。分组仅用于浏览，复核状态沿用原记录。</p>
    <template v-if="!selected">
      <div class="law-toolbar">
        <div><strong>法规目录</strong><span class="law-muted">{{ groups.length }} 组 · 点击法规展开关系</span></div>
        <input v-model="search" aria-label="搜索图谱法规" placeholder="搜索法规名称" type="search" />
      </div>
      <div class="law-grid" data-testid="kg-law-directory">
        <button v-for="group in directory" :key="group.id" class="law-card" @click="emit('select', group.id)">
          <span class="law-name">{{ group.name }}</span>
          <span v-if="groups.some(other => other.id !== group.id && other.name === group.name)" class="law-hint">同名文件 · {{ group.id.slice(0, 8) }}</span>
          <span class="law-card-footer">{{ group.edges.length }} 条关系 <span>展开 →</span></span>
        </button>
      </div>
      <p v-if="!directory.length" class="law-hint">没有匹配的法规，请调整搜索或筛选条件。</p>
      <div v-if="directoryPages > 1" class="law-pagination">
        <button :disabled="directoryPage === 1" @click="directoryPage--">上一页法规</button>
        <span>第 {{ directoryPage }} / {{ directoryPages }} 页 · {{ matched.length }} 组</span>
        <button :disabled="directoryPage === directoryPages" @click="directoryPage++">下一页法规</button>
      </div>
    </template>
    <template v-else>
      <div class="law-toolbar">
        <div class="law-selection"><button @click="emit('select', '')">← 返回法规目录</button><h3>{{ selected.name }}</h3></div>
        <span class="law-muted">本法规 {{ selected.edges.length }} 条关系</span>
      </div>
      <slot />
      <div class="law-pagination" data-testid="kg-relation-pagination">
        <button :disabled="page <= 1" @click="emit('page', page - 1)">上一页关系</button>
        <span>第 {{ page }} / {{ pages }} 页 · 每页最多 36 组连线</span>
        <button :disabled="page >= pages" @click="emit('page', page + 1)">下一页关系</button>
      </div>
    </template>
    <p class="law-hint">跨法规关系可在双方分组中查看，全部关系数按记录去重。目录未展开时不绘制连线；完整证据可在证据明细中查看。</p>
  </section>
</template>

<style scoped>
.law-browser { padding: 20px; }
.law-counts { display: flex; flex-wrap: wrap; gap: 12px 32px; color: #526174; font-size: 14px; }
.law-counts strong { color: #172c43; font-size: 22px; margin-left: 6px; }
.law-hint, .law-muted { color: #64748b; font-size: 13px; line-height: 1.7; }
.law-hint { margin: 12px 0 18px; }
.law-toolbar { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 16px; margin: 20px 0 16px; }
.law-muted { margin-left: 12px; }
input { width: min(100%, 280px); min-height: 40px; border: 1px solid #d8e1eb; border-radius: 8px; padding: 8px 12px; font: inherit; background: white; }
.law-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 250px), 1fr)); gap: 16px; }
button { font: inherit; cursor: pointer; color: #244766; background: white; border: 1px solid #d8e1eb; border-radius: 8px; padding: 8px 12px; min-height: 40px; }
button:focus-visible, input:focus-visible { outline: 2px solid #2563eb; outline-offset: 3px; }
.law-card { display: flex; flex-direction: column; gap: 24px; justify-content: space-between; text-align: left; padding: 20px; transition: border-color .18s, background .18s; }
.law-card:hover { border-color: #3b82f6; background: #f5f9ff; }
.law-name { font-size: 15px; font-weight: 600; line-height: 1.6; overflow-wrap: anywhere; }
.law-card-footer { display: flex; justify-content: space-between; color: #64748b; font-size: 13px; }
.law-card-footer span { color: #2563eb; }
.law-selection { display: flex; align-items: center; flex-wrap: wrap; gap: 16px; min-width: 0; }
h3 { margin: 0; font-size: 17px; line-height: 1.6; overflow-wrap: anywhere; flex: 1; min-width: 180px; }
.law-pagination { display: flex; justify-content: center; align-items: center; gap: 16px; margin-top: 20px; flex-wrap: wrap; font-size: 13px; color: #64748b; }
button:disabled { opacity: .45; cursor: default; }
@media (max-width: 640px) { .law-browser { padding: 14px; } .law-counts { gap: 12px; } .law-counts strong { font-size: 19px; } }
@media (prefers-reduced-motion: reduce) { .law-card { transition: none; } }
</style>
