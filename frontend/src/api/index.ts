import { api, download, type Paged } from './http'
import type { LiveViewInfo } from './ws'

// ===================== 通用类型 =====================

export interface Scenic {
  id: number
  scenic_id: string
  scenic_name: string
  province?: string | null
  city?: string | null
  remark?: string | null
  enabled: number
  keyword_count?: number
  target_count?: number
  create_time?: string
  keywords?: Keyword[]
  targets?: Target[]
}

export interface Keyword {
  id: number
  scenic_id: string
  keyword: string
  enabled: number
  sort_order: number
}

/** 景区的附关键字 / 过滤关键字。kind: aux=附关键字（命中就留存）exclude=过滤关键字（命中就丢弃） */
export interface FilterWord {
  id: number
  scenic_id: string
  kind: 'aux' | 'exclude'
  word: string
  enabled: number
  sort_order: number
}

export interface Target {
  id: number
  scenic_id: string
  channel: string
  target_type: 'poi' | 'creator'
  target_id: string
  target_name?: string | null
  target_url?: string | null
  extra?: Record<string, unknown>
  enabled: number
}

export interface ChannelOption {
  value: string
  label: string
  need_login?: boolean
}

export interface Task {
  task_id: string
  task_name: string
  scenic_id?: string | null
  channels: string[]
  collect_type: string
  keywords: string[]
  targets: Record<string, unknown>[]
  params: Record<string, unknown>
  status: string
  progress: number
  schedule_type: string
  schedule_enabled: number
  schedule_at?: string | null
  schedule_interval_seconds?: number | null
  cron_expression?: string | null
  timezone: string
  next_run_time?: string | null
  last_run_time?: string | null
  runs_count: number
  stat_new_works: number
  stat_updated_works: number
  stat_new_comments: number
  stat_updated_comments: number
  error?: string | null
  create_time: string
  is_running?: boolean
  /** 后端算好的「每个平台最终生效的采集数量」，只读 */
  collect_limits?: CollectLimit[]
  /** 内容过滤的当前配置 + 一句人话说明（后端 _content_filter_summary） */
  content_filter?: {
    enabled: boolean
    mode: string
    extra_keywords: string
    description: string
  }
  /** 每个平台最终生效的搜索条件，只读 */
  search_filters?: TaskSearchFilter[]
}

export interface FilterOption { value: string; label: string }

export interface ChannelFilterOptions {
  channel: string
  sorts: FilterOption[]
  /** false = 该平台接口不支持排序（快手） */
  sort_supported: boolean
  time_supported: boolean
  /** true = 时间筛选由接口完成；false = 采回来本地过滤 */
  time_native: boolean
  publish_within: FilterOption[]
  note: string
}

export interface TaskSearchFilter {
  channel: string
  sort: string
  publish_within: string
  start_date: string
  end_date: string
  window: [string, string] | null
  native_time_filter: boolean
  description: string
}

export interface CollectLimit {
  channel: string
  /** false = 该平台只有评论（携程/同程） */
  has_works: boolean
  max_works: number
  max_comments: number
  /** 携程/同程的翻页上限，0 = 一直翻到没有数据 */
  max_pages: number
  max_comment_level: number
  enable_sub_comments: boolean
  collect_comments: boolean
  /** 采集模式：api / hybrid / human（老任务可能是 auto / browser） */
  collect_engine: string
  /** 这个平台有没有拟人采集器；false 时前端不该显示模式选择器 */
  supports_browser_engine: boolean
  /** 是否配了平台级覆盖 */
  customized: boolean
}

export interface TaskLog {
  id: number
  task_id: string
  log_time: string
  level: string
  channel?: string | null
  message: string
}

export interface Work {
  id: number
  scenic_id: string
  scenic_name: string
  channel: string
  work_id: string
  work_url: string
  author_id: string
  author_name: string
  title?: string | null
  description?: string | null
  label?: string | null
  image_list?: string | null
  video_list?: string | null
  likes: number
  collection_cnt: number
  comment_cnt: number
  shares: number
  location: string
  publish_time?: string | null
  crawl_time?: string | null
  source_keyword: string
  /** 后端在 SQL 里算好的"是不是携程/同程的合成作品"。
   *  列表接口不再返回 extra_content（几十 KB 的原始 JSON），只给这个布尔。 */
  is_synthetic?: boolean
}

