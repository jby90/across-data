<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import {
  mergeReaderPage,
  readerLabel,
  readerNeighborhood,
  readerPositions,
  readerRoot,
  readerTypes,
} from '@cyshuhe/shared/src/readerRelations'
import type { ReaderPayload } from '@cyshuhe/shared/src/readerRelations'
import { api } from '../api'

echarts.use([GraphChart, TooltipComponent, CanvasRenderer])
const props = defineProps<{ documentId: string; versionId: string | null }>()
const emit = defineEmits<{ basis: [clauseId: string] }>()
const payload = ref<ReaderPayload | null>(null),
  loading = ref(false),
  error = ref('')
const focus = ref(''),
  selected = ref(''),
  listPage = ref(1),
  nodeLimit = ref(15)
const host = ref<HTMLDivElement>()
let chart: echarts.ECharts | undefined,
  observer: ResizeObserver | undefined,
  request = 0
const view = computed(() =>
  readerNeighborhood(
    payload.value?.nodes || [],
    payload.value?.relations || [],
    focus.value,
    nodeLimit.value,
  ),
)
const evidence = computed(() => payload.value?.relations.find((r) => r.id === selected.value))
const names = computed(() => new Map(payload.value?.nodes.map((n) => [n.id, n.name]) || []))
const rows = computed(() =>
  (payload.value?.relations || []).slice((listPage.value - 1) * 6, listPage.value * 6),
)
const safeSource = computed(() =>
  /^https?:\/\//i.test(payload.value?.source_url || '') ? payload.value?.source_url : '',
)
function expand(id: string) {
  focus.value = id
  nodeLimit.value = 15
  if (!evidence.value || ![evidence.value.source, evidence.value.target].includes(id))
    selected.value =
      payload.value?.relations.find((r) => r.source === id || r.target === id)?.id || ''
}
function chooseRelation(id: string) {
  selected.value = id
  const relation = evidence.value
  if (relation) expand(relation.source)
}
async function load(more = false) {
  const id = ++request
  loading.value = true
  error.value = ''
  if (!more) {
    payload.value = null
    focus.value = ''
    selected.value = ''
    listPage.value = 1
  }
  try {
    const params = new URLSearchParams({
      offset: String(more ? payload.value?.relations.length || 0 : 0),
      limit: '60',
    })
    if (props.versionId) params.set('version_id', props.versionId)
    const page = await api.get<ReaderPayload>(
      `/api/knowledge/regulations/${props.documentId}/relations?${params}`,
    )
    if (id !== request) return
    payload.value = mergeReaderPage(payload.value, page)
    if (!focus.value) focus.value = readerRoot(payload.value.nodes, payload.value.relations)
    if (!selected.value) selected.value = payload.value.relations[0]?.id || ''
  } catch (e) {
    if (id === request) error.value = (e as Error).message
  } finally {
    if (id === request) loading.value = false
  }
}
function draw() {
  if (!host.value || !host.value.clientWidth) return
  if (!chart) {
    chart = echarts.init(host.value)
    chart.on('click', (event: any) => {
      if (event.dataType === 'node') expand(event.data.id)
      if (event.dataType === 'edge') selected.value = event.data.evidenceIds[0]
    })
  }
  const positions = readerPositions(view.value.nodes, focus.value)
  const data = view.value.nodes.map((n) => {
    const position = positions.get(n.id)!
    return {
      id: n.id,
      name: n.name,
      x: position.x,
      y: position.y,
      symbolSize: position.center ? 28 : 16,
      itemStyle: { color: readerTypes[n.type]?.color || '#718096' },
      label: {
        show: true,
        formatter: readerLabel(n.name),
        fontSize: 12,
        lineHeight: 17,
        position: position.center ? 'bottom' : position.x < -40 ? 'left' : 'right',
        distance: 9,
        color: '#34495d',
      },
    }
  })
  chart.setOption(
    {
      animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
      tooltip: {
        renderMode: 'richText',
        formatter: (p: any) => (p.dataType === 'node' ? p.name : p.data.fullLabel),
      },
      series: [
        {
          type: 'graph',
          layout: 'none',
          roam: true,
          draggable: true,
          nodeScaleRatio: 0,
          left: '22%',
          right: '22%',
          top: 48,
          bottom: 55,
          data,
          links: view.value.lines.map((line, index) => ({
            source: line.source,
            target: line.target,
            evidenceIds: line.ids,
            fullLabel: `${line.label} · ${line.ids.length} 条依据`,
            label: {
              show: true,
              formatter: line.label,
              fontSize: 11,
              color: '#50657b',
              backgroundColor: '#ffffffdd',
              padding: 3,
            },
            lineStyle: {
              color: '#b5c7d8',
              width: 1.2,
              curveness: index % 2 ? 0.06 : -0.06,
            },
          })),
          edgeSymbol: ['none', 'arrow'],
          edgeSymbolSize: [0, 6],
          emphasis: { focus: 'adjacency' },
        },
      ],
    },
    true,
  )
  chart.resize()
}
watch(view, async () => {
  await nextTick()
  draw()
})
watch(
  () => [props.documentId, props.versionId],
  () => {
    void load()
  },
)
onMounted(() => {
  observer = new ResizeObserver(() => {
    if (chart) chart.resize()
    else draw()
  })
  if (host.value) observer.observe(host.value)
  void load()
})
onBeforeUnmount(() => {
  ++request
  observer?.disconnect()
  chart?.dispose()
})
</script>

