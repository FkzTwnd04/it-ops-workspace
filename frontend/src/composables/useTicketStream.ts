import { ref, onBeforeUnmount } from 'vue'
import { API_BASE } from '@/api/client'
import { TOKEN_KEY } from '@/stores/auth'

export interface ProgressEvent {
  type: 'status' | 'stage' | 'todos' | 'subagent_start' | 'subagent_result' | 'tool_call' | 'tool_result'
    | 'agent_message' | 'awaiting' | 'error' | 'done' | 'ping'
  ts?: number
  [key: string]: any
}

/** 订阅工单处理进度（SSE）。EventSource 不能带 Authorization 头，所以用 fetch 读流。 */
export function useTicketStream(onEvent: (ev: ProgressEvent) => void) {
  const streaming = ref(false)
  let controller: AbortController | null = null

  async function connect(ticketId: string) {
    close()
    controller = new AbortController()
    streaming.value = true
    try {
      const res = await fetch(`${API_BASE}/tickets/${ticketId}/stream`, {
        headers: { Authorization: `Bearer ${localStorage.getItem(TOKEN_KEY) ?? ''}` },
        signal: controller.signal,
      })
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const blocks = buffer.split('\n\n')
        buffer = blocks.pop() ?? ''
        for (const block of blocks) {
          const line = block.split('\n').find(l => l.startsWith('data: '))
          if (!line) continue
          try {
            const ev = JSON.parse(line.slice(6)) as ProgressEvent
            if (ev.type !== 'ping') onEvent(ev)
          } catch {
            // 忽略残缺数据
          }
        }
      }
    } catch (e) {
      if ((e as Error).name !== 'AbortError') onEvent({ type: 'error', message: '进度连接中断' })
    } finally {
      streaming.value = false
    }
  }

  function close() {
    controller?.abort()
    controller = null
  }

  onBeforeUnmount(close)
  return { streaming, connect, close }
}
