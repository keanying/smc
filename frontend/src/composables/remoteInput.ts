// CloudSense（云听）· Copyright (c) 2026 keanying
// SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
// 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
// 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
// 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

/**
 * 实时推流画面上的鼠标 / 滚轮 / 键盘 → 回放到服务端浏览器。登录窗口和任务实时画面共用。
 *
 * 之前踩过的坑，这里统一处理：
 * - **一次点击变两次**：同时发了 mousedown + mouseup 和 click，服务端各执行一遍，
 *   播放 / 暂停这类按钮切了两下等于没点。现在只发按下 / 松开（带点击次数，双击也对）。
 * - **拖动时整页跟着走**：画面是 <img>，按住拖动会触发浏览器原生的「拖图片」
 *   （下载插件还会弹「Drag & drop here」），之后的移动、松开都收不到，
 *   服务端一直以为左键按着。现在用 Pointer 事件 + setPointerCapture：
 *   拖出画面、松手在画面外都能收到，原生拖拽也禁掉了。
 * - **移动事件太多**：一秒几百个 mousemove 全发过去会排长队，后面的点击要等很久。
 *   限到每 16ms 一个，并保证最后一个点不丢（拖滑块要停在准确的位置）。
 * - **滚轮总滚页面中间**：滚动前先把鼠标移到当前位置，滚的就是鼠标下面那块。
 */

export interface FrameSize { width: number; height: number }

export interface RemoteInputOptions {
  /** 发一条消息给服务端 */
  send: (message: Record<string, unknown>) => void
  /** 当前帧的像素尺寸（坐标要换算到这个尺寸） */
  frameSize: () => FrameSize
  /** 什么时候才回放（任务实时画面要先「接管」）；不传 = 一直回放 */
  enabled?: () => boolean
  /** 没按键时也发移动（悬停菜单、悬停才出现的按钮）；默认 true */
  hover?: boolean
}

const MOVE_INTERVAL_MS = 16
const WHEEL_INTERVAL_MS = 40
/** 两次按下间隔多久、离多近算双击（和系统默认差不多） */
const MULTI_CLICK_MS = 500
const MULTI_CLICK_PX = 6

/** 要按「键名」发过去的特殊键（其余单个字符按文字输入） */
export const SPECIAL_KEYS = new Set([
  'Enter', 'Backspace', 'Tab', 'Escape', 'Delete', 'Insert', 'Home', 'End', 'PageUp', 'PageDown',
  'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', ' ',
  'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12',
])

const BUTTONS: Record<number, string> = { 0: 'left', 1: 'middle', 2: 'right' }