<template>
  <section class="relations-reader" v-loading="loading" aria-label="法规关系解读">
    <div class="relation-summary" aria-live="polite">
      <span
        >全部关系 <strong>{{ payload?.total || 0 }}</strong></span
      >
      <span>已加载 {{ payload?.relations.length || 0 }} 条</span>
      <span>当前图中 {{ view.edges.length }} 条 · {{ view.lines.length }} 组连线</span>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-button v-if="error" @click="load(!!payload)">重新加载</el-button>
    <div class="relation-controls" v-if="payload?.relations.length">
      <el-select
        :model-value="focus"
        filterable
        aria-label="选择关联主体"
        placeholder="选择关联主体"
        @update:model-value="expand"
      >
        <el-option
          v-for="n in payload.nodes"
          :key="n.id"
          :value="n.id"
          :label="`${readerTypes[n.type]?.label || n.type} · ${n.name}`"
        />
      </el-select>
      <el-button
        v-if="view.edges.length < view.incidentCount && nodeLimit < 35"
        @click="nodeLimit += 10"
        >展开更多关联</el-button
      >
      <el-button text @click="expand(readerRoot(payload.nodes, payload.relations))"
        >重置视图</el-button
      >
    </div>
    <div
      v-show="payload?.relations.length"
      class="relation-graph"
      ref="host"
      role="img"
      aria-label="法规局部语义关系图，点击节点展开，点击连线查看依据；也可使用下方关系目录"
    />
    <el-empty
      v-if="!loading && !error && !payload?.total"
      description="该版本暂无可展示的关系，可切换条款阅读查看原文"
    />
    <template v-if="payload?.relations.length">
      <p class="graph-help">点击节点展开直接关联，点击连线查看依据。滚轮缩放，拖动调整位置。</p>
      <div class="relation-legend">
        <span v-for="(type, key) in readerTypes" :key="key"
          ><i :style="{ background: type.color }" />{{ type.label }}</span
        >
      </div>
      <div class="relation-detail-grid">
        <section class="relation-directory" aria-label="关系目录">
          <h3>本法规关系目录</h3>
          <button
            v-for="r in rows"
            :key="r.id"
            class="relation-row"
            :class="{ active: selected === r.id }"
            @click="chooseRelation(r.id)"
          >
            <span>{{ names.get(r.source) }}</span
            ><em>{{ r.label }} →</em><span>{{ names.get(r.target) }}</span>
            <small>{{ r.review_status === 'approved' ? '已复核' : '待核对' }}</small>
          </button>
          <el-pagination
            v-if="payload.relations.length > 6"
            v-model:current-page="listPage"
            :page-size="6"
            :total="payload.relations.length"
            layout="prev, pager, next"
            size="small"
          />
        </section>
        <aside class="relation-evidence" aria-label="关系条款依据" aria-live="polite">
          <template v-if="evidence">
            <div class="evidence-heading">
              <h3>条款依据</h3>
              <el-tag
                :type="evidence.review_status === 'approved' ? 'success' : 'warning'"
                size="small"
                >{{ evidence.review_status === 'approved' ? '已复核' : '待核对' }}</el-tag
              >
            </div>
            <p>
              {{ names.get(evidence.source) }}
              <strong>{{ evidence.label }}</strong>
              {{ names.get(evidence.target) }}
            </p>
            <div class="basis-location">
              归档版本 {{ payload.version_no }} · 条款
              {{ evidence.clause_number || '编号未记录' }}
            </div>
            <blockquote dir="auto">{{ evidence.quote }}</blockquote>
            <el-button type="primary" plain @click="emit('basis', evidence.clause_id)"
              >阅读条款与中文对照</el-button
            >
            <a v-if="safeSource" :href="safeSource" target="_blank" rel="noopener noreferrer"
              >官方原文 ↗</a
            >
          </template>
        </aside>
      </div>
      <el-button
        v-if="payload.relations.length < payload.total"
        :loading="loading"
        class="load-more"
        @click="load(true)"
        >加载更多关系（{{ payload.relations.length }}/{{ payload.total }}）</el-button
      >
    </template>
  </section>
