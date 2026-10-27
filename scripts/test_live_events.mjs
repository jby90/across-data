/** Node unit test of the shared fetch/SSE client. Does not control a browser. */
import assert from 'node:assert/strict'
import {parseSseBlock, startProgressStream} from '../packages/shared/src/liveEvents.ts'

assert.equal(parseSseBlock(': heartbeat'), null)
assert.deepEqual(parseSseBlock('id: 12\nevent: phase_started\ndata: {"phase":\ndata: "retrieval"}'),
  {id:'12',type:'phase_started',data:{phase:'retrieval'}})

const originalFetch = globalThis.fetch
let requests = []
let states = []
let received = []
let control
try {
  globalThis.fetch = async (url, options) => {
    requests.push({url, headers:options.headers})
    if (requests.length === 1) throw new Error('Synthetic network outage')
    const id = requests.length === 2 ? '9' : '10'
    const type = id === '9' ? 'phase_started' : 'completed'
    const text = `: ping\r\n\r\nid: ${id}\r\nevent: ${type}\r\ndata: {"task_id":"synthetic"}\r\n\r\n`
    const bytes = new TextEncoder().encode(text)
    return new Response(new ReadableStream({start(c) {
      // Split CRLF across chunks to exercise incremental framing.
      for (let i=0;i<bytes.length;i+=3) c.enqueue(bytes.slice(i,i+3))
      c.close()
    }}),{headers:{'Content-Type':'text/event-stream'}})
  }
  await new Promise((resolve,reject) => {
    const timeout = setTimeout(() => reject(new Error('SSE reconnect timed out')),7000)
    control=startProgressStream({url:'/api/synthetic/events',token:()=> 'synthetic-header-only',
      onState:s=>states.push(s),onUnauthorized:()=>reject(new Error('Unexpected auth failure')),
      onEvent:event=> {
        received.push(event)
        if(event.type==='completed'){ control.close();clearTimeout(timeout);resolve() }
      }})
  })
  assert(states.includes('polling') && states.includes('live'))
  assert.deepEqual(received.map(e=>e.type),['phase_started','completed'])
  assert.equal(requests[2].headers['Last-Event-ID'],'9')
  assert(requests.every(r=>!r.url.includes('synthetic-header-only')))
  assert(requests.every(r=>r.headers.Authorization==='Bearer synthetic-header-only'))
  let revoked=false
  globalThis.fetch=async()=>new Response('',{status:401})
  const bad=startProgressStream({url:'/api/synthetic/events',token:()=>null,
    onState:()=>{},onEvent:()=>{},onUnauthorized:()=>{revoked=true}})
  await new Promise(resolve=>setTimeout(resolve,20))
  bad.close()
  assert(revoked)
  console.log(JSON.stringify({checks:['incremental_frame','header_auth','polling_fallback','reconnect_replay','revoked_auth'],passed:5}))
} finally {control?.close(); globalThis.fetch=originalFetch}
