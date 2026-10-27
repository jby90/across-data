export interface ConversationSnapshot {
  id: string
  status: string
  messages_count: number
  latest_report_version: number
}

// Store only counters/state per signed-in user, never messages, facts or report content.
export function conversationStamp(task: ConversationSnapshot): string {
  return `${task.status}:${task.messages_count}:${task.latest_report_version}`
}

export function conversationSignal(task: Pick<ConversationSnapshot, 'status'>) {
  if (task.status === 'awaiting_user_facts') return { tone: 'amber', label: '有追问，请补充信息' }
  if (task.status === 'running') return { tone: 'blue', label: '正在分析' }
  if (task.status === 'completed') return { tone: 'green', label: '报告已生成' }
  if (task.status === 'partial_sample') return { tone: 'green', label: '报告已生成，部分维度有缺口' }
  if (task.status === 'failed') return { tone: 'red', label: '评估暂未完成' }
  return { tone: 'neutral', label: '等待您的回答' }
}

export function elapsedLabel(start: number, now: number): string {
  const seconds = Math.max(0, Math.floor((now - start) / 1000))
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

/** Receive actual bytes; report generation before response headers has no invented percentage. */
export async function receiveExport(response: Response, onBytes: (received: number, total: number | null) => void) {
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    throw new Error(typeof error?.detail === 'string' ? error.detail : `导出失败 (${response.status})`)
  }
  const size = Number(response.headers.get('content-length'))
  const total = size > 0 && !response.headers.get('content-encoding') ? size : null
  onBytes(0, total)
  if (!response.body) return response.blob()
  const reader = response.body.getReader()
  const chunks: BlobPart[] = []
  let received = 0
  try {
    while (true) {
      const {done, value} = await reader.read()
      if (done) break
      chunks.push(value.slice().buffer)
      received += value.byteLength
      onBytes(received, total)
    }
  } finally { reader.releaseLock() }
  return new Blob(chunks, {type: response.headers.get('content-type') || 'application/octet-stream'})
}
