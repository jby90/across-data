/** Display projection only. Never changes graph records, evidence or review decisions. */
export interface DisplayNode {
  id: string
  node_type: string
  name_zh: string
  name_orig?: string | null
  document_id?: string | null
}

export interface DisplayEdge {
  id: string
  source_node_id: string
  target_node_id: string
  relation_type: string
  evidence_clause_id?: string
  document_title?: string | null
  usable_as_legal_basis: boolean
}

export interface LawGroup<E extends DisplayEdge = DisplayEdge> {
  id: string
  name: string
  instrumentIds: string[]
  edges: E[]
}

export interface MergedDisplayEdge {
  source: string
  target: string
  relation: string
  count: number
  approved: boolean
  evidenceIds: string[]
}

export const UNASSIGNED_GROUP = '__unassigned__'

/** Use explicit instrument endpoints or unambiguous clause provenance, never shared hub reachability. */
export function groupByLaw<N extends DisplayNode, E extends DisplayEdge>(nodes: N[], edges: E[]): LawGroup<E>[] {
  const groups = new Map<string, LawGroup<E>>()
  const instruments = new Map<string, string>()
  const titles = new Map<string, Set<string>>()
  for (const n of nodes) {
    if (n.node_type !== 'INSTRUMENT') continue
    const id = n.document_id || n.id
    if (!groups.has(id)) groups.set(id, { id, name: n.name_zh, instrumentIds: [], edges: [] })
    groups.get(id)!.instrumentIds.push(n.id)
    instruments.set(n.id, id)
    for (const name of [n.name_zh, n.name_orig]) {
      if (!name) continue
      if (!titles.has(name)) titles.set(name, new Set())
      titles.get(name)!.add(id)
    }
  }
  const directGroups = (e: E) => new Set(
    [instruments.get(e.source_node_id), instruments.get(e.target_node_id)].filter((id): id is string => !!id),
  )
  const clauseGroups = new Map<string, Set<string>>()
  for (const e of edges) {
    if (!e.evidence_clause_id) continue
    if (!clauseGroups.has(e.evidence_clause_id)) clauseGroups.set(e.evidence_clause_id, new Set())
    for (const id of directGroups(e)) clauseGroups.get(e.evidence_clause_id)!.add(id)
  }
  const seen = new Set<string>()
  for (const e of edges) {
    if (seen.has(e.id)) continue
    seen.add(e.id)
    let ids = directGroups(e)
    if (!ids.size) {
      const fromClause = e.evidence_clause_id ? clauseGroups.get(e.evidence_clause_id) : undefined
      const fromTitle = e.document_title ? titles.get(e.document_title) : undefined
      if (fromClause?.size === 1) ids = fromClause
      else if (fromTitle?.size === 1) ids = fromTitle
    }
    if (!ids.size) {
      if (!groups.has(UNASSIGNED_GROUP)) groups.set(UNASSIGNED_GROUP, {
        id: UNASSIGNED_GROUP, name: '其他关联（法规归属待定位）', instrumentIds: [], edges: [],
      })
      ids = new Set([UNASSIGNED_GROUP])
    }
    for (const id of ids) groups.get(id)!.edges.push(e)
  }
  return [...groups.values()].sort((a, b) =>
    Number(a.id === UNASSIGNED_GROUP) - Number(b.id === UNASSIGNED_GROUP)
    || a.name.localeCompare(b.name, 'zh-CN', { numeric: true }) || a.id.localeCompare(b.id),
  )
}

export function mergeDisplayEdges<N extends DisplayNode, E extends DisplayEdge>(nodes: N[], edges: E[]): MergedDisplayEdge[] {
  const merged = new Map<string, MergedDisplayEdge>()
  const names = new Map(nodes.map((n) => [n.id, n.name_zh]))
  const seen = new Set<string>()
  for (const e of edges) {
    if (seen.has(e.id)) continue
    seen.add(e.id)
    const key = JSON.stringify([e.source_node_id, e.target_node_id, e.relation_type])
    const existing = merged.get(key)
    if (existing) {
      existing.count++
      existing.approved &&= e.usable_as_legal_basis
      existing.evidenceIds.push(e.id)
    } else merged.set(key, {
      source: e.source_node_id, target: e.target_node_id, relation: e.relation_type,
      count: 1, approved: e.usable_as_legal_basis, evidenceIds: [e.id],
    })
  }
  // Stable name/type ordering, independent of degree and regenerated edge IDs.
  const label = (e: MergedDisplayEdge) => JSON.stringify([
    e.relation, names.get(e.source) || e.source, names.get(e.target) || e.target, e.source, e.target,
  ])
  return [...merged.values()].sort((a, b) => label(a).localeCompare(label(b), 'zh-CN', { numeric: true }))
}

