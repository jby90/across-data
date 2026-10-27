import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'
const require = createRequire(new URL('../apps/web-user/package.json', import.meta.url))
const ts = require('typescript')
const source = await readFile(
  new URL('../packages/shared/src/readerRelations.ts', import.meta.url),
  'utf8',
)
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText
const { readerNeighborhood, readerRoot, mergeReaderPage, readerLabel, readerPositions } =
  await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const node = (id, type = 'OBLIGATION') => ({
  id,
  type,
  name: id,
  original_name: null,
})
const edge = (id, source, target, type = 'REQUIRES') => ({
  id,
  source,
  target,
  relation_type: type,
  label: type,
  clause_id: 'clause-' + id,
  clause_number: '1',
  quote: 'Original.',
  review_status: 'pending_review',
})
test('large hubs have bounded labels and do not starve less frequent relation types', () => {
  const nodes = [
    node('law', 'INSTRUMENT'),
    ...Array.from({ length: 120 }, (_, i) => node(String(i))),
  ]
  const edges = Array.from({ length: 120 }, (_, i) =>
    edge(String(i), 'law', String(i), i === 119 ? 'EXEMPTS' : 'REQUIRES'),
  )
  const before = JSON.stringify({ nodes, edges })
  const view = readerNeighborhood(nodes, edges, readerRoot(nodes, edges))
  assert.equal(view.nodes.length, 15)
  assert.equal(view.incidentCount, 120)
  assert.ok(view.edges.some((e) => e.relation_type === 'EXEMPTS'))
  assert.equal(JSON.stringify({ nodes, edges }), before)
})
test('disconnected clause relations, incoming direction, cycles and self-loops remain reachable without invented lines', () => {
  const nodes = ['law', 'a', 'b', 'c'].map((id) => node(id))
  const edges = [
    edge('1', 'law', 'a'),
    edge('2', 'b', 'c'),
    edge('3', 'c', 'b'),
    edge('4', 'c', 'c'),
  ]
  assert.equal(readerNeighborhood(nodes, edges, 'law').edges.length, 1)
  assert.deepEqual(
    readerNeighborhood(nodes, edges, 'c').edges.map((e) => e.id),
    ['2', '3', '4'],
  )
  assert.equal(readerNeighborhood(nodes, edges, 'missing').nodes.length, 0)
  assert.equal(readerNeighborhood(nodes, [edge('bad', 'law', 'missing')], 'law').edges.length, 0)
})
test('merging lines preserves every evidence and direction; review states remain untouched', () => {
  const edges = [
    edge('1', 'a', 'b'),
    edge('2', 'a', 'b'),
    edge('3', 'b', 'a'),
    edge('4', 'a', 'b', 'PROHIBITS'),
  ]
  edges[0].review_status = 'approved'
  const view = readerNeighborhood([node('a'), node('b')], edges, 'a')
  assert.equal(view.lines.length, 3)
  assert.deepEqual(view.lines[0].ids, ['1', '2'])
  assert.deepEqual(
    view.edges.map((e) => e.review_status),
    ['approved', 'pending_review', 'pending_review', 'pending_review'],
  )
})
test('pagination deduplicates exact IDs, never conflates countries/documents/versions or equal labels', () => {
  const first = {
    document_id: 'doc',
    version_id: 'v1',
    total: 3,
    offset: 0,
    limit: 2,
    nodes: [node('a'), node('b')],
    relations: [edge('1', 'a', 'b'), edge('2', 'b', 'a')],
  }
  const second = {
    ...first,
    offset: 2,
    nodes: [node('a'), node('c')],
    relations: [edge('2', 'b', 'a'), edge('3', 'a', 'c')],
  }
  const result = mergeReaderPage(first, second)
  assert.equal(result.relations.length, 3)
  assert.equal(result.nodes.length, 3)
  assert.equal(mergeReaderPage(first, { ...second, version_id: 'v2' }).relations.length, 2)
  assert.equal(mergeReaderPage(first, { ...second, document_id: 'other' }).relations.length, 2)
})
test('multilingual unknown nodes have readable labels, bounded Unicode labels and deterministic roots', () => {
  const nodes = [node('عربي', 'NEW_TYPE'), node('法😀文'.repeat(30), 'INSTRUMENT')]
  assert.equal(readerRoot(nodes, []), nodes[1].id)
  const label = readerLabel(nodes[1].id)
  assert.equal(label.split('\n').length, 2)
  assert.ok(label.endsWith('…'))
  assert.ok(!label.includes('\uFFFD'))
  assert.equal(readerRoot([], []), '')
})

test('sparse and dense graphs have finite non-degenerate coordinates, including a lone relation', () => {
  for (const size of [1, 2, 3, 4, 15, 35]) {
    const nodes = Array.from({ length: size }, (_, i) => node(String(i)))
    const positions = [...readerPositions(nodes, '0').values()]
    assert.ok(positions.every((p) => Number.isFinite(p.x) && Number.isFinite(p.y)))
    if (size > 1) {
      const xs = positions.map((p) => p.x),
        ys = positions.map((p) => p.y)
      const aspect = (Math.max(...xs) - Math.min(...xs)) / (Math.max(...ys) - Math.min(...ys))
      assert.ok(aspect > 0.4 && aspect < 2)
    }
  }
})