export interface Comment {
  id: number
  scenic_id: string
  scenic_name: string
  channel: string
  work_id: string
  comment_level: string
  comment_parent_id: string
  comment_id: string
  commenter_id: string
  commenter_name: string
  content?: string | null
  likes: number
  location: string
  image_list?: string | null
  video_list?: string | null
  sub_comment_count: number
  publish_time?: string | null
  extra_content?: string | null
  children?: Comment[]
}

export interface CookieImportResult {
  count: number
  domains: string[]
  names: string[]
  format: string
  injected_to_profile: number
  profile_dir: string
  profile_warning?: string
}

export interface CookieInspect {
  count: number
  names: string[]
  cookie_updated_at?: string | null
  status: string
  /** Cookie 里有没有真正的登录凭据 */
  logged_in: boolean
  /** 缺失时列出「需要其中之一」的 key */
  missing: string[]
  expected_any_of: string[]
  home_url: string
}

export interface HarvestResult {
  ok: boolean
  message: string
  cookie_count: number
  logged_in: boolean
  missing: string[]
}

export interface ProbeReport {
  channel: string
  channel_label: string
  keyword: string
  account_name: string
  ok: boolean
  stage: string
  found: number
  sample?: {
    work_id: string
    title: string
    author: string
    publish_time: string
    url: string
  } | null
  error: string
  logs: { level: string; message: string }[]
  proxy: { enabled?: boolean; proxy_url?: string; fingerprint?: string }
  http: { url?: string; status?: number; length?: number; snippet?: string }
  elapsed_seconds: number
}

export interface Account {
  id: number
  channel: string
  account_name: string
  nickname?: string | null
  status: string
  login_type: string
  account_group?: string
  enabled: number
  cookie_updated_at?: string | null
  last_check_time?: string | null
  last_error?: string | null
  /** 配额冷却到期时间；null 或已过期 = 不在冷却 */
  cooldown_until?: string | null
  cooldown_reason?: string | null
  /** 轮换锁时长（小时）。0 = 跟随系统设置里的全局值 */
  rotate_lock_hours?: number
}

export interface AccountGroup {
  name: string
  total: number
  active: number
}

export interface OverviewRow {
  scenic_id: string
  scenic_name: string
  total_works: number
  total_comments: number
  channels: { channel: string; work_cnt: number; comment_cnt: number; last_crawl?: string }[]
}

// ===================== 景区 =====================

/** 下拉框用的精简景区 */
export interface ScenicOption {
  scenic_id: string
  scenic_name: string
}

