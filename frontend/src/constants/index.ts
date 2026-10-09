export const CHANNEL_LABELS: Record<string, string> = {
  douyin: '抖音',
  kuaishou: '快手',
  xiaohongshu: '小红书',
  weibo: '微博',
  ctrip: '携程',
  tongcheng: '同程',
  qunar: '去哪儿',
}

export const CHANNEL_COLORS: Record<string, string> = {
  douyin: '#161823',
  kuaishou: '#ff6600',
  xiaohongshu: '#ff2442',
  weibo: '#e6162d',
  ctrip: '#2577e3',
  tongcheng: '#08a2f7',
  qunar: '#00afc9',
}

/** 携程/同程/去哪儿没有作品，只有景区点评 */
export const POI_CHANNELS = ['ctrip', 'tongcheng', 'qunar']
/** 需要登录的平台 */
export const LOGIN_CHANNELS = ['douyin', 'kuaishou', 'xiaohongshu', 'weibo']

/**
 * 写了拟人采集器的平台——只有这几个能选「采集模式」。
 * 和后端 collect_params.SUPPORTS_BROWSER_ENGINE 保持一致；
 * 其它平台给出这个选项只会让人以为设置没生效。
 */
export const BROWSER_ENGINE_CHANNELS = ['douyin', 'kuaishou', 'xiaohongshu']

/** 采集模式三选一。说明文案就是用户在页面上唯一能看到的解释，写清楚。 */
export const COLLECT_ENGINES = [
  { value: 'api', label: 'API', hint: '只走接口。最快，浏览器只在刷新登录态时开一下就关；接口被风控就直接报错，不换路。' },
  { value: 'hybrid', label: '混合', hint: '先走接口，接口被风控、拿不到数据时自动换成拟人继续采（推荐）。' },
  { value: 'human', label: '拟人', hint: '全程浏览器模拟真人：输入关键字、点筛选、点开作品、翻评论。最慢但最不容易被拦。' },
]

/** 库里可能还存着改名前的旧值，读出来先映射一下再显示 */
export const LEGACY_ENGINE_ALIASES: Record<string, string> = {
  auto: 'hybrid',
  browser: 'human',
}

export function normalizeEngine(value: unknown): string {
  const text = String(value || '').trim().toLowerCase()
  const mapped = LEGACY_ENGINE_ALIASES[text] || text
  return COLLECT_ENGINES.some((item) => item.value === mapped) ? mapped : 'hybrid'
}

export const TASK_STATUS_LABELS: Record<string, string> = {
  pending: '待执行',
  queued: '排队中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  canceled: '已取消',
  paused: '已暂停',
}

export const TASK_STATUS_TYPES: Record<string, '' | 'success' | 'warning' | 'info' | 'danger'> = {
  pending: 'info',
  queued: 'info',
  running: 'warning',
  completed: 'success',
  failed: 'danger',
  canceled: 'info',
  paused: 'info',
}

export const SCHEDULE_LABELS: Record<string, string> = {
  once: '立即执行一次',
  at: '定时执行一次',
  interval: '固定间隔重复',
  cron: 'cron 表达式',
}

export const ACCOUNT_STATUS_LABELS: Record<string, string> = {
  never_login: '未登录',
  active: '正常',
  expired: '已失效',
  disabled: '已停用',
}

export const ACCOUNT_STATUS_TYPES: Record<string, '' | 'success' | 'warning' | 'info' | 'danger'> = {
  never_login: 'info',
  active: 'success',
  expired: 'danger',
  disabled: 'info',
}

/** 常用 cron 预设，新建任务页直接点选 */
export const CRON_PRESETS = [
  { label: '每天凌晨 2 点', value: '0 2 * * *' },
  { label: '每天 8 点和 20 点', value: '0 8,20 * * *' },
  { label: '每 6 小时', value: '0 */6 * * *' },
  { label: '每周一凌晨 3 点', value: '0 3 * * 1' },
  { label: '每月 1 号凌晨 4 点', value: '0 4 1 * *' },
  { label: '工作日 9 点', value: '0 9 * * 1-5' },
]

export function channelLabel(channel: string): string {
  return CHANNEL_LABELS[channel] || channel
}

/** 把后端存的 JSON 字符串列表安全地解析成数组 */
export function parseJsonList(raw?: string | null): string[] {
  if (!raw) return []
  try {
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((x) => typeof x === 'string') : []
  } catch {
    return []
  }
}

export function parseJsonObject(raw?: string | null): Record<string, unknown> {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

/** 大数字缩写显示：12500 -> 1.2万 */
export function formatCount(value?: number | null): string {
  const num = Number(value || 0)
  if (num >= 100_000_000) return `${(num / 100_000_000).toFixed(1)}亿`
  if (num >= 10_000) return `${(num / 10_000).toFixed(1)}万`
  return String(num)
}

export function formatTime(value?: string | null): string {
  if (!value) return '-'
  return String(value).replace('T', ' ').slice(0, 19)
}

/**
 * 把平台图片地址换成走后端代理的地址。
 *
 * ⚠️ 各平台的图片 CDN（微博 sinaimg、抖音 douyinpic、小红书 xhscdn、快手 kwimgs）
 * 都做了防盗链：请求带着别的站点的 Referer 就回 403。
 * 这个现象特别具有迷惑性——把地址复制到浏览器能正常打开（那样不带 Referer），
 * 页面里却是一片"加载失败"，让人以为是采到的地址不对。
 *
 * 页面上的 `<meta name="referrer" content="no-referrer">` 能解决一部分，但不够：
 * 有的 CDN 要的不是"没有 Referer"而是"自家域名的 Referer"，
 * 而且浏览器扩展、企业策略都可能把页面的 referrer policy 覆盖掉。
 * 交给后端代取最稳——服务端想发什么头就发什么头。
 */
export function proxiedImage(url?: string | null): string {
  const raw = (url || '').trim()
  if (!raw) return ''
  // data: / blob: / 站内相对路径都不用代理
  if (!/^https?:\/\//i.test(raw)) return raw
  return `/api/media/image?url=${encodeURIComponent(raw)}`
}
