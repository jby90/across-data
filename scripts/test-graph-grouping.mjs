import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'

const require = createRequire(new URL('../apps/web-admin/package.json', import.meta.url))
const ts = require('typescript')
const source = await readFile(new URL('../packages/shared/src/graphGrouping.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText
const { groupByLaw, mergeDisplayEdges, relationPage, layoutLawOverview, UNASSIGNED_GROUP } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`,
)
const law = (id, name = id, doc = id) => ({ id, name_zh: name, node_type: 'INSTRUMENT', document_id: doc })
const entity = (id) => ({ id, name_zh: id, node_type: 'OBLIGATION' })
const edge = (id, source, target, extra = {}) => ({
  id, source_node_id: source, target_node_id: target, relation_type: 'REQUIRES',
  usable_as_legal_basis: false, evidence_clause_id: `clause-${id}`, ...extra,
})

test('two large laws cannot starve smaller laws; all evidence remains reachable by paging', () => {
  const nodes = Array.from({ length: 20 }, (_, i) => law(`law-${i}`))
  const edges = nodes.flatMap((n, i) => Array.from({ length: i < 2 ? 500 : 2 }, (_, j) => edge(`${i}-${j}`, n.id, `item-${i}-${j}`)))
  const before = JSON.stringify({ nodes, edges })
  const groups = groupByLaw(nodes, edges)
  assert.equal(groups.length, 20)
  assert.equal(groups.find(g => g.id === 'law-19').edges.length, 2)
  const reached = new Set()
  for (const group of groups) {
    const merged = mergeDisplayEdges(nodes, group.edges)
    for (let page = 1; page <= relationPage(merged, 1, 36).pages; page++) {
      const view = relationPage(merged, page, 36)
      assert.ok(view.edges.length <= 36)
      for (const e of view.edges) for (const id of e.evidenceIds) reached.add(id)
    }
  }
  assert.equal(reached.size, edges.length)
  assert.equal(JSON.stringify({ nodes, edges }), before)
})

test('same title across different document IDs is never conflated; versions of same document share a group', () => {
  const nodes = [law('a', 'Same title', 'doc-a'), law('a-version', 'Same title', 'doc-a'), law('b', 'Same title', 'doc-b')]
  const groups = groupByLaw(nodes, [edge('a', 'a-version', 'x'), edge('b', 'b', 'y')])
  assert.equal(groups.length, 2)
  assert.deepEqual(groups.find(g => g.id === 'doc-a').instrumentIds, ['a', 'a-version'])
  assert.equal(groups.find(g => g.id === 'doc-b').edges[0].id, 'b')
})

test('cross-law relation appears in both law views; global evidence IDs still count once', () => {
  const groups = groupByLaw([law('a'), law('b')], [edge('cross', 'a', 'b', { relation_type: 'AMENDS' })])
  assert.deepEqual(groups.map(g => g.edges.length), [1, 1])
  assert.equal(new Set(groups.flatMap(g => g.edges.map(e => e.id))).size, 1)
})

test('shared entities and hubs do not invent a relationship to another law', () => {
  const groups = groupByLaw([law('a'), law('b'), entity('shared')], [edge('a', 'a', 'shared'), edge('b', 'b', 'shared'), edge('unknown', 'shared', 'data')])
  assert.equal(groups.find(g => g.id === 'a').edges.length, 1)
  assert.equal(groups.find(g => g.id === 'b').edges.length, 1)
  assert.equal(groups.find(g => g.id === UNASSIGNED_GROUP).edges[0].id, 'unknown')
})

test('indirect relation uses unique clause provenance or exact title, ambiguous title remains explicit', () => {
  const nodes = [law('a', 'A'), law('b', 'Duplicate'), law('c', 'Duplicate')]
  const edges = [edge('anchor', 'a', 'x', { evidence_clause_id: 'clause-a' }),
    edge('indirect', 'x', 'y', { evidence_clause_id: 'clause-a' }),
    edge('title', 'z', 'y', { document_title: 'A' }),
    edge('ambiguous', 'z', 'w', { document_title: 'Duplicate' })]
  const groups = groupByLaw(nodes, edges)
  assert.deepEqual(groups.find(g => g.id === 'a').edges.map(e => e.id), ['anchor', 'indirect', 'title'])
  assert.equal(groups.find(g => g.id === UNASSIGNED_GROUP).edges[0].id, 'ambiguous')
})

test('merged line counts preserve direction, relation type and every evidence ID; mixed approval stays pending', () => {
  const edges = [edge('1', 'a', 'b', { usable_as_legal_basis: true }), edge('2', 'a', 'b'),
    edge('3', 'b', 'a'), edge('4', 'a', 'b', { relation_type: 'PROHIBITS' })]
  const merged = mergeDisplayEdges([], edges)
  assert.equal(merged.length, 3)
  assert.equal(merged.reduce((sum, e) => sum + e.count, 0), 4)
  const pair = merged.find(e => e.count === 2)
  assert.equal(pair.approved, false)
  assert.deepEqual(pair.evidenceIds, ['1', '2'])
  assert.equal(mergeDisplayEdges([], [edges[0]])[0].approved, true)
  assert.equal(mergeDisplayEdges([], [edges[0], edges[0]])[0].count, 1)
})

test('pagination clamps stale page after filters and handles empty countries', () => {
  assert.deepEqual(relationPage([], 100, 36), { page: 1, pages: 1, edges: [] })
  assert.deepEqual(relationPage([1, 2, 3], 100, 2), { page: 2, pages: 2, edges: [3] })
  assert.deepEqual(relationPage([1, 2], -5, 0), { page: 1, pages: 2, edges: [1] })
  assert.deepEqual(groupByLaw([], []), [])
})

test('display order is independent of node degree, API response order and regenerated edge IDs', () => {
  const nodes = [law('a'), entity('z'), entity('b')]
  const edges = [edge('old-z', 'a', 'z'), edge('old-b', 'a', 'b')]
  const projection = es => mergeDisplayEdges(nodes, es).map(e => [e.source, e.target, e.relation])
  assert.deepEqual(projection(edges), projection(edges.toReversed().map(e => ({ ...e, id: `new-${e.id}` }))))
})

test('both apps use the same tested projection and identical directory presentation', async () => {
  const [admin, user] = await Promise.all(['web-admin', 'web-user'].map(app =>
    readFile(new URL(`../apps/${app}/src/components/KgLawGroups.vue`, import.meta.url), 'utf8')))
  assert.equal(admin.replace(/\r\n/g, '\n'), user.replace(/\r\n/g, '\n'))
})

test('overview draws every filtered relationship, keeps shared nodes once and creates no graph data', () => {
  const nodes = [law('a'), law('b'), entity('shared'), entity('data'), { id: 'country', name_zh: 'SG', node_type: 'COUNTRY' }]
  const edges = [edge('1', 'a', 'shared'), edge('2', 'b', 'shared'), edge('3', 'shared', 'data'), edge('4', 'country', 'a')]
  const before = JSON.stringify({ nodes, edges })
  const merged = mergeDisplayEdges(nodes, edges)
  const overview = layoutLawOverview(nodes, groupByLaw(nodes, edges), merged)
  assert.equal(overview.filter(n => n.id === 'shared').length, 1)
  assert.equal(new Set(overview.map(n => n.id)).size, overview.length)
  for (const e of merged) {
    assert.ok(overview.some(n => n.id === e.source))
    assert.ok(overview.some(n => n.id === e.target))
  }
  assert.equal(merged.reduce((sum, e) => sum + e.count, 0), edges.length)
  assert.equal(JSON.stringify({ nodes, edges }), before)
})

test('large laws stay inside equally spaced clusters; smaller laws retain their own position', () => {
  const nodes = [law('a'), law('b'), law('c')]
  const edges = []
  for (let i = 0; i < 800; i++) {
    nodes.push(entity(`a-leaf-${i}`))
    edges.push(edge(`a-edge-${i}`, 'a', `a-leaf-${i}`))
  }
  nodes.push(entity('c-leaf'))
  edges.push(edge('c-edge', 'c', 'c-leaf'))
  const overview = layoutLawOverview(nodes, groupByLaw(nodes, edges), mergeDisplayEdges(nodes, edges))
  const a = overview.find(n => n.id === 'a')
  const b = overview.find(n => n.id === 'b')
  const c = overview.find(n => n.id === 'c')
  assert.ok(Math.hypot(a.x - b.x, a.y - b.y) >= 180)
  assert.ok(Math.hypot(a.x - c.x, a.y - c.y) >= 180)
  for (const n of overview.filter(n => n.id.startsWith('a-leaf-'))) {
    assert.ok(Math.hypot(n.x - a.x, n.y - a.y) <= 70.00001)
  }
  assert.ok(overview.some(n => n.id === 'b'), 'law with no visible relations still has an overview entry')
})

test('overview position is deterministic and empty filters produce a safe empty diagram', () => {
  const nodes = [law('a'), law('b'), entity('shared')]
  const edges = [edge('1', 'a', 'shared'), edge('2', 'b', 'shared')]
  const project = (ns, es) => layoutLawOverview(ns, groupByLaw(ns, es), mergeDisplayEdges(ns, es)).sort((a, b) => a.id.localeCompare(b.id))
  assert.deepEqual(project(nodes, edges), project(nodes.toReversed(), edges.toReversed()))
  assert.deepEqual(layoutLawOverview([], [], []), [])
  for (const n of project(nodes, edges)) assert.ok(Number.isFinite(n.x) && Number.isFinite(n.y))
})

test('same pipeline handles 30 non-pilot countries and multilingual laws without per-country branches', () => {
  const countries = 'US CA BR MX AR CL PE CO GB FR DE ES IT PT NL BE SE NO FI DK PL CZ AT CH JP KR IN ID TH VN'.split(' ')
  const titles = ['Synthetic fixture', 'مثال اصطناعي', 'Exemple synthétique', '合成测试文件']
  const nodes = []
  const edges = []
  for (const code of countries) for (let i = 0; i < 12; i++) {
    const id = `${code}-law-${i}`
    nodes.push({ ...law(id, titles[i % titles.length]), country_code: code })
    for (let j = 0; j < 15; j++) {
      const target = `${id}-entity-${j}`
      nodes.push({ ...entity(target), country_code: code })
      edges.push(edge(`${id}-evidence-${j}`, id, target))
    }
  }
  const before = JSON.stringify({ nodes, edges })
  const groups = groupByLaw(nodes, edges)
  const merged = mergeDisplayEdges(nodes, edges)
  const overview = layoutLawOverview(nodes, groups, merged)
  assert.equal(groups.length, 360)
  assert.equal(overview.length, 5760)
  assert.equal(merged.reduce((sum, e) => sum + e.count, 0), 5400)
  assert.ok(merged.every(e => !e.approved))
  const renamed = nodes.map(n => ({ ...n, country_code: 'ZZ', name_zh: `任意新名称 ${n.id}` }))
  const positions = ns => layoutLawOverview(ns, groupByLaw(ns, edges), mergeDisplayEdges(ns, edges))
    .map(n => [n.id, n.x, n.y]).sort((a, b) => a[0].localeCompare(b[0]))
  assert.deepEqual(positions(nodes), positions(renamed), 'country/title must not select a special layout algorithm')
  assert.equal(JSON.stringify({ nodes, edges }), before)
})

test('many instrument versions of one document stay in a bounded cluster', () => {
  const nodes = Array.from({ length: 80 }, (_, i) => law(`version-${i}`, 'Synthetic versions', 'same-document'))
  const overview = layoutLawOverview(nodes, groupByLaw(nodes, []), [])
  assert.equal(overview.length, 80)
  for (const n of overview) assert.ok(Math.hypot(n.x, n.y - 170) <= 18.00001)
})