export const scenicApi = {
  list: (params: { keyword?: string; enabled_only?: boolean; page?: number; page_size?: number } = {}) =>
    api.get<Paged<Scenic>>('/api/scenics', params),
  detail: (scenicId: string) => api.get<Scenic>(`/api/scenics/${scenicId}`),
  save: (payload: Partial<Scenic>) => api.post<null>('/api/scenics', payload),
  remove: (scenicId: string) => api.del<null>(`/api/scenics/${scenicId}`),
  importCsv: (file: File) =>
    api.upload<{ created: number; updated: number; errors: string[] }>('/api/scenics/import-csv', file),
  /**
   * 下拉框专用：只返回 scenic_id / scenic_name。
   *
   * 别再用 list({ page_size: 500 }) 去填下拉框——那个接口每一行都要
   * 附带"启用关键字数""采集目标数"，500 行就是几百次聚合，
   * 而下拉框只显示一个名字。
   */
  options: (enabledOnly = true) =>
    api.get<ScenicOption[]>('/api/scenics/options', { enabled_only: enabledOnly }),
  channels: () => api.get<ChannelOption[]>('/api/scenics/channels'),

  keywords: (scenicId: string, params: { enabled_only?: boolean; limit?: number } = {}) =>
    api.get<Keyword[]>(`/api/scenics/${scenicId}/keywords`, params),
  addKeywords: (scenicId: string, keywords: string[]) =>
    api.post<{ added: number; skipped: number }>(`/api/scenics/${scenicId}/keywords`, { keywords }),
  removeKeyword: (keywordId: number) => api.del<null>(`/api/scenics/keywords/${keywordId}`),
  toggleKeyword: (keywordId: number, enabled: boolean) =>
    api.put<null>(`/api/scenics/keywords/${keywordId}/enabled`, undefined, { enabled }),

  filterWords: (scenicId: string, params: { kind?: string; enabled_only?: boolean } = {}) =>
    api.get<FilterWord[]>(`/api/scenics/${scenicId}/filter-words`, params),
  addFilterWords: (scenicId: string, kind: 'aux' | 'exclude', words: string[]) =>
    api.post<{ added: number; skipped: number; dropped: number }>(
      `/api/scenics/${scenicId}/filter-words`, { kind, words }),
  removeFilterWord: (wordId: number) =>
    api.del<null>(`/api/scenics/filter-words/${wordId}`),
  toggleFilterWord: (wordId: number, enabled: boolean) =>
    api.put<null>(`/api/scenics/filter-words/${wordId}/enabled`, undefined, { enabled }),

  targets: (scenicId: string, params: { channel?: string; target_type?: string } = {}) =>
    api.get<Target[]>(`/api/scenics/${scenicId}/targets`, params),
  saveTarget: (scenicId: string, payload: Partial<Target>) =>
    api.post<null>(`/api/scenics/${scenicId}/targets`, payload),
  removeTarget: (targetId: number) => api.del<null>(`/api/scenics/targets/${targetId}`),
}

// ===================== AI 标注 =====================

/** 审核列表里的一条评论（含标注结果与人工复核状态） */
export interface LabelComment {
  id: number
  channel: string
  scenic_id: string
  scenic_name: string
  work_id: string
  comment_id: string
  commenter_name: string
  content: string
  likes: number
  publish_time: string | null
  comment_level: string
  sentiment_label: string | null
  sentiment_score: number | null
  dimension_tags: Array<{ dim1: string; dim2: string; dim3: string; sentiment: number }>
  entity_tags: Array<{ type: string; value: string }>
  keyword_tags: string[]
  /** 0未标注 1人工复核正确 2人工复核错误 3复核成功 4AI标注成功 5AI标注错误 6未人工复核 */
  label_review_flag: number
  label_review_label: string
  reviewed_by_human: boolean
  label_review_by: string
  label_review_time: string | null
  labeled: boolean
}

export interface LabelStats {
  total: number
  labeled: number
  unlabeled: number
  positive: number
  neutral: number
  negative: number
  /** 引擎写的三档 */
  ai_ok: number
  ai_failed: number
  ai_low_conf: number
  /** 人工写的三档 */
  human_right: number
  human_wrong: number
  human_fixed: number
  reviewed: number
  /** AI 标过但还没人复核的（4+5+6） */
  pending_review: number
}

export interface LabelCheckItem { name: string; ok: boolean; detail: string }

export interface BackfillJob {
  job_id: string
  scenic_id: string
  channel: string
  /** 开跑时符合条件的未标注总数 */
  total: number
  /** 累计喂进队列的条数（可能大于 done：喂进去不等于标完了） */
  submitted: number
  /** 已经标完写回库的条数——进度以它为准 */
  done: number
  /** 已经喂了几批 */
  batches: number
  /** 当前队列里还压着多少 */
  queued: number
  percent: number
  status: string
  error: string
  elapsed_seconds: number
  eta_seconds: number | null
  updated_at: number
  cancel_requested: boolean
}

