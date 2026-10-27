import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'

const require = createRequire(new URL('../apps/web-admin/package.json', import.meta.url))
const ts = require('typescript')
const source = await readFile(new URL('../packages/shared/src/graphPresentation.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText
const { graphSeries } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const categories = [{ type: 'INSTRUMENT', name: '法规', color: '#7c3aed' }]
const law = { id: 'law', name: '多语言法规 / قانون / Texte', type: 'INSTRUMENT', degree: 800 }
const edges = [{ source: 'law', target: 'country', label: '适用场景 ×12', approved: false }]
const nodes = [law, { id: 'country', name: '新增国家', type: 'COUNTRY', degree: 1 }]

test('overview and detail both use force layout and actual names without fixed grid coordinates', () => {
  for (const overview of [true, false]) {
    const series = graphSeries(nodes, edges, categories, { overview })
    assert.equal(series.layout, 'force')
    assert.equal(series.roam, true)
    assert.equal(series.draggable, true)
    assert.equal(series.data[0].name, law.name)
    assert.equal('x' in series.data[0] || 'y' in series.data[0], false)
    assert.equal(series.data[0].label.formatter, undefined)
  }
})

test('small and large graphs never enable rotated edge labels automatically or on emphasis', () => {
  for (const count of [1, 6, 20, 21, 5400]) {
    const series = graphSeries(nodes, Array.from({ length: count }, () => edges[0]), categories)
    assert.equal(series.edgeLabel.show, false)
    assert.ok(series.links.every(e => !e.label.show && !e.emphasis.label.show))
    assert.ok(series.links.every(e => e.name === '适用场景 ×12'))
    assert.equal(series.links.length, count)
    assert.equal(series.labelLayout.hideOverlap, true)
  }
})

test('text and node sizes are bounded for dense hubs and selected nodes', () => {
  for (const overview of [true, false]) {
    const series = graphSeries(nodes, edges, categories, { overview, selectedId: law.id })
    assert.ok(series.label.fontSize <= 12)
    assert.ok(series.emphasis.label.fontSize <= 12)
    assert.equal(series.nodeScaleRatio, 0)
    assert.ok(series.data.every(n => n.symbolSize <= 32))
  }
})

test('dense overviews keep every node but limit default labels according to available width', () => {
  const many = Array.from({ length: 100 }, (_, i) => ({ ...law, id: `law-${i}`, degree: 100 - i }))
  for (const width of [320, 728, 1440]) {
    const series = graphSeries(many, [], categories, { overview: true, viewportWidth: width })
    assert.equal(series.data.length, 100)
    assert.ok(series.data.filter(n => n.label.show).length <= Math.max(2, Math.floor(width / 140)))
    assert.ok(series.data.find(n => n.id === 'law-0').label.show)
    assert.equal(series.emphasis.label.show, undefined, 'adjacency hover must not reveal all hidden node labels')
  }
  const larger = Array.from({ length: 900 }, (_, i) => ({ ...law, id: `large-${i}` }))
  assert.ok(graphSeries(many, [], categories).zoom <= graphSeries(nodes, edges, categories).zoom)
  assert.ok(graphSeries(larger, [], categories).zoom < graphSeries(many, [], categories).zoom)
})

test('projection preserves pending styling, exact relations and source input; reduced motion remains supported', () => {
  const before = JSON.stringify({ nodes, edges, categories })
  const series = graphSeries(nodes, edges, categories, { overview: true, reducedMotion: true })
  assert.equal(series.force.layoutAnimation, false)
  assert.deepEqual(series.links.map(e => [e.source, e.target, e.name]), [['law', 'country', '适用场景 ×12']])
  assert.equal(series.links[0].lineStyle.color, '#94a3b8')
  assert.equal(graphSeries(nodes, [{ ...edges[0], approved: true }], categories).links[0].lineStyle.color, '#16a34a')
  assert.equal(JSON.stringify({ nodes, edges, categories }), before)
  assert.deepEqual(graphSeries([], [], []).data, [])
})

test('both apps use shared canvas rules including mini graphs; no local font override reintroduces oversized labels', async () => {
  for (const app of ['web-admin', 'web-user']) {
    const component = await readFile(new URL(`../apps/${app}/src/components/KgForceGraph.vue`, import.meta.url), 'utf8')
    assert.match(component, /series: \[graphSeries\(props.nodes, props.edges, props.categories/)
    assert.doesNotMatch(component, /showEdgeLabel|overviewLabel|layout: props.overview/)
  }
})
