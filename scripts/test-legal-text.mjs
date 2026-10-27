import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { splitLegalText } from '../packages/shared/src/legalText.ts'

const term = (id, source, target, kind='terminology_only', extra={}) => ({
  id, source_term:source, target_zh:target, source_kind:kind,
  source_label:kind, source_reference:id, ...extra,
})
test('hover spans preserve every source character, numbers, scripts and markup literally', () => {
  const raw='<script>alert(1)</script> Personal data, البيانات الشخصية — 2026 / $1,000.'
  const terms=[term('pd','personal data','个人数据'),term('ar','البيانات الشخصية','个人数据')]
  const before=JSON.stringify(terms)
  const pieces=splitLegalText(raw,terms)
  assert.equal(pieces.map(p=>p.text).join(''),raw)
  assert.equal(pieces.filter(p=>p.term).length,2)
  assert.equal(JSON.stringify(terms),before)
})
test('whole-word aliases avoid hardware and unrelated words, Chinese renderings still explainable', () => {
  const t=term('controller','controller','数据控制者','terminology_only',{concept_code:'data_controller'})
  assert.equal(splitLegalText('A controller operates vehicle brakes.',[t]).filter(p=>p.term).length,0)
  assert.equal(splitLegalText('The controller processes personal data.',[t]).filter(p=>p.term).length,1)
  assert.equal(splitLegalText('microcontroller processes personal data.',[t]).filter(p=>p.term).length,0)
  assert.equal(splitLegalText('数据控制者负责数据处理。',[t]).filter(p=>p.term).length,1)
})
test('a source-bound legal definition outranks a broader glossary and guide', () => {
  const guide=term('guide','controller','数据控制者','regulator_guidance')
  const law=term('law','controller','controller','law_definition',{definition_original:'An embedded vehicle unit.'})
  const pieces=splitLegalText('The controller operates the vehicle.',[guide,law])
  assert.equal(pieces.find(p=>p.term).term.id,'law')
})
test('different definitions are presented as alternatives, never silently merged or overwritten', () => {
  const a=term('a','personal data','个人数据','law_definition',{definition_original:'Definition A.'})
  const b=term('b','personal data','个人数据','law_definition',{definition_original:'Definition B.'})
  const pieces=splitLegalText('Personal data is processed.',[a,b])
  assert.equal(pieces[0].term.alternatives.length,2)
  assert.notEqual(pieces[0].term,a)
})
test('ordinary user pages use business navigation; administrative graph remains available', async () => {
  const pages=await Promise.all(['KnowledgeBase.vue','ChatAnalysis.vue'].map(file=>readFile(new URL(`../apps/web-user/src/views/${file}`,import.meta.url),'utf8')))
  assert.ok(pages.every(p=>!p.includes('<KgForceGraph')&&!p.includes('<KgLawGroups')))
  const admin=await readFile(new URL('../apps/web-admin/src/views/KnowledgeGraph.vue',import.meta.url),'utf8')
  assert.ok(admin.includes('<KgForceGraph'))
})
