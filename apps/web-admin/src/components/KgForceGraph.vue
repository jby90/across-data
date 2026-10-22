<script setup lang="ts">
import * as echarts from 'echarts'
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

export interface FgNode {
  id: string
  name: string
  type: string
  degree: number
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
  selectedId?: string
  height?: string
}>()
const emit = defineEmits<{ (e: 'node-click', id: string): void }>()

const host = ref<HTMLDivElement | null>(null)
const canvas = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let observer: ResizeObserver | null = null

function buildOption(): echarts.EChartsOption {
  const showEdgeLabel = props.edges.length <= 40
  const data = props.nodes.map((n) => {
    const selected = n.id === props.selectedId
    return {
      id: n.id,
      name: n.name,
      category: props.categories.findIndex((c) => c.type === n.type),
      symbolSize: selected ? 34 : Math.min(40, 12 + Math.sqrt(n.degree) * 4),
      itemStyle: selected ? { borderColor: '#0f172a', borderWidth: 3 } : undefined,
      label: { show: n.type === 'COUNTRY' || n.degree >= 8 || selected },
    }
  })
  const links = props.edges.map((e) => ({
    source: e.source,
    target: e.target,
    value: e.label as unknown as number,
    label: { show: showEdgeLabel, formatter: e.label, fontSize: 10 },
    lineStyle: { color: e.approved ? '#16a34a' : '#94a3b8', curveness: 0.12 },
  }))
  return {
    tooltip: {
      formatter: (p: any) => {
        if (p.dataType === 'edge') return `${p.data.value}`
        const cat = props.categories[p.data.category]
        return `${cat?.name ?? ''}<br/>${p.data.name}`
      },
    },
    legend: [
      {
        data: props.categories.map((c) => c.name),
        bottom: 0,
        itemWidth: 10,
        itemHeight: 10,
        textStyle: { color: '#475569' },
      },
    ],
    series: [
      {
        type: 'graph',
        layout: 'force',
        roam: true,
        draggable: true,
        categories: props.categories.map((c) => ({ name: c.name, itemStyle: { color: c.color } })),
        data,
        links,
        force: { repulsion: 420, edgeLength: [70, 170], gravity: 0.06 },
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: 6,
        emphasis: { focus: 'adjacency', label: { show: true } },
        lineStyle: { width: 1.2 },
        label: { position: 'right', fontSize: 11, color: '#334155', overflow: 'truncate', width: 140 },
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
  }
  chart.setOption(buildOption(), true)
  chart.resize()
}

watch(() => [props.nodes, props.edges, props.selectedId], render)

onMounted(() => {
  if (typeof ResizeObserver !== 'undefined' && host.value) {
    observer = new ResizeObserver(() => render())
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
  <div ref="host" class="kg-force" :style="{ height: height || '520px' }" data-testid="kg-force-graph">
    <div ref="canvas" class="canvas"></div>
    <div v-if="!nodes.length" class="empty">暂无图谱数据，请先点击「重新抽取本国图谱」</div>
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
</style>