export const labelingApi = {
  status: () => api.get<{
    enabled: boolean; running: boolean; degraded: boolean; error: string
    engine_present: boolean; channels: ChannelOption[]
    review_flags: Array<{ value: number; label: string }>
    human_flags: number[]
    stats?: Record<string, unknown>
  }>('/api/labeling/status'),
  /** 自检：模型配置、数据库联调、表结构。开边采边标之前先按它 */
  check: () => api.post<{ passed: boolean; items: LabelCheckItem[]; summary: string }>(
    '/api/labeling/check'),

  stats: (params: { scenic_id?: string; channel?: string } = {}) =>
    api.get<LabelStats>('/api/labeling/stats', params),
  comments: (params: Record<string, unknown> = {}) =>
    api.get<Paged<LabelComment>>('/api/labeling/comments', params),
  /** 人工确认（不传字段）或人工修改（传字段） */
  save: (id: number, payload: Record<string, unknown>) =>
    api.put<LabelComment>(`/api/labeling/comments/${id}`, payload),
  /** 撤销复核，回到「待复核」 */
  reset: (id: number) => api.post<LabelComment>(`/api/labeling/comments/${id}/reset`),
  /** AI 再标注这一条。同步等模型，前端要给 loading */
  relabel: (id: number) => api.post<{ result: Record<string, unknown>; comment: LabelComment }>(
    `/api/labeling/comments/${id}/relabel`),

  backfill: (payload: { scenic_id?: string; channel?: string; limit?: number }) =>
    api.post<BackfillJob>('/api/labeling/backfill', payload),
  backfillJobs: () => api.get<BackfillJob[]>('/api/labeling/backfill'),
  backfillJob: (jobId: string) =>
    api.get<BackfillJob>(`/api/labeling/backfill/${jobId}`, undefined, true),
  cancelBackfill: (jobId: string) =>
    api.post<null>(`/api/labeling/backfill/${jobId}/cancel`),
}

// ===================== 任务 =====================

export const taskApi = {
  list: (params: { status?: string; scenic_id?: string; keyword?: string; page?: number; page_size?: number } = {}) =>
    api.get<Paged<Task>>('/api/tasks', params),
  detail: (taskId: string) => api.get<Task>(`/api/tasks/${taskId}`),
  create: (payload: Record<string, unknown>) => api.post<Task>('/api/tasks', payload),
  update: (taskId: string, payload: Record<string, unknown>) => api.put<Task>(`/api/tasks/${taskId}`, payload),
  /** 局部修改：只传要改的字段，运行中的任务也能改 */
  patch: (taskId: string, payload: Record<string, unknown>) => api.patch<Task>(`/api/tasks/${taskId}`, payload),
  remove: (taskId: string) => api.del<null>(`/api/tasks/${taskId}`),
  run: (taskId: string) => api.post<{ started: boolean }>(`/api/tasks/${taskId}/run`),
  cancel: (taskId: string) => api.post<{ cancelled: boolean }>(`/api/tasks/${taskId}/cancel`),
  toggleSchedule: (taskId: string, enabled: boolean) =>
    api.put<null>(`/api/tasks/${taskId}/schedule-enabled`, undefined, { enabled }),
  logs: (taskId: string, afterId = 0, limit = 500) =>
    api.get<TaskLog[]>(`/api/tasks/${taskId}/logs`, { after_id: afterId, limit }),
  clearLogs: (taskId: string) => api.del<null>(`/api/tasks/${taskId}/logs`),
  /** 新建/编辑任务的筛选表单选项，由后端给出，前端不硬编码 */
  filterOptions: () => api.get<{
    channels: ChannelFilterOptions[]
    publish_within: FilterOption[]
  }>('/api/tasks/filter-options'),
  previewSchedule: (params: Record<string, unknown>) =>
    api.post<{ next_runs: string[] }>('/api/tasks/preview-schedule', undefined, params),
}

// ===================== 采集实时画面 =====================

export const liveApi = {
  /** 现在有哪些采集页面可以看。不传 taskId 就是全部 */
  views: (taskId = '') =>
    api.get<LiveViewInfo[]>('/api/live/views', taskId ? { task_id: taskId } : {}),
  /** 单帧地址。WebSocket 走不通时前端轮询它兜底；带时间戳绕开缓存 */
  frameUrl: (viewId: string) =>
    `/api/live/views/${encodeURIComponent(viewId)}/frame.jpg?t=${Date.now()}`,
}

// ===================== 数据中心 =====================

