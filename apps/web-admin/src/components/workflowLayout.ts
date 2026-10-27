export interface NodePosition {
  x: number
  y: number
}

export interface LayoutNode {
  node_id: string
  position_x: number
  position_y: number
}

export const NODE_WIDTH = 260
export const NODE_HEIGHT = 210
export const NODE_GAP = 96
export const LAYOUT_LIMIT = 10000

export function boundedPosition(x: number, y: number): NodePosition {
  return {
    x: Math.round(Math.max(24, Math.min(LAYOUT_LIMIT, x))),
    y: Math.round(Math.max(24, Math.min(LAYOUT_LIMIT, y))),
  }
}

// Retain the configured left-to-right order, but give each column enough room
// for real card heights. This changes presentation only, never the workflow DSL.
export function spacedLayout(nodes: LayoutNode[], heights: Record<string, number>) {
  const columns = [...new Set(nodes.map((node) => node.position_x))].sort((a, b) => a - b)
  const groups = columns.map((x) =>
    nodes.filter((node) => node.position_x === x).sort((a, b) => a.position_y - b.position_y),
  )
  const heightOf = (node: LayoutNode) => heights[node.node_id] || NODE_HEIGHT
  const columnHeights = groups.map(
    (group) => group.reduce((sum, node) => sum + heightOf(node), 0) + (group.length - 1) * NODE_GAP,
  )
  const contentHeight = Math.max(520, ...columnHeights)
  const positions: Record<string, NodePosition> = {}
  groups.forEach((group, column) => {
    let y = 60 + (contentHeight - columnHeights[column]) / 2
    group.forEach((node) => {
      positions[node.node_id] = { x: 60 + column * (NODE_WIDTH + 116), y: Math.round(y) }
      y += heightOf(node) + NODE_GAP
    })
  })
  return positions
}

export function dragPosition(
  origin: NodePosition,
  deltaX: number,
  deltaY: number,
  scale: number,
): NodePosition {
  return boundedPosition(origin.x + deltaX / scale, origin.y + deltaY / scale)
}

export function readPositions(
  raw: string | null,
  nodes: LayoutNode[],
): Record<string, NodePosition> {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw)
    if (parsed?.version !== 1 || !parsed.positions || typeof parsed.positions !== 'object')
      return {}
    return Object.fromEntries(
      nodes.flatMap((node) => {
        const value = parsed.positions[node.node_id]
        return typeof value?.x === 'number' &&
          Number.isFinite(value.x) &&
          typeof value?.y === 'number' &&
          Number.isFinite(value.y)
          ? [[node.node_id, boundedPosition(value.x, value.y)]]
          : []
      }),
    )
  } catch {
    return {}
  }
}