/** Pagination, rather than truncation: every group remains reachable. */
export function relationPage<T>(relations: T[], page: number, size: number) {
  const pageSize = Number.isFinite(size) ? Math.max(1, Math.floor(size)) : 36
  const pages = Math.max(1, Math.ceil(relations.length / pageSize))
  const current = Math.min(pages, Math.max(1, Math.floor(page) || 1))
  return { page: current, pages, edges: relations.slice((current - 1) * pageSize, current * pageSize) }
}

/** Fixed, equally spaced law clusters for the overview. No synthetic nodes or edges.
 * Shared entities are drawn once; their placement is not a new legal relationship.
 */
export function layoutLawOverview<N extends DisplayNode, E extends DisplayEdge>(
  nodes: N[], groups: LawGroup<E>[], edges: MergedDisplayEdge[],
) {
  const byId = new Map(nodes.map(n => [n.id, n]))
  const degree = new Map<string, number>()
  for (const e of edges) {
    degree.set(e.source, (degree.get(e.source) || 0) + 1)
    degree.set(e.target, (degree.get(e.target) || 0) + 1)
  }
  const owners = new Map<string, string>()
  const ordered = [...groups].sort((a, b) => a.id.localeCompare(b.id))
  for (const g of ordered) for (const id of g.instrumentIds) owners.set(id, g.id)
  for (const g of ordered) for (const e of g.edges) {
    for (const id of [e.source_node_id, e.target_node_id]) {
      const n = byId.get(id)
      if (n && !['COUNTRY', 'REGULATOR'].includes(n.node_type) && !owners.has(id)) owners.set(id, g.id)
    }
  }
  const visible = nodes.filter(n => degree.has(n.id) || (n.node_type === 'INSTRUMENT' && owners.has(n.id)))
  const columns = Math.max(1, Math.ceil(Math.sqrt(ordered.length)))
  const positions = new Map<string, { x: number; y: number }>()
  const membersByGroup = new Map<string, N[]>()
  for (const n of visible) {
    const owner = owners.get(n.id)
    if (!owner) continue
    if (!membersByGroup.has(owner)) membersByGroup.set(owner, [])
    membersByGroup.get(owner)!.push(n)
  }
  ordered.forEach((g, index) => {
    const cx = (index % columns) * 210
    const cy = Math.floor(index / columns) * 180 + 170
    const members = (membersByGroup.get(g.id) || []).sort((a, b) => a.id.localeCompare(b.id))
    const instruments = members.filter(n => n.node_type === 'INSTRUMENT')
    const leaves = members.filter(n => n.node_type !== 'INSTRUMENT')
    instruments.forEach((n, i) => {
      const radius = instruments.length > 1 ? 18 : 0
      const angle = i * 2 * Math.PI / instruments.length
      positions.set(n.id, { x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius })
    })
    leaves.forEach((n, i) => {
      const angle = i * Math.PI * (3 - Math.sqrt(5))
      const radius = 22 + 48 * Math.sqrt((i + 1) / leaves.length)
      positions.set(n.id, { x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius })
    })
  })
  const hubs = visible.filter(n => !positions.has(n.id)).sort((a, b) => a.id.localeCompare(b.id))
  hubs.forEach((n, i) => positions.set(n.id, { x: (i % columns) * 210, y: -Math.floor(i / columns) * 85 }))
  const groupIndex = new Map(ordered.map((g, index) => [g.id, index + 1]))
  return visible.map(n => ({
    overviewLabel: n.node_type === 'INSTRUMENT' ? `#${String(groupIndex.get(owners.get(n.id)!) || '').padStart(2, '0')}` : undefined,
    id: n.id, name: n.name_zh, type: n.node_type, degree: degree.get(n.id) || 0,
    ...positions.get(n.id)!,
  }))
}