export const dataApi = {
  overview: () => api.get<OverviewRow[]>('/api/data/overview'),
  works: (params: Record<string, unknown>) => api.get<Paged<Work>>('/api/data/works', params),
  comments: (params: Record<string, unknown>) => api.get<Paged<Comment>>('/api/data/comments', params),
  thread: (channel: string, workId: string, rootCommentId: string) =>
    api.get<Comment[]>('/api/data/comments/thread', {
      channel, work_id: workId, root_comment_id: rootCommentId,
    }),
  authors: (params: Record<string, unknown>) => api.get<Paged<Record<string, unknown>>>('/api/data/authors', params),
  exportCsv: (kind: 'works' | 'comments', params: Record<string, unknown>) =>
    download(`/api/data/export/${kind}`, params),
}

// ===================== 账号 =====================

export interface AccountQuotaLimits {
  daily_works: number
  session_minutes: number
  cooldown_minutes: number
}

export interface AccountQuotaUsage {
  channel: string
  account_name: string
  stat_date: string
  works: number
  last_active_at: string | null
}

export interface AccountRotationInfo {
  enabled: boolean
  default_hours: number
  /** key 是 `渠道/账号名`，值是这把锁还剩多少秒 */
  locked: Record<string, number>
}

export interface AccountQuotaInfo {
  enabled: boolean
  limits: Record<string, AccountQuotaLimits>
  usage: AccountQuotaUsage[]
  rotation?: AccountRotationInfo
}

export const accountApi = {
  channels: () => api.get<ChannelOption[]>('/api/accounts/channels'),
  groups: () => api.get<AccountGroup[]>('/api/accounts/groups'),
  list: (params: { channel?: string; status?: string; group?: string } = {}) =>
    api.get<Paged<Account>>('/api/accounts', params),
  create: (payload: Record<string, unknown>) => api.post<null>('/api/accounts', payload),
  remove: (channel: string, accountName: string) =>
    api.del<null>(`/api/accounts/${channel}/${encodeURIComponent(accountName)}`),
  toggle: (channel: string, accountName: string, enabled: boolean) =>
    api.put<null>(`/api/accounts/${channel}/${encodeURIComponent(accountName)}/enabled`, undefined, { enabled }),
  /** 试搜一个关键字，返回完整诊断报告 */
  probe: (channel: string, accountName: string, keyword: string, targetId = '') =>
    api.post<ProbeReport>(
      `/api/accounts/${channel}/${encodeURIComponent(accountName || '-')}/probe`,
      undefined, { keyword, target_id: targetId },
    ),
  /** 立即从浏览器 profile 抓一次 Cookie 存库 */
  harvest: (channel: string, accountName: string) =>
    api.post<HarvestResult>(
      `/api/accounts/${channel}/${encodeURIComponent(accountName)}/harvest`,
    ),
  /** 手动导入 Cookie（Cookie-Editor 的 JSON 或 name=value; … 串） */
  importCookies: (channel: string, accountName: string, raw: string) =>
    api.post<CookieImportResult>(
      `/api/accounts/${channel}/${encodeURIComponent(accountName)}/cookies`, { raw },
    ),
  /** 查看该账号当前存了哪些 Cookie（只回 key） */
  inspectCookies: (channel: string, accountName: string) =>
    api.get<CookieInspect>(
      `/api/accounts/${channel}/${encodeURIComponent(accountName)}/cookies`,
    ),
  check: (channel: string, accountName: string) =>
    api.post<{ ok: boolean; message: string; cookie_count: number }>(
      `/api/accounts/${channel}/${encodeURIComponent(accountName)}/check`,
    ),
  logout: (channel: string, accountName: string) =>
    api.post<null>(`/api/accounts/${channel}/${encodeURIComponent(accountName)}/logout`),
  createLoginSession: (channel: string, accountName: string) =>
    api.post<{ session_id: string; ws_path: string }>(
      `/api/accounts/${channel}/${encodeURIComponent(accountName)}/login-session`,
    ),
  closeLoginSession: (sessionId: string) =>
    api.del<{ logged_in: boolean }>(`/api/accounts/login-session/${sessionId}`),
  /** 用户自己判断"我登录好了"，直接把浏览器里的 Cookie 存下来 */
  saveLoginSession: (sessionId: string) =>
    api.post<{ saved: boolean; cookie_count: number; message: string }>(
      `/api/accounts/login-session/${sessionId}/save`,
    ),
  /** 全平台体检：六个平台挨个试搜一遍 */
  /** 账号今日用量 + 各渠道生效的配额上限 */
  quota: (params: { channel?: string; account_name?: string; days?: number } = {}) =>
    api.get<AccountQuotaInfo>('/api/accounts/quota', params),
  /** 人工解除冷却 */
  clearCooldown: (channel: string, accountName: string) =>
    api.post<null>(
      `/api/accounts/${channel}/${encodeURIComponent(accountName)}/clear-cooldown`),
  /** 人工解除轮换锁，让这个号下一轮就能被挑到 */
  unlockRotation: (channel: string, accountName: string) =>
    api.post<null>(
      `/api/accounts/${channel}/${encodeURIComponent(accountName)}/unlock-rotation`),

  probeAll: (keyword: string, ctripTarget = '', tongchengTarget = '') =>
    api.post<ProbeReport[]>('/api/accounts/probe-all', undefined, {
      keyword, ctrip_target: ctripTarget, tongcheng_target: tongchengTarget,
    }),
}