</template>

<style scoped>
.relation-summary,
.relation-controls,
.relation-legend {
  display: flex;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
}
.relation-summary {
  color: #718096;
  font-size: 13px;
  margin: 22px 0 18px;
}
.relation-summary strong {
  color: #263c4b;
  font-size: 18px;
  margin-left: 6px;
}
.relation-controls .el-select {
  width: 340px;
  max-width: 100%;
}
.relation-graph {
  width: 100%;
  height: 460px;
  margin-top: 16px;
  border: 1px solid #edf1f5;
  border-radius: 12px;
  background: #fbfcfe;
}
.graph-help {
  color: #718096;
  font-size: 12px;
  margin: 12px 0;
}
.relation-legend {
  gap: 14px;
  font-size: 12px;
  color: #61758a;
  margin-bottom: 24px;
}
.relation-legend i {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 5px;
}
.relation-detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 22px;
}
h3 {
  font-size: 15px;
  margin: 0 0 14px;
  color: #263c4b;
}
.relation-row {
  display: flex;
  align-items: baseline;
  gap: 7px;
  flex-wrap: wrap;
  width: 100%;
  text-align: left;
  border: 1px solid #e7edf4;
  border-radius: 9px;
  background: white;
  padding: 12px;
  margin: 8px 0;
  font: inherit;
  font-size: 13px;
  line-height: 1.65;
  color: #3c5065;
  cursor: pointer;
  overflow-wrap: anywhere;
}
.relation-row.active {
  border-color: #94b7e1;
  background: #f4f8ff;
}
.relation-row em {
  font-style: normal;
  color: #2565a6;
}
.relation-row small {
  color: #7d8999;
  white-space: nowrap;
}
.relation-evidence {
  background: #f7f9fc;
  border-radius: 12px;
  padding: 20px;
  min-width: 0;
  font-size: 14px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.evidence-heading {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}
.basis-location {
  color: #718096;
  font-size: 12px;
}
blockquote {
  margin: 14px 0 20px;
  padding: 0 0 0 14px;
  border-left: 2px solid #c7d8eb;
  white-space: pre-wrap;
  max-height: 260px;
  overflow: auto;
}
.relation-evidence a {
  display: inline-block;
  margin: 12px;
  color: #2565a6;
  font-size: 13px;
}
.load-more {
  display: block;
  margin: 24px auto 0;
}
@media (max-width: 1000px) {
  .relation-detail-grid {
    grid-template-columns: 1fr;
  }
  .relation-graph {
    height: 420px;
  }
}
@media (prefers-reduced-motion: reduce) {
  * {
    transition: none !important;
  }
}
</style>
