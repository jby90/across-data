<script setup lang="ts">
import * as echarts from 'echarts'
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

import { graphSeries, type FgNode, type FgEdge, type FgCategory } from '@cyshuhe/shared/src/graphPresentation'
export type { FgNode, FgEdge, FgCategory } from '@cyshuhe/shared/src/graphPresentation'

const props = defineProps<{
  nodes: FgNode[]
  edges: FgEdge[]
  categories: FgCategory[]
  visibleTypes?: string[]
  selectedId?: string
  height?: string
  overview?: boolean
}>()
const emit = defineEmits<{
  (e: 'node-click', id: string): void
  (e: 'toggle-type', nodeType: string): void
}>()

const host = ref<HTMLDivElement | null>(null)
const canvas = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let observer: ResizeObserver | null = null

function buildOption(): echarts.EChartsOption {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  return {
    animation: !reducedMotion,
    animationDuration: 240,
    animationDurationUpdate: 180,
    tooltip: {
      renderMode: 'richText',
      textStyle: { fontSize: 12 },
      confine: true,
      formatter: (p: any) => {
        if (p.dataType === 'edge') return `${p.data.name}`
        const cat = props.categories[p.data.category]
        return `${cat?.name ?? ''}\n${p.data.name}`
      },
    },
    series: [graphSeries(props.nodes, props.edges, props.categories, {
      overview: props.overview, selectedId: props.selectedId, reducedMotion, viewportWidth: canvas.value?.clientWidth,
    })],
  }
}

async function render() {
  await nextTick()
  const el = canvas.value
  if (!el || !el.clientWidth || !el.clientHeight) return
  if (!chart || chart.getDom() !== el) {
    chart?.dispose()
    chart = echarts.init(el)
    chart.on('click', (p: any) => {
      if (p.dataType === 'node') emit('node-click', String(p.data.id))
    })
    chart.on('legendselectchanged', (p: any) => {
      const cat = props.categories.find((c) => c.name === p?.name)
      if (cat) emit('toggle-type', cat.type)
    })
  }
  chart.setOption(buildOption(), true)
  chart.resize()
}

watch(
  () => [props.nodes, props.edges, props.selectedId, props.visibleTypes, props.overview],
  () => render(),
  { deep: true },
)

onMounted(() => {
  if (typeof ResizeObserver !== 'undefined' && host.value) {
    observer = new ResizeObserver(() => {
      const el = canvas.value
      // Hidden tabs have a zero-sized canvas; ECharts must not resize until visible.
      if (!el || !el.clientWidth || !el.clientHeight) return
      if (chart) chart.resize()
      else render()
    })
    observer.observe(host.value)
  }
  render()
})
onUnmounted(() => {
  observer?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div
    ref="host"
    class="kg-force"
    :style="{ height: height || '500px' }"
    data-testid="user-kg-force-graph"
  >
    <div
      ref="canvas"
      class="canvas"
      role="img"
      aria-label="法规知识关系图，证据明细可在列表中查看"
    ></div>
    <button class="reset-view" type="button" @click="chart?.dispatchAction({ type: 'restore' })">
      重置视图
    </button>
    <div v-if="!nodes.length" class="empty">当前筛选条件下暂无关联节点</div>
  </div>
</template>

<style scoped>
.kg-force {
  position: relative;
  width: 100%;
  min-height: 280px;
}
.canvas {
  position: absolute;
  inset: 0;
}
.empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #64748b;
  font-size: 13px;
}
.reset-view {
  position: absolute;
  bottom: 16px;
  left: 16px;
  z-index: 1;
  background: #ffffffeb;
  border: 1px solid #dce4ed;
  border-radius: 7px;
  padding: 7px 12px;
  font-size: 12px;
  color: #64798a;
}
.reset-view:hover {
  color: #315fe8;
  border-color: #b8c9f1;
}
</style>
