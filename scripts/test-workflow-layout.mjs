import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'

// Use the app's existing TypeScript compiler; no extra test/runtime dependency.
const require = createRequire(new URL('../apps/web-admin/package.json', import.meta.url))
const ts = require('typescript')
const source = await readFile(
  new URL('../apps/web-admin/src/components/workflowLayout.ts', import.meta.url),
  'utf8',
)
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText
const { spacedLayout, dragPosition, readPositions, NODE_WIDTH, NODE_GAP } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`
)

const nodes = [
  { node_id: 'start', position_x: 60, position_y: 190 },
  { node_id: 'analysis', position_x: 350, position_y: 90 },
  { node_id: 'verify', position_x: 350, position_y: 120 },
  { node_id: 'report', position_x: 650, position_y: 190 },
]

test('variable-height cards never overlap in the default layout; configured order stays intact', () => {
  const before = JSON.stringify(nodes)
  const heights = { start: 210, analysis: 320, verify: 270, report: 240 }
  const positions = spacedLayout(nodes, heights)
  assert.ok(positions.verify.y - positions.analysis.y - heights.analysis >= NODE_GAP)
  assert.ok(positions.analysis.x - positions.start.x > NODE_WIDTH)
  assert.ok(positions.report.x - positions.verify.x > NODE_WIDTH)
  assert.equal(JSON.stringify(nodes), before, 'UI layout must not mutate the workflow DSL')
})

test('drag distance accounts for zoom and scrolling in screen-pixel deltas', () => {
  assert.deepEqual(dragPosition({ x: 100, y: 200 }, 80, 40, 0.5), { x: 260, y: 280 })
  assert.deepEqual(dragPosition({ x: 100, y: 200 }, 80, 40, 1.25), { x: 164, y: 232 })
})

test('dragging cannot lose cards beyond the upper/left edge or grow an unbounded canvas', () => {
  assert.deepEqual(dragPosition({ x: 100, y: 200 }, -1000, -1000, 0.5), { x: 24, y: 24 })
  assert.deepEqual(dragPosition({ x: 100, y: 200 }, 100000, 100000, 0.5), { x: 10000, y: 10000 })
})

test('saved layout round-trips; stale nodes and invalid coordinates are discarded', () => {
  const raw = JSON.stringify({
    version: 1,
    positions: {
      start: { x: 88, y: 399 },
      analysis: { x: 'not a number', y: 10 },
      verify: { x: null, y: 300 },
      obsolete: { x: 500, y: 500 },
    },
  })
  assert.deepEqual(readPositions(raw, nodes), { start: { x: 88, y: 399 } })
})

test('corrupt or incompatible browser storage safely restores default positions', () => {
  for (const raw of [null, '', 'broken JSON', '{}', '{"version":2,"positions":{}}']) {
    assert.deepEqual(readPositions(raw, nodes), {})
  }
  assert.deepEqual(spacedLayout([], {}), {})
})
