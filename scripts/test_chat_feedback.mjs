import assert from 'node:assert/strict'
import test from 'node:test'
import {conversationStamp, conversationSignal, elapsedLabel, receiveExport} from '../apps/web-user/src/chatFeedback.ts'

test('conversation signals distinguish follow-up, ready report, running and failure without country rules', () => {
  for (const [status, tone] of [['awaiting_user_facts','amber'],['completed','green'],['partial_sample','green'],['running','blue'],['failed','red']]) {
    assert.equal(conversationSignal({status}).tone, tone)
    assert(conversationSignal({status}).label.length > 0)
  }
  const task = {id:'generic', status:'running', messages_count:4, latest_report_version:0}
  assert.notEqual(conversationStamp(task), conversationStamp({...task, status:'awaiting_user_facts', messages_count:5}))
  assert.notEqual(conversationStamp(task), conversationStamp({...task, status:'completed', latest_report_version:1}))
  assert.equal(conversationStamp(task), conversationStamp({...task, updated_at:'new progress event'}))
  assert.equal(elapsedLabel(1000, 65000), '1 分 4 秒')
  assert.equal(elapsedLabel(1000, 0), '0 秒')
})

test('export reports actual byte progress and preserves PDF bytes', async () => {
  const progress = []
  const response = new Response(new ReadableStream({start(c) {
    c.enqueue(new Uint8Array([37,80,68])); c.enqueue(new Uint8Array([70])); c.close()
  }}), {headers:{'content-type':'application/pdf','content-length':'4'}})
  const blob = await receiveExport(response, (received,total) => progress.push([received,total]))
  assert.equal(await blob.text(), '%PDF')
  assert.equal(blob.type, 'application/pdf')
  assert.deepEqual(progress, [[0,4],[3,4],[4,4]])
})

test('compressed/unknown length has no fabricated percentage; server rejection and broken transfer propagate', async () => {
  const progress=[]
  await receiveExport(new Response('abc',{headers:{'content-length':'99','content-encoding':'gzip'}}), (n,total)=>progress.push(total))
  assert(progress.every(total=>total===null))
  await assert.rejects(receiveExport(new Response(JSON.stringify({detail:'来源已隔离'}),{status:409}),()=>{}), /来源已隔离/)
  await assert.rejects(receiveExport(new Response(new ReadableStream({start(c){c.error(new Error('broken transfer'))}})),()=>{}), /broken transfer/)
})
