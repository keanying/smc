// CloudSense（云听）· Copyright (c) 2026 keanying
// SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
// 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
// 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
// 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

/**
 * 全站消息通知（替代 ElMessage）。效果照 HeroUI v3 的 Toast 做（Sonner 那种叠放卡片）：
 *
 * - 同一个位置的通知叠成一摞：最新的在最前，后面的每张往后缩 5%、露出 12px 边
 * - 鼠标移上去整摞展开，所有通知的倒计时暂停；移开收回、接着倒计时
 * - 最多露出 3 张，更早的淡出（倒计时照走）
 * - 标题 + 说明两行；成功 / 警告 / 出错 / 强调各有颜色和图标；可以带一个操作按钮
 * - promise：先显示「处理中」转圈，完成后原地变成成功或失败
 *
 * 用法（和 HeroUI 的 toast 一样）：
 *   toast.success('已保存')
 *   toast.danger('保存失败', { description: '数据库连不上' })
 *   toast('已复制', { action: { label: '撤销', onClick: undo } })
 *   toast.promise(api.save(), { loading: '保存中…', success: '已保存', error: (e) => e.message })
 *
 * 渲染在 components/ToastHost.vue（挂在 App.vue 里，跟着全站缩放走）。
 */
import { reactive } from 'vue'

export type ToastVariant = 'default' | 'accent' | 'success' | 'warning' | 'danger'
export type ToastPlacement = 'top start' | 'top' | 'top end' | 'bottom start' | 'bottom' | 'bottom end'

export interface ToastAction {
  label: string
  onClick: () => void
  /** 点了按钮后是否关掉这条通知（默认关） */
  closeOnClick?: boolean
}

export interface ToastOptions {
  description?: string
  variant?: ToastVariant
  /** 多少毫秒后自动消失；0 = 不自动消失。默认 4000，出错的默认 6000（要多看一会儿） */
  timeout?: number
  placement?: ToastPlacement
  action?: ToastAction
  /** 转圈（处理中） */
  isLoading?: boolean
  onClose?: () => void
}

export interface ToastItem {
  key: string
  title: string
  description: string
  variant: ToastVariant
  timeout: number
  placement: ToastPlacement
  action?: ToastAction
  isLoading: boolean
  exiting: boolean
  onClose?: () => void
  /** 内容被原地替换的次数（promise 结束时），用来给图标播一下切换动画 */
  revision: number
}

/** 默认位置：右下角——不挡顶栏，也不挡各页右上角的操作按钮。改这一处全站生效 */
export const DEFAULT_PLACEMENT: ToastPlacement = 'bottom end'
const DEFAULT_TIMEOUT = 4000
const DANGER_TIMEOUT = 6000
/** 关闭后留在页面上播退场动画的时间（比最长的退场过渡 250ms 多一点） */
export const EXIT_DURATION = 300
/** 每个位置最多存多少条（露出的只有 3 条，其余淡出但计时；太多就直接关掉最老的） */
const MAX_PER_PLACEMENT = 12

export const toastState = reactive({ items: [] as ToastItem[] })

// ------------------------------------------------------------------ 倒计时（可暂停）
interface Timer { remaining: number; startedAt: number; handle: number | null }
const timers = new Map<string, Timer>()
/** 被暂停的位置（鼠标停在那一摞上） + 整页不可见时全部暂停 */
const pausedPlacements = new Set<ToastPlacement>()
let pageHidden = typeof document !== 'undefined' && document.visibilityState === 'hidden'

function isPaused(item: ToastItem) {
  return pageHidden || pausedPlacements.has(item.placement)
}

function startTimer(item: ToastItem, delay = item.timeout) {
  clearTimer(item.key)
  if (!delay) return
  const timer: Timer = { remaining: delay, startedAt: 0, handle: null }
  timers.set(item.key, timer)
  if (!isPaused(item)) resumeTimer(item.key)
}

function resumeTimer(key: string) {
  const timer = timers.get(key)
  if (!timer || timer.handle != null || timer.remaining <= 0) return
  timer.startedAt = performance.now()
  timer.handle = window.setTimeout(() => {
    timer.handle = null
    timer.remaining = 0
    close(key)
  }, timer.remaining)
}

function pauseTimer(key: string) {
  const timer = timers.get(key)
  if (!timer || timer.handle == null) return
  window.clearTimeout(timer.handle)
  timer.handle = null
  timer.remaining -= performance.now() - timer.startedAt
}

function clearTimer(key: string) {
  const timer = timers.get(key)
  if (timer?.handle != null) window.clearTimeout(timer.handle)
  timers.delete(key)
}

/** 鼠标停在某一摞上：这一摞全部暂停 */
export function pausePlacement(placement: ToastPlacement) {
  pausedPlacements.add(placement)
  toastState.items.filter((t) => t.placement === placement).forEach((t) => pauseTimer(t.key))
}
export function resumePlacement(placement: ToastPlacement) {
  pausedPlacements.delete(placement)
  if (pageHidden) return
  toastState.items.filter((t) => t.placement === placement && !t.exiting).forEach((t) => resumeTimer(t.key))
}

