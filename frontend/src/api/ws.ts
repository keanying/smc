/**
 * WebSocket 封装：任务实时日志、交互式登录浏览器推流。
 *
 * 两个都做了自动重连（指数退避），因为采集任务可能跑几十分钟，
 * 中间网络抖一下不该让用户以为任务断了。
 */

function wsUrl(path: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}${path}`
}

export interface TaskLogMessage {
  type: 'log' | 'ping'
  data?: {
    task_id: string
    log_time: string
    level: string
    channel?: string | null
    message: string
  }
}

export class TaskLogSocket {
  private socket: WebSocket | null = null
  private retries = 0
  private closedByUser = false

  constructor(
    private readonly taskId: string,
    private readonly onLog: (log: NonNullable<TaskLogMessage['data']>) => void,
    private readonly onStateChange?: (connected: boolean) => void,
  ) {}

  open(): void {
    this.closedByUser = false
    this.socket = new WebSocket(wsUrl(`/ws/tasks/${this.taskId}/logs`))

    this.socket.onopen = () => {
      this.retries = 0
      this.onStateChange?.(true)
    }
    this.socket.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data) as TaskLogMessage
        if (payload.type === 'log' && payload.data) this.onLog(payload.data)
      } catch {
        /* 心跳等非 JSON 帧直接忽略 */
      }
    }
    this.socket.onclose = () => {
      this.onStateChange?.(false)
      if (this.closedByUser) return
      // 指数退避重连，上限 15 秒
      const delay = Math.min(1000 * 2 ** this.retries, 15_000)
      this.retries += 1
      setTimeout(() => this.open(), delay)
    }
    this.socket.onerror = () => this.socket?.close()
  }

  close(): void {
    this.closedByUser = true
    this.socket?.close()
    this.socket = null
  }
}

export interface BrowserFrame {
  type: 'frame'
  data: string
  width: number
  height: number
}

export interface BrowserStatus {
  type: 'status'
  state: string
  message: string
  logged_in?: boolean
}

export type BrowserMessage = BrowserFrame | BrowserStatus

/**
 * 交互式登录：服务端推 JPEG 帧，前端回传鼠标键盘事件。
 * 不做自动重连——登录会话在服务端是一次性的，断了就该重新发起。
 */
export class LoginBrowserSocket {
  private socket: WebSocket | null = null

  constructor(
    private readonly sessionId: string,
    private readonly onMessage: (message: BrowserMessage) => void,
    private readonly onClose?: () => void,
  ) {}

  open(): void {
    this.socket = new WebSocket(wsUrl(`/ws/browser/${this.sessionId}`))
    this.socket.onmessage = (event) => {
      try {
        this.onMessage(JSON.parse(event.data) as BrowserMessage)
      } catch {
        /* ignore */
      }
    }
    this.socket.onclose = () => this.onClose?.()
  }

  send(event: Record<string, unknown>): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(event))
    }
  }

  close(): void {
    this.send({ type: 'close' })
    this.socket?.close()
    this.socket = null
  }
}

export interface LiveViewInfo {
  view_id: string
  task_id: string
  task_name: string
  channel: string
  account_name: string
  scenic_name: string
  engine: string
  url: string
  title: string
  step: string
  running_seconds: number
  viewers: number
  streaming: boolean
  /** 是否处于人工接管（服务端为准，前端只跟着显示） */
  takeover: boolean
  takeover_seconds: number
  /** 接管期间已注入多少个事件，事后对账用 */
  injected: number
}

export type LiveViewMessage =
  | BrowserFrame
  | BrowserStatus
  | { type: 'meta'; data: LiveViewInfo }

/**
 * 采集实时画面：**默认只读，可以显式接管**。
 *
 * 以前这里没有 send()，是刻意做成只读的。加上是因为采集跑到一半弹**拖动验证**
 * 时，只读会把人堵死：页面等人拖、采集器等页面，最后耗到看门狗把任务杀掉。
 * 但输入只在服务端确认接管之后才生效（见 app/api/live.py 的协议说明），
 * 手滑点一下画面不会有任何事。
 * 采集器正按自己的节奏在这个页面上走流程，观众点一下，
 * 它的下一步就落在一个没预料到的页面上，而且事后无法归因。
 *
 * 会自动重连（采集经常要跑几十分钟），但服务端明确说会话已结束时就不再重连。
 */
export class LiveViewSocket {
  private socket: WebSocket | null = null
  private retries = 0
  private stopped = false

  constructor(
    private readonly viewId: string,
    private readonly onMessage: (message: LiveViewMessage) => void,
    private readonly onStateChange?: (connected: boolean) => void,
  ) {}

  open(): void {
    this.stopped = false
    this.socket = new WebSocket(wsUrl(`/ws/live/${encodeURIComponent(this.viewId)}`))
    this.socket.onopen = () => {
      this.retries = 0
      this.onStateChange?.(true)
    }
    this.socket.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data) as LiveViewMessage
        // 会话结束是终态，不该再退避重连——那只会每 15 秒刷一次同样的提示
        if (message.type === 'status' && message.state === 'gone') this.stopped = true
        this.onMessage(message)
      } catch {
        /* ignore */
      }
    }
    this.socket.onclose = () => {
      this.onStateChange?.(false)
      if (this.stopped) return
      const delay = Math.min(1000 * 2 ** this.retries, 15_000)
      this.retries += 1
      setTimeout(() => { if (!this.stopped) this.open() }, delay)
    }
    this.socket.onerror = () => this.socket?.close()
  }

  /**
   * 往采集页面发一个事件。没连上就直接丢——排队重发在拖滑块这个场景里
   * 只会更糟：断线期间攒下的一串轨迹点，恢复后一股脑打过去，
   * 落在页面上是一条瞬移的直线，验证必然不过。
   */
  send(event: Record<string, unknown>): void {
    if (this.socket?.readyState !== WebSocket.OPEN) return
    try {
      this.socket.send(JSON.stringify(event))
    } catch {
      /* ignore */
    }
  }

  close(): void {
    this.stopped = true
    this.socket?.close()
    this.socket = null
  }
}