// ===================== 系统设置 =====================

export const settingsApi = {
  get: () => api.get<{
    config: Record<string, any>
    editable_sections: string[]
    config_file: string
    channels: ChannelOption[]
  }>('/api/settings'),
  save: (section: string, values: Record<string, unknown>) =>
    api.put<Record<string, unknown>>('/api/settings', { section, values }),
  proxyStatus: () => api.get<Record<string, any>>('/api/settings/proxy/status'),
  testProxy: (payload: Record<string, unknown>) =>
    api.post<{ ok: boolean; proxy?: string; ttl_seconds?: number; message?: string }>(
      '/api/settings/proxy/test', payload,
    ),
  redisStatus: () => api.get<Record<string, any>>('/api/settings/redis/status'),
  schedulerStatus: () => api.get<Record<string, any>>('/api/settings/scheduler/status'),
  /** 发一条测试通知。preview 是渲染出来的正文，发不出去时也返回——
   *  至少能确认格式和远程账号密码填对了。 */
  testNotify: () => api.post<{ sent: boolean; preview: string; error: string }>(
    '/api/settings/notify/test'),
  health: () => api.get<Record<string, any>>('/api/health'),
}

export { download }
export type { Paged }

// ---------------- 景区档案（同程 / 携程 / 去哪儿） ----------------
export interface ArchiveChannel {
  channel: string
  label: string
  /** 平台有没有景区详情页。false 的渠道（同程）详情几列永远是空的 */
  supports_detail: boolean
}

export interface ArchiveRegion {
  region_id: string
  region_name: string
  level: string
  poi_count: number
  last_sync_time: string | null
}

export interface ArchiveItem {
  id: number
  channel: string
  poi_id: string
  scenic_id: string
  poi_name: string
  province: string
  city_id: string
  city_name: string
  address: string
  scenic_level: string
  tel: string
  open_time: string
  source_url: string
  detail_status: string
  detail_error: string
  detail_time: string | null
  crawl_time: string | null
  region_id: string
  /** 只有单条详情接口才返回下面这几个大字段 */
  scenic_intro?: string
  discount_policy?: string
  amenity?: string
}

export interface ArchiveJob {
  job_id: string
  channel: string
  region_names: string[]
  with_detail: boolean
  regions_total: number
  regions_done: number
  collected: number
  created: number
  updated: number
  detail_done: number
  detail_failed: number
  current: string
  message: string
  status: 'running' | 'finished' | 'failed' | 'canceled'
  error: string
  percent: number
  elapsed_seconds: number
}

export interface ArchiveStat {
  channel: string
  total: number
  linked: number
  detailed: number
  last_crawl: string | null
}

