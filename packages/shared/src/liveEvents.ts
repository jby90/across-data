export interface ProgressEvent {
  type: string
  id?: string
  data: Record<string, unknown>
}
export type StreamState = 'connecting' | 'live' | 'polling' | 'idle'

export function parseSseBlock(block: string): ProgressEvent | null {
  let type = 'message'
  let id: string | undefined
  const data: string[] = []
  for (const line of block.split('\n')) {
    const colon = line.indexOf(':')
    if (colon < 1) continue
    const field = line.slice(0, colon)
    const value = line.slice(colon + 1).replace(/^ /, '')
    if (field === 'event') type = value
    if (field === 'id' && /^\d+$/.test(value)) id = value
    if (field === 'data') data.push(value)
  }
  if (!data.length) return null
  return { type, id, data: JSON.parse(data.join('\n')) }
}

/** Authorization stays in the header; committed event IDs support reconnect replay. */
export function startProgressStream(options: {
  url: string
  token: () => string | null
  onEvent: (event: ProgressEvent) => void | Promise<void>
  onState: (state: StreamState) => void
  onUnauthorized: () => void
}) {
  let closed = false
  let controller: AbortController | undefined
  let lastId = ''
  let retryTimer: ReturnType<typeof setTimeout> | undefined
  let cancelRetry: (() => void) | undefined
  const delay = (ms: number) => new Promise<void>(resolve => {
    cancelRetry = resolve
    retryTimer = setTimeout(resolve, ms)
  })
  async function connect() {
    let failures = 0
    while (!closed) {
      controller = new AbortController()
      options.onState(failures ? 'polling' : 'connecting')
      try {
        const headers: Record<string, string> = {
          Accept: 'text/event-stream', Authorization: `Bearer ${options.token() || ''}`,
        }
        if (lastId) headers['Last-Event-ID'] = lastId
        const response = await fetch(options.url, {headers, signal:controller.signal, cache:'no-store'})
        if (response.status === 401 || response.status === 403) {
          options.onUnauthorized()
          return
        }
        if (!response.ok || !response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
          throw new Error('Progress stream unavailable')
        }
        options.onState('live')
        failures = 0
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        try {
          while (!closed) {
            const {done, value} = await reader.read()
            if (done) break
            buffer = (buffer + decoder.decode(value, {stream:true})).replace(/\r\n/g, '\n')
            if (buffer.length > 1000000) throw new Error('Oversized progress event')
            let boundary: number
            while ((boundary = buffer.indexOf('\n\n')) >= 0) {
              const event = parseSseBlock(buffer.slice(0, boundary))
              buffer = buffer.slice(boundary + 2)
              if (!event) continue
              if (event.type === 'access_revoked') { options.onUnauthorized(); return }
              if (event.type === 'connected' && /^\d+$/.test(String(event.data.cursor))) {
                lastId = String(event.data.cursor)
              }
              await options.onEvent(event)
              if (event.id) lastId = event.id
            }
          }
        } finally { await reader.cancel().catch(() => {}) }
      } catch {
        // The view retains its current state and resumes its existing polling path.
      }
      if (closed) break
      options.onState('polling')
      await delay(Math.min(10000, 1000 * 2 ** Math.min(failures++, 4)))
    }
  }
  void connect()
  return {close() {
    closed = true
    controller?.abort()
    clearTimeout(retryTimer)
    cancelRetry?.()
    options.onState('idle')
  }}
}
