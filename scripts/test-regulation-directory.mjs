import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'

const require = createRequire(new URL('../apps/web-admin/package.json', import.meta.url))
const ts = require('typescript')
const source = await readFile(new URL('../packages/shared/src/regulationDirectory.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText
const { directoryLevels, directoryCountries, documentPage } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`,
)
const doc = (id, country = 'SG', type = 'LAW', title = id) => ({
  id, country_code: country, document_type: type, title,
  evidence: 'original evidence', review_status: 'pending_review', versions: ['original-version'],
})

test('country/type grouping preserves every file, its identity and original metadata', () => {
  const documents = [doc('1'), doc('2', 'SG', 'GUIDANCE'), doc('3', 'EG'), doc('4', 'EG', 'REGULATION')]
  const before = JSON.stringify(documents)
  const countries = directoryCountries(documents)
  assert.deepEqual(countries.map(c => [c.code, c.count]), [['EG', 2], ['SG', 2]])
  const groups = countries.flatMap(c => directoryLevels(documents, c.code))
  assert.equal(groups.reduce((sum, g) => sum + g.items.length, 0), documents.length)
  assert.equal(groups.find(g => g.items.some(d => d.id === '1')).items.find(d => d.id === '1'), documents[0])
  assert.equal(JSON.stringify(documents), before)
})

test('levels follow metadata order; misleading titles do not change file classification', () => {
  const groups = directoryLevels([
    doc('guide', 'SG', 'GUIDANCE', 'An Act to amend the Law'),
    doc('form', 'SG', 'FORM'), doc('rule', 'SG', 'REGULATION'), doc('law'),
  ], 'SG')
  assert.deepEqual(groups.map(g => g.key), ['LAW', 'REGULATION', 'GUIDANCE', 'FORM'])
  assert.equal(groups.find(g => g.key === 'GUIDANCE').items[0].id, 'guide')
})

test('same titles never merge distinct documents or versions', () => {
  const groups = directoryLevels([doc('a', 'SG', 'LAW', 'Same title'), doc('b', 'SG', 'LAW', 'Same title')], 'SG')
  assert.deepEqual(groups[0].items.map(d => d.id), ['a', 'b'])
})

test('unknown countries and custom/missing document types remain visible', () => {
  const documents = [doc('custom', 'XX', 'CUSTOM'), doc('missing', 'XX', '')]
  assert.equal(directoryCountries(documents)[0].name, 'XX')
  const levels = directoryLevels(documents, 'XX')
  assert.equal(levels.length, 2)
  assert.equal(levels.find(g => g.key === '__unclassified__').name, '未分类')
  assert.ok(levels.some(g => g.name.includes('CUSTOM')))
})

test('large categories are fully reachable in small pages, with correct last-page counts', () => {
  const documents = Array.from({ length: 105 }, (_, i) => doc(`law-${i}`))
  const items = directoryLevels(documents, 'SG')[0].items
  const visited = new Set()
  for (let page = 1; page <= documentPage(items, 1).pages; page++) {
    const view = documentPage(items, page)
    assert.ok(view.items.length <= 8)
    for (const d of view.items) visited.add(d.id)
  }
  assert.equal(visited.size, 105)
  assert.equal(documentPage(items, 99).items.length, 1)
})

test('empty search results and stale pagination shrink safely without showing other-country documents', () => {
  assert.deepEqual(directoryLevels([doc('eg', 'EG')], 'SG'), [])
  assert.deepEqual(documentPage([], 99), { page: 1, pages: 1, items: [] })
  assert.deepEqual(documentPage([1, 2, 3], 99, 2), { page: 2, pages: 2, items: [3] })
  assert.deepEqual(documentPage([1], -5, 0), { page: 1, pages: 1, items: [1] })
})

test('file ordering is stable and natural, independent of API response order', () => {
  const documents = [doc('10', 'SG', 'LAW', 'Regulation 10'), doc('2', 'SG', 'LAW', 'Regulation 2')]
  assert.deepEqual(directoryLevels(documents, 'SG')[0].items.map(d => d.id), ['2', '10'])
  assert.deepEqual(directoryLevels(documents.toReversed(), 'SG'), directoryLevels(documents, 'SG'))
})

test('admin and user directory components stay identical', async () => {
  const files = await Promise.all(['web-admin', 'web-user'].map(app => readFile(
    new URL(`../apps/${app}/src/components/RegulationDirectory.vue`, import.meta.url), 'utf8')))
  assert.equal(files[0].replace(/\r\n/g, '\n'), files[1].replace(/\r\n/g, '\n'))
})