export const archiveApi = {
  channels: () => api.get<ArchiveChannel[]>('/api/archive/channels'),
  regions: (channel: string) =>
    api.get<ArchiveRegion[]>('/api/archive/regions', { channel }),
  refreshRegions: (channel: string) =>
    api.post<{ total: number; saved: number }>('/api/archive/regions/refresh', { channel }),
  addRegion: (channel: string, region_id: string, region_name: string, level = 'province') =>
    api.post<null>('/api/archive/regions/add', { channel, region_id, region_name, level }),
  removeRegion: (channel: string, region_id: string) =>
    api.del<null>('/api/archive/regions', { channel, region_id }),

  collect: (payload: {
    channel: string; region_ids: string[]; with_detail?: boolean; limit?: number
  }) => api.post<ArchiveJob>('/api/archive/collect', payload),
  jobs: () => api.get<ArchiveJob[]>('/api/archive/jobs'),
  job: (jobId: string) => api.get<ArchiveJob>(`/api/archive/jobs/${jobId}`),
  cancelJob: (jobId: string) => api.post<null>(`/api/archive/jobs/${jobId}/cancel`, {}),

  list: (params: {
    channel?: string; province?: string; city?: string; keyword?: string
    linked?: string; level?: string; scenic_id?: string
    page?: number; page_size?: number
  }) => api.get<Paged<ArchiveItem>>('/api/archive/list', params),
  provinces: (channel = '') =>
    api.get<{ province: string; total: number }[]>('/api/archive/provinces', { channel }),
  stats: () => api.get<ArchiveStat[]>('/api/archive/stats'),
  detail: (channel: string, poiId: string) =>
    api.get<ArchiveItem | null>(`/api/archive/${channel}/${poiId}`),
  refreshOne: (channel: string, poiId: string) =>
    api.post<ArchiveItem | null>(`/api/archive/${channel}/${poiId}/refresh`, {}),

  importPois: (channel: string, poi_ids: string[], scenic_prefix = '') =>
    api.post<{ created: number; updated: number; failed: unknown[] }>(
      '/api/archive/import', { channel, poi_ids, scenic_prefix }),
  attach: (channel: string, poi_id: string, scenic_id: string) =>
    api.post<null>('/api/archive/attach', { channel, poi_id, scenic_id }),
}

export interface QunarProvince {
  province: string
  cities: { city_id: string; city_name: string }[]
}

/** 去哪儿城市索引里的一项（vendor/qunar_crawler/models.py 的 City） */
export interface QunarCity {
  city_id: string
  city_name: string
  city_url: string
}

/** 城市景区列表里的一项（vendor/qunar_crawler/models.py 的 PoiSummary） */
export interface QunarPoi {
  poi_id: string
  poi_name: string
  address: string
  scenic_level: string
}

export const qunarApi = {
  /**
   * 去哪儿的行政区划同步。
   *
   * 只剩这两个接口了：浏览/导入景区已经统一到「景区档案」板块，
   * 但行政区划是去哪儿**独有**的一步——它的城市索引是一张扁平名单，
   * 没有省份归属，不同步这一次，采下来的档案省份列就是空的。
   */
  provinces: () => api.get<QunarProvince[]>('/api/qunar/provinces'),
  syncRegions: () => api.post<{ total: number; matched: number; saved: number }>(
    '/api/qunar/regions/sync', {}),

  // 下面四个给 QunarImportDialog 用（按城市浏览去哪儿景区、勾选导入），
  // 对应后端 app/api/qunar.py 里仍在的 /cities /pois /saved /import
  cities: () => api.get<QunarCity[]>('/api/qunar/cities'),
  pois: (city: string, page = 1) =>
    api.get<{ city_id: string; city_name: string; page: number; total_pages: number;
      items: QunarPoi[] }>('/api/qunar/pois', { city, page }),
  saved: (params: { city?: string; scenic_id?: string; keyword?: string;
    page?: number; page_size?: number } = {}) =>
    api.get<{ total: number; page: number; page_size: number;
      items: { poi_id: string; poi_name?: string; scenic_id?: string }[] }>(
      '/api/qunar/saved', params),
  importPois: (payload: { items: QunarPoi[]; city_id: string; city_name: string;
    with_detail?: boolean; scenic_prefix?: string }) =>
    api.post<{ created: number; updated: number; failed: unknown[] }>(
      '/api/qunar/import', payload),
}
