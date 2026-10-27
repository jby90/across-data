/** Shared canvas presentation only. No evidence or review record is modified. */
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

export function graphSeries(
  nodes: FgNode[], edges: FgEdge[], categories: FgCategory[],
  options: { overview?: boolean; selectedId?: string; reducedMotion?: boolean; viewportWidth?: number } = {},
) {
  const overview = !!options.overview
  const labelBudget = Math.max(2, Math.min(12, Math.floor((options.viewportWidth || 960) / 140)))
  const labelled = new Set(nodes
    .filter(n => n.type === 'INSTRUMENT' || (!overview && n.degree >= 10))
    .sort((a, b) => b.degree - a.degree || a.id.localeCompare(b.id))
    .slice(0, labelBudget).map(n => n.id))
  return {
    type: 'graph' as const,
    layout: 'force' as const,
    // Larger graphs start farther out, rather than clipping the overview at its edges.
    zoom: Math.max(0.12, Math.min(0.8, 8 / Math.sqrt(Math.max(1, nodes.length)))),
    scaleLimit: { min: 0.08, max: 3 },
    roam: true,
    draggable: true,
    // Keep node sizes stable when zooming; labels are screen-sized as well.
    nodeScaleRatio: 0,
    categories: categories.map(c => ({ name: c.name, itemStyle: { color: c.color } })),
    data: nodes.map(n => {
      const selected = n.id === options.selectedId
      return {
        id: n.id, name: n.name,
        category: categories.findIndex(c => c.type === n.type),
        symbolSize: selected ? 32 : Math.min(30, 9 + Math.sqrt(Math.max(0, n.degree)) * 3),
        itemStyle: selected ? { borderColor: '#0f172a', borderWidth: 2 } : undefined,
        label: {
          show: selected || n.type === 'COUNTRY' || labelled.has(n.id),
        },
      }
    }),
    links: edges.map(e => ({
      source: e.source, target: e.target, name: e.label, value: 1,
      // Small graphs must not automatically turn into rotated walls of edge text.
      label: { show: false },
      emphasis: { label: { show: false } },
      lineStyle: {
        color: e.approved ? '#16a34a' : '#94a3b8',
        curveness: 0.08, opacity: overview ? 0.45 : 0.7,
      },
    })),
    force: {
      repulsion: 150, edgeLength: [45, 115], gravity: 0.15,
      layoutAnimation: !options.reducedMotion,
    },
    edgeSymbol: ['none', 'arrow'],
    edgeSymbolSize: 4,
    edgeLabel: { show: false, fontSize: 11 },
    labelLayout: { hideOverlap: true },
    // Preserve normal label visibility on adjacent nodes; tooltip reveals the hovered name.
    emphasis: { focus: 'adjacency' as const, label: { fontSize: 12 } },
    lineStyle: { width: overview ? 0.8 : 1 },
    label: {
      position: 'right' as const, fontSize: 11, color: '#334155',
      overflow: 'truncate' as const, width: 160,
    },
  }
}
