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
