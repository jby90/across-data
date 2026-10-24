<script setup lang="ts">
import * as echarts from 'echarts'
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

export interface FgNode {
  id: string
  name: string
  type: string
  degree: number
  overviewLabel?: string
  x?: number
  y?: number
}
export interface FgEdge {
  source: string
  target: string
  label: string
  approved: boolean
}
export interface FgCategory {
  type: string
  name: string
  color: string
}

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
  const showEdgeLabel = !props.overview && props.edges.length <= 20
  const data = props.nodes.map((n) => {
    const selected = n.id === props.selectedId
    return {
      id: n.id,
      name: n.name,
      category: props.categories.findIndex((c) => c.type === n.type),
      x: n.x,
      y: n.y,
      symbolSize: selected ? (props.overview ? 18 : 34) : props.overview
        ? (n.type === 'INSTRUMENT' ? 14 : ['COUNTRY', 'REGULATOR'].includes(n.type) ? 10 : 4)
        : Math.min(40, 12 + Math.sqrt(n.degree) * 4),
      itemStyle: selected ? { borderColor: '#0f172a', borderWidth: 3 } : undefined,
      label: {
        show: props.overview ? ['COUNTRY', 'INSTRUMENT'].includes(n.type) || selected : n.type === 'COUNTRY' || n.degree >= 10 || selected,
        ...(props.overview && n.overviewLabel ? { formatter: n.overviewLabel, position: 'bottom' as const, width: 42 } : {}),
      },
    }
  })
  const links = props.edges.map((e) => ({
    source: e.source,
    target: e.target,
    value: e.label as unknown as number,
    label: { show: showEdgeLabel, formatter: e.label, fontSize: 12 },
    lineStyle: { color: e.approved ? '#16a34a' : '#94a3b8', curveness: props.overview ? 0.04 : 0.12, opacity: props.overview ? 0.25 : 1 },
  }))
  return {
    animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    animationDuration: 240,
    animationDurationUpdate: 180,
    tooltip: {
      renderMode: 'richText',
      formatter: (p: any) => {
        if (p.dataType === 'edge') return `${p.data.value}`
        const cat = props.categories[p.data.category]
        return `${cat?.name ?? ''}\n${p.data.name}`
      },
    },
    series: [
      {
        type: 'graph',
        layout: props.overview ? 'none' : 'force',
        zoom: props.overview ? 0.9 : 0.6,
        scaleLimit: { min: 0.2, max: 3 },
        roam: true,
        draggable: !props.overview,
        categories: props.categories.map((c) => ({
          name: c.name,
          itemStyle: { color: c.color },
        })),
        data,
        links,
        force: { repulsion: 150, edgeLength: [45, 115], gravity: 0.15 },
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: props.overview ? 3 : 6,
        labelLayout: { hideOverlap: !!props.overview },
        emphasis: { focus: 'adjacency', label: { show: true } },
        lineStyle: { width: props.overview ? 0.65 : 1.2 },
        label: {
          position: 'right',
          fontSize: props.overview ? 11 : 13,
          color: '#334155',
          overflow: 'truncate',
          width: props.overview ? 110 : 180,
        },
      },
    ],
  }
}

async function render() {
  await nextTick()
  const el = canvas.value
  // 容器尺寸为 0 时 ECharts 会得到空画布，等 ResizeObserver 再次触发
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
    :style="{ height: height || '520px' }"
    data-testid="kg-force-graph"
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
    <div v-if="!nodes.length" class="empty">
      当前筛选条件下暂无关联节点，可开启更多节点类型或切换关系筛选
    </div>
  </div>
</template>

<style scoped>
.kg-force {
  position: relative;
  width: 100%;
  min-height: 300px;
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
