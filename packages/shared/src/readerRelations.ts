/** A bounded one-hop reader projection. Never fabricates edges or changes review state. */
export interface ReaderNode {
  id: string
  name: string
  original_name: string | null
  type: string
}
export interface ReaderRelation {
  id: string
  source: string
  target: string
  relation_type: string
  label: string
  clause_id: string
  clause_number: string | null
  quote: string
  review_status: string
}
export interface ReaderPayload {
  document_id: string
  version_id: string | null
  version_no: number | null
  source_url: string
  total: number
  offset: number
  limit: number
  nodes: ReaderNode[]
  relations: ReaderRelation[]
}
export const readerTypes: Record<string, { label: string; color: string }> = {
  INSTRUMENT: { label: '法规文件', color: '#7655c9' },
  OBLIGATION: { label: '合规义务', color: '#b96821' },
  DATA_CATEGORY: { label: '数据类型', color: '#267aa2' },
  PENALTY: { label: '责任与罚则', color: '#bd5475' },
  REGULATOR: { label: '监管机构', color: '#338578' },
  COUNTRY: { label: '国家与地区', color: '#4e70ac' },
}
export function mergeReaderPage(old: ReaderPayload | null, page: ReaderPayload): ReaderPayload {
  if (!old || old.document_id !== page.document_id || old.version_id !== page.version_id)
    return page
  return {
    ...page,
    nodes: [...new Map([...old.nodes, ...page.nodes].map((n) => [n.id, n])).values()],
    relations: [...new Map([...old.relations, ...page.relations].map((r) => [r.id, r])).values()],
  }
}
export function readerRoot(nodes: ReaderNode[], edges: ReaderRelation[]) {
  const degree = new Map<string, number>()
  for (const e of edges)
    for (const id of new Set([e.source, e.target])) degree.set(id, (degree.get(id) || 0) + 1)
  return (
    [...nodes].sort(
      (a, b) =>
        Number(b.type === 'INSTRUMENT') - Number(a.type === 'INSTRUMENT') ||
        (degree.get(b.id) || 0) - (degree.get(a.id) || 0) ||
        a.id.localeCompare(b.id),
    )[0]?.id || ''
  )
}
export function readerNeighborhood(
  nodes: ReaderNode[],
  relations: ReaderRelation[],
  focus: string,
  maxNodes = 15,
) {
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const selected = new Set<string>(byId.has(focus) ? [focus] : [])
  const incident = relations.filter(
    (e) => (e.source === focus || e.target === focus) && byId.has(e.source) && byId.has(e.target),
  )
  // Round-robin relation types avoids hiding a minority relationship behind a large fan-out.
  const groups = new Map<string, ReaderRelation[]>()
  for (const edge of incident)
    groups.set(edge.relation_type, [...(groups.get(edge.relation_type) || []), edge])
  const queues = [...groups.values()].map((items) => [...items])
  while (queues.some((q) => q.length) && selected.size < Math.max(2, maxNodes)) {
    for (const queue of queues) {
      const edge = queue.shift()
      if (!edge) continue
      const other = edge.source === focus ? edge.target : edge.source
      if (selected.size < Math.max(2, maxNodes)) selected.add(other)
    }
  }
  const edges = incident.filter((e) => selected.has(e.source) && selected.has(e.target))
  const lines = new Map<string, { source: string; target: string; label: string; ids: string[] }>()
  for (const e of edges) {
    const key = JSON.stringify([e.source, e.target, e.relation_type])
    const line = lines.get(key)
    if (line) line.ids.push(e.id)
    else
      lines.set(key, {
        source: e.source,
        target: e.target,
        label: e.label,
        ids: [e.id],
      })
  }
  return {
    nodes: nodes.filter((n) => selected.has(n.id)),
    edges,
    lines: [...lines.values()],
    incidentCount: incident.length,
  }
}
export function readerLabel(text: string, width = 10, lines = 2) {
  const chars = Array.from(text)
  const visible = chars.slice(0, width * lines)
  return (
    Array.from({ length: Math.ceil(visible.length / width) }, (_, i) =>
      visible.slice(i * width, (i + 1) * width).join(''),
    ).join('\n') + (chars.length > visible.length ? '…' : '')
  )
}

/** Non-degenerate coordinates also for one/two neighbors (avoid cosine epsilon extents). */
export function readerPositions(nodes: ReaderNode[], focus: string) {
  const neighbors = nodes.filter((n) => n.id !== focus)
  return new Map(
    nodes.map((n) => {
      const index = neighbors.findIndex((other) => other.id === n.id)
      const angle =
        (index / Math.max(1, neighbors.length)) * Math.PI * 2 -
        (neighbors.length <= 2 ? Math.PI / 4 : Math.PI / 2)
      return [
        n.id,
        {
          x: index < 0 ? 0 : Math.round(Math.cos(angle) * 240),
          y: index < 0 ? 0 : Math.round(Math.sin(angle) * 200),
          center: index < 0,
        },
      ]
    }),
  )
}