export function useRemoteInput(options: RemoteInputOptions) {
  const enabled = () => (options.enabled ? options.enabled() : true)
  const hover = options.hover !== false

  let pressedButton: string | null = null
  let captureEl: Element | null = null
  let last = { x: 0, y: 0 }
  let lastMoveSentAt = 0
  let pendingMove: { x: number; y: number } | null = null
  let moveTimer: number | undefined
  let lastDown = { at: 0, x: 0, y: 0, button: '', count: 0 }
  let wheel = { dx: 0, dy: 0 }
  let wheelTimer: number | undefined

  /** 屏幕坐标 → 帧像素坐标，夹在画面范围内（拖出画面时按边缘算） */
  function toFrame(event: MouseEvent, el: Element) {
    const rect = el.getBoundingClientRect()
    const size = options.frameSize()
    if (!rect.width || !rect.height) return { x: 0, y: 0 }
    const x = ((event.clientX - rect.left) / rect.width) * size.width
    const y = ((event.clientY - rect.top) / rect.height) * size.height
    return {
      x: Math.round(Math.min(Math.max(x, 0), size.width - 1)),
      y: Math.round(Math.min(Math.max(y, 0), size.height - 1)),
    }
  }

  function flushMove() {
    window.clearTimeout(moveTimer)
    moveTimer = undefined
    if (pendingMove) {
      options.send({ type: 'mousemove', ...pendingMove })
      lastMoveSentAt = performance.now()
      pendingMove = null
    }
  }

  function onPointerDown(event: PointerEvent) {
    if (!enabled() || event.pointerType === 'touch' && !event.isPrimary) return
    const button = BUTTONS[event.button]
    if (!button) return
    event.preventDefault()
    const el = event.currentTarget as Element
    // 拿到焦点，键盘才打得进去（画面外层带 tabindex 的那个元素）
    ;(el.closest('[tabindex]') as HTMLElement | null)?.focus({ preventScroll: true })
    try {
      el.setPointerCapture(event.pointerId)
      captureEl = el
    } catch { /* 个别浏览器不支持就算了，靠 lostpointercapture 兜底 */ }
    flushMove()
    const at = toFrame(event, el)
    const now = performance.now()
    const near = Math.abs(at.x - lastDown.x) <= MULTI_CLICK_PX && Math.abs(at.y - lastDown.y) <= MULTI_CLICK_PX
    const count = near && button === lastDown.button && now - lastDown.at < MULTI_CLICK_MS
      ? Math.min(lastDown.count + 1, 3) : 1
    lastDown = { at: now, x: at.x, y: at.y, button, count }
    last = at
    pressedButton = button
    options.send({ type: 'mousedown', ...at, button, clickCount: count })
  }

  function onPointerMove(event: PointerEvent) {
    if (!enabled()) return
    if (!pressedButton && !hover) return
    const el = (captureEl || event.currentTarget) as Element
    const at = toFrame(event, el)
    if (at.x === last.x && at.y === last.y) return
    last = at
    pendingMove = at
    const wait = MOVE_INTERVAL_MS - (performance.now() - lastMoveSentAt)
    if (wait <= 0) {
      flushMove()
    } else if (moveTimer === undefined) {
      // 限流期间最后一个点留着，到点补发——拖滑块时终点不会丢
      moveTimer = window.setTimeout(flushMove, wait)
    }
  }

  function release(at: { x: number; y: number }) {
    if (!pressedButton) return
    flushMove()
    if (at.x !== last.x || at.y !== last.y) options.send({ type: 'mousemove', ...at })
    options.send({ type: 'mouseup', ...at, button: pressedButton, clickCount: lastDown.count })
    pressedButton = null
  }

  function onPointerUp(event: PointerEvent) {
    if (!pressedButton) return
    event.preventDefault()
    const el = (captureEl || event.currentTarget) as Element
    release(toFrame(event, el))
    try { el.releasePointerCapture(event.pointerId) } catch { /* 已经释放了 */ }
    captureEl = null
  }

  /** 捕获被浏览器收走（切窗口、弹系统对话框）：按着的键必须补一个松开，否则服务端一直按着 */
  function onPointerCancel() {
    release(last)
    captureEl = null
  }

  function onWheel(event: WheelEvent) {
    if (!enabled()) return
    event.preventDefault()
    const el = event.currentTarget as Element
    // deltaMode：0=像素 1=行 2=页，统一换成像素
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? options.frameSize().height : 1
    wheel.dx += event.deltaX * unit
    wheel.dy += event.deltaY * unit
    last = toFrame(event, el)
    if (wheelTimer !== undefined) return
    wheelTimer = window.setTimeout(() => {
      wheelTimer = undefined
      flushMove()
      options.send({ type: 'wheel', ...last, deltaX: Math.round(wheel.dx), deltaY: Math.round(wheel.dy) })
      wheel = { dx: 0, dy: 0 }
    }, WHEEL_INTERVAL_MS)
  }

  /**
   * 键盘：画面获得焦点后直接打字。
   * - 必须 preventDefault：不拦的话退格让本地浏览器后退、方向键和空格滚动本地页面
   * - Ctrl / ⌘ + V 不拦，交给下面的 onPaste 把本机剪贴板的文字发过去
   *   （服务端浏览器的剪贴板是空的，原样发组合键粘不出东西）
   * - 中文输入法组字时拿不到字（key 是 'Process'），走下面的输入框
   */
  function onKeyDown(event: KeyboardEvent) {
    if (!enabled()) return
    const key = event.key
    if (event.isComposing || key === 'Process' || key === 'Unidentified' || key === 'Dead') return
    if ((event.ctrlKey || event.metaKey) && key.toLowerCase() === 'v') return
    if (['Control', 'Shift', 'Alt', 'Meta', 'CapsLock'].includes(key)) return

    const combo: string[] = []
    if (event.ctrlKey) combo.push('Control')
    if (event.altKey) combo.push('Alt')
    if (event.metaKey) combo.push('Meta')
    // Shift 只在组合键 / 特殊键里带上：单按 Shift+a 时 key 已经是 'A'
    if (event.shiftKey && (combo.length || SPECIAL_KEYS.has(key))) combo.push('Shift')

    const name = key === ' ' ? 'Space' : key
    if (SPECIAL_KEYS.has(key) || combo.length) {
      event.preventDefault()
      options.send({ type: 'key', key: [...combo, name].join('+') })
    } else if (key.length === 1) {
      event.preventDefault()
      options.send({ type: 'type', text: key })
    }
  }

  function onPaste(event: ClipboardEvent) {
    if (!enabled()) return
    const text = event.clipboardData?.getData('text/plain') || ''
    if (!text) return
    event.preventDefault()
    options.send({ type: 'type', text })
  }

  /** 组件卸载 / 退出接管时调：清定时器，按着的键补松开 */
  function reset() {
    flushMove()
    window.clearTimeout(wheelTimer)
    wheelTimer = undefined
    wheel = { dx: 0, dy: 0 }
    if (pressedButton) onPointerCancel()
  }

  return { onPointerDown, onPointerMove, onPointerUp, onPointerCancel, onWheel, onKeyDown, onPaste, reset }
}