// 切到别的标签页时不倒计时：回来还能看到刚才的报错
if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    pageHidden = document.visibilityState === 'hidden'
    toastState.items.forEach((t) => (isPaused(t) ? pauseTimer(t.key) : resumeTimer(t.key)))
  })
}

// ------------------------------------------------------------------ 增删改
let seq = 0

/**
 * 「保存失败：数据库连不上」这种一句话，拆成标题「保存失败」+ 说明「数据库连不上」，
 * 和 HeroUI 两行的样子一致。冒号前面太长就不拆（那不是标题）。
 */
function splitMessage(message: string, description?: string): [string, string] {
  if (description !== undefined) return [message, description]
  const at = message.indexOf('：')
  if (at > 0 && at <= 12 && message.length - at > 1) {
    return [message.slice(0, at), message.slice(at + 1).trim()]
  }
  return [message, '']
}

function add(message: string, options: ToastOptions = {}): string {
  const variant = options.variant ?? 'default'
  const placement = options.placement ?? DEFAULT_PLACEMENT
  const [title, description] = splitMessage(String(message ?? ''), options.description)

  // 一模一样的通知还挂着（比如连点了几次、同一个错误连着报）：不再叠一张，原地重新计时
  const same = toastState.items.find((t) => !t.exiting && !t.isLoading && t.placement === placement
    && t.variant === variant && t.title === title && t.description === description)
  if (same && !options.isLoading) {
    startTimer(same)
    return same.key
  }

  const item: ToastItem = {
    key: `toast-${++seq}`,
    title,
    description,
    variant,
    timeout: options.isLoading ? 0 : (options.timeout ?? (variant === 'danger' ? DANGER_TIMEOUT : DEFAULT_TIMEOUT)),
    placement,
    action: options.action,
    isLoading: !!options.isLoading,
    exiting: false,
    onClose: options.onClose,
    revision: 0,
  }
  toastState.items.push(item)
  startTimer(item)

  const same_place = toastState.items.filter((t) => t.placement === placement && !t.exiting)
  if (same_place.length > MAX_PER_PLACEMENT) close(same_place[0].key)
  return item.key
}

/** 关掉一条：先标记退场、播完动画再移除 */
export function close(key: string) {
  const item = toastState.items.find((t) => t.key === key)
  if (!item || item.exiting) return
  clearTimer(key)
  item.exiting = true
  item.onClose?.()
  window.setTimeout(() => {
    const at = toastState.items.findIndex((t) => t.key === key)
    if (at >= 0) toastState.items.splice(at, 1)
  }, EXIT_DURATION)
}

/** 原地改一条（promise 结束时用）。给了 timeout 或从「处理中」变成结果，就重新计时 */
function update(key: string, message: string, options: ToastOptions = {}): string {
  const item = toastState.items.find((t) => t.key === key && !t.exiting)
  if (!item) return add(message, options)
  const [title, description] = splitMessage(String(message ?? ''), options.description)
  const wasLoading = item.isLoading
  Object.assign(item, {
    title,
    description,
    variant: options.variant ?? item.variant,
    action: options.action ?? item.action,
    isLoading: !!options.isLoading,
    revision: item.revision + 1,
  })
  if (options.onClose) item.onClose = options.onClose
  if (options.timeout !== undefined || (wasLoading && !item.isLoading)) {
    item.timeout = options.timeout ?? (item.variant === 'danger' ? DANGER_TIMEOUT : DEFAULT_TIMEOUT)
    startTimer(item)
  }
  return key
}

function clearAll() {
  toastState.items.forEach((t) => close(t.key))
}

type Shortcut = (message: string, options?: Omit<ToastOptions, 'variant'>) => string

interface PromiseOptions<T> {
  loading: string
  success: string | ((data: T) => string)
  error: string | ((error: Error) => string)
  placement?: ToastPlacement
}

export interface ToastApi {
  (message: string, options?: ToastOptions): string
  success: Shortcut
  danger: Shortcut
  /** 同 danger（照顾 ElMessage.error 的习惯） */
  error: Shortcut
  warning: Shortcut
  info: Shortcut
  accent: Shortcut
  update: typeof update
  close: typeof close
  clear: typeof clearAll
  promise: <T>(promise: Promise<T> | (() => Promise<T>), options: PromiseOptions<T>) => string
}

const shortcut = (variant: ToastVariant): Shortcut => (message, options) => add(message, { ...options, variant })

export const toast: ToastApi = Object.assign(
  (message: string, options?: ToastOptions) => add(message, options),
  {
    success: shortcut('success'),
    danger: shortcut('danger'),
    error: shortcut('danger'),
    warning: shortcut('warning'),
    info: shortcut('default'),
    accent: shortcut('accent'),
    update,
    close,
    clear: clearAll,
    promise<T>(promise: Promise<T> | (() => Promise<T>), options: PromiseOptions<T>) {
      const key = add(options.loading, { isLoading: true, placement: options.placement })
      const running = typeof promise === 'function' ? promise() : promise
      running.then(
        (data) => update(key, typeof options.success === 'function' ? options.success(data) : options.success,
          { variant: 'success' }),
        (error: Error) => update(key, typeof options.error === 'function' ? options.error(error) : options.error,
          { variant: 'danger' }),
      )
      return key
    },
  },
)
