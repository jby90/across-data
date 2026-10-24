<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { directoryLevels, documentPage, type DirectoryCountry, type DirectoryDocument } from '@cyshuhe/shared/src/regulationDirectory'

const props = defineProps<{
  countries: DirectoryCountry[]
  documents: DirectoryDocument[]
  country: string
  selectedId?: string
  level?: string
  loading?: boolean
}>()
const emit = defineEmits<{
  (e: 'country', code: string): void
  (e: 'level', key: string): void
  (e: 'document', id: string): void
}>()
const expandedCountry = ref(props.country)
const expandedLevel = ref(props.level || '')
const page = ref(1)
const levels = computed(() => directoryLevels(props.documents, props.country))
const currentLevel = computed(() => levels.value.find(g => g.key === expandedLevel.value))
const visibleFiles = computed(() => documentPage(currentLevel.value?.items || [], page.value))

function toggleCountry(code: string) {
  expandedCountry.value = expandedCountry.value === code ? '' : code
  if (code !== props.country) {
    expandedLevel.value = ''
    page.value = 1
    emit('country', code)
  }
}
function toggleLevel(key: string) {
  expandedLevel.value = expandedLevel.value === key ? '' : key
  page.value = 1
  if (expandedLevel.value) emit('level', key)
}
watch(() => props.country, code => {
  expandedCountry.value = code
  expandedLevel.value = ''
  page.value = 1
})
watch(() => props.level, key => { expandedLevel.value = key || ''; page.value = 1 })
watch(levels, () => { page.value = 1 })
watch([() => props.selectedId, levels], ([id]) => {
  const group = levels.value.find(g => g.items.some(item => item.id === id))
  if (!group) return
  expandedCountry.value = props.country
  expandedLevel.value = group.key
  page.value = Math.floor(group.items.findIndex(item => item.id === id) / 8) + 1
})
</script>

<template>
  <nav class="regulation-directory" aria-label="国家与文件层级目录" data-testid="regulation-directory">
    <div class="directory-heading"><strong>法规目录</strong><span>国家 / 文件层级 / 文件</span></div>
    <section v-for="c in countries" :key="c.code" class="directory-country">
      <button class="directory-folder country-folder" :aria-expanded="expandedCountry === c.code"
        :disabled="loading" @click="toggleCountry(c.code)">
        <span class="folder-arrow" aria-hidden="true">{{ expandedCountry === c.code ? '▾' : '▸' }}</span>
        <span>{{ c.name }} <small>{{ c.code }}</small></span><span class="folder-count">{{ c.count }} 份</span>
      </button>
      <div v-if="expandedCountry === c.code && country === c.code" class="directory-levels">
        <p v-if="loading" class="directory-hint" role="status">正在加载该国法规…</p>
        <p v-else-if="!levels.length" class="directory-hint">当前筛选下没有文件。</p>
        <section v-for="group in levels" :key="group.key">
          <button class="directory-folder level-folder" :aria-expanded="expandedLevel === group.key"
            :disabled="loading" @click="toggleLevel(group.key)">
            <span class="folder-arrow" aria-hidden="true">{{ expandedLevel === group.key ? '▾' : '▸' }}</span>
            <span>{{ group.name }}</span><span class="folder-count">{{ group.items.length }}</span>
          </button>
          <div v-if="expandedLevel === group.key" class="directory-files">
            <button v-for="item in visibleFiles.items" :key="item.id" class="directory-file"
              :class="{ selected: selectedId === item.id }" :aria-current="selectedId === item.id ? 'page' : undefined"
              :title="item.title" :data-testid="`directory-file-${item.id}`" @click="emit('document', item.id)">
              <span>{{ item.title }}</span>
              <small v-if="currentLevel?.items.some(other => other.id !== item.id && other.title === item.title)">同名文件 · {{ item.id.slice(0, 8) }}</small>
            </button>
            <div v-if="visibleFiles.pages > 1" class="directory-pagination" aria-label="目录文件分页">
              <button :disabled="visibleFiles.page <= 1" @click="page--">上一页文件</button>
              <span>{{ visibleFiles.page }} / {{ visibleFiles.pages }}</span>
              <button :disabled="visibleFiles.page >= visibleFiles.pages" @click="page++">下一页文件</button>
            </div>
          </div>
        </section>
      </div>
    </section>
    <p v-if="!countries.length" class="directory-hint">暂无可显示的国家与文件。</p>
    <p class="directory-hint">按已有文档类型分类；法律效力与有效状态请查看原文和复核结果。</p>
  </nav>
</template>

<style scoped>
.regulation-directory { background: white; border: 1px solid #e2e8f0; border-radius: 12px; padding: 16px; min-width: 0; }
.directory-heading { display: flex; flex-direction: column; gap: 6px; margin-bottom: 18px; }
.directory-heading strong { font-size: 16px; color: #263c4b; }
.directory-heading > span, .directory-hint { font-size: 12px; color: #738495; line-height: 1.8; }
.directory-country + .directory-country { margin-top: 6px; }
button { font: inherit; cursor: pointer; border: 0; background: transparent; text-align: left; color: #334b60; }
button:focus-visible { outline: 2px solid #2563eb; outline-offset: 2px; }
button:disabled { cursor: default; opacity: .5; }
.directory-folder { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 44px; padding: 10px 8px; border-radius: 8px; font-size: 14px; }
.country-folder { font-weight: 600; }
.directory-folder:hover { background: #f4f7fb; }
.folder-arrow { width: 12px; color: #8494a5; flex-shrink: 0; }
small { color: #8494a5; margin-left: 4px; font-size: 12px; }
.folder-count { color: #8494a5; font-size: 12px; margin-left: auto; white-space: nowrap; font-weight: 400; }
.directory-levels { margin-left: 12px; padding-left: 8px; border-left: 1px solid #e8edf3; }
.level-folder { font-size: 13px; }
.directory-files { padding: 4px 0 8px 12px; }
.directory-file { display: block; width: 100%; padding: 12px; margin: 4px 0; border-radius: 8px; line-height: 1.7; font-size: 13px; overflow-wrap: anywhere; transition: background .16s; }
.directory-file:hover { background: #f4f7fb; }
.directory-file.selected { background: #eef5ff; color: #1d4ed8; box-shadow: inset 3px 0 #3b82f6; }
.directory-file span { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; overflow: hidden; }
.directory-pagination { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 6px; font-size: 12px; color: #8494a5; padding: 8px 0; }
.directory-pagination button { min-height: 36px; padding: 4px; color: #2563eb; font-size: 12px; }
@media (prefers-reduced-motion: reduce) { .directory-file { transition: none; } }
</style>
