<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!-- 景区版概览：只看本盒子分到的景区、本机的采集账号和任务 -->
  <div v-loading="loading" class="box-home">
    <div class="head">
      <div>
        <h1 class="head-title">概览</h1>
        <p class="head-sub">
          <span class="sync-dot" :class="syncClass" />{{ syncText }}
          <template v-if="assigned.length"> · 分到 {{ assigned.length }} 个景区</template>
        </p>
      </div>
      <div class="head-actions">
        <el-button v-if="isAdmin" :icon="Refresh" round :loading="syncing" @click="syncNow">立即同步</el-button>
        <el-button type="primary" :icon="Plus" round :disabled="!assigned.length" @click="go('/tasks/new')">
          新建任务
        </el-button>
      </div>
    </div>

    <!-- 还没分到景区：没配主 / 连不上主 / 主还没给这台盒子分 -->
    <div v-if="!loading && !assigned.length" class="empty-card">
      <div class="empty-icon"><el-icon><LocationFilled /></el-icon></div>
      <template v-if="!sync?.master_configured">
        <h3>先把这台盒子连到主</h3>
        <p>
          景区由主统一分配，采到的数据也直接报给主存储和标注。请主的管理员在「盒子管理」里登记这台盒子，
          把给的 <b>master_url</b> 和 <b>box_token</b> 写进盒子的 config/config.yaml（edition 段），重启盒子。
        </p>
      </template>
      <template v-else-if="sync?.ok === false">
        <h3>连不上主</h3>
        <p>{{ sync.last_error }}</p>
        <el-button v-if="isAdmin" type="primary" round :icon="Refresh" :loading="syncing" @click="syncNow">再试一次</el-button>
      </template>
      <template v-else>
        <h3>主还没给这台盒子分景区</h3>
        <p>请主的管理员在「盒子管理」里给这台盒子勾选景区，盒子一分钟内自动同步过来。</p>
        <el-button v-if="isAdmin" type="primary" round :icon="Refresh" :loading="syncing" @click="syncNow">立即同步</el-button>
      </template>
    </div>

    <div v-else class="grid">
      <!-- ============ 左 ============ -->
      <div class="col-main">
        <!-- 采集趋势：淡蓝大卡 -->
        <div class="trend-card">
          <div class="trend-head">
            <div>
              <div class="card-label">近 {{ days }} 天采到的评论</div>
              <div class="trend-number">{{ formatCount(trendComments) }}</div>
              <div class="trend-meta">
                作品 {{ formatCount(trendWorks) }}
                <span class="dot-sep" />
                今天 {{ formatCount(todayComments) }} 条评论
              </div>
            </div>
            <div class="seg" role="tablist" aria-label="时间范围">
              <button v-for="d in [7, 14, 30]" :key="d" type="button" class="seg-btn"
                      :class="{ 'is-on': days === d }" @click="setDays(d)">{{ d }} 天</button>
            </div>
          </div>
          <div class="chart-wrap" @mouseleave="focusIndex = defaultFocus">
            <svg class="chart" :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" aria-hidden="true">
              <defs>
                <linearGradient id="boxTrendFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stop-color="#1d1f26" stop-opacity="0.16" />
                  <stop offset="100%" stop-color="#1d1f26" stop-opacity="0" />
                </linearGradient>
              </defs>
              <path v-if="chart.area" :d="chart.area" fill="url(#boxTrendFill)" />
              <path v-if="chart.line" :d="chart.line" fill="none" stroke="#1d1f26" stroke-width="2.2"
                    vector-effect="non-scaling-stroke" />
            </svg>
            <!-- 悬停热区：每天一条 -->
            <div class="hover-cols">
              <span v-for="(p, i) in trend" :key="p.date" @mouseenter="focusIndex = i" />
            </div>
            <template v-if="chart.focus">
              <span class="focus-line" :style="{ left: `${chart.focus.px}%` }" />
              <span class="focus-dot" :style="{ left: `${chart.focus.px}%`, top: `${chart.focus.py}%` }" />
              <span class="focus-tip" :class="{ 'is-left': chart.focus.px > 75 }"
                    :style="{ left: `${chart.focus.px}%`, top: `${chart.focus.py}%` }">
                <b>{{ formatCount(chart.focus.value) }}</b> 条 · {{ chart.focus.label }}
              </span>
            </template>
          </div>
          <div class="axis">
            <span v-for="(p, i) in trend" :key="p.date">{{ showAxisLabel(i) ? shortDate(p.date) : '' }}</span>
          </div>
        </div>

        <!-- 分到的景区：马卡龙色卡片 -->
        <div class="section-head">
          <h3>我的景区</h3>
          <span class="link" role="button" tabindex="0" @click="go('/data')" @keyup.enter="go('/data')">
            数据中心 <el-icon><ArrowRight /></el-icon>
          </span>
        </div>
        <div class="scenic-grid">
          <div v-for="(s, i) in scenicCards" :key="s.scenic_id" class="scenic-card"
               :class="`tone-${i % 3}`" role="button" tabindex="0"
               @click="go({ path: '/data', query: { scenic_id: s.scenic_id } })"
               @keyup.enter="go({ path: '/data', query: { scenic_id: s.scenic_id } })">
            <div class="scenic-top">
              <span class="scenic-icon"><el-icon><LocationFilled /></el-icon></span>
              <span class="scenic-when">{{ s.lastCrawl }}</span>
            </div>
            <div class="scenic-name" :title="s.scenic_name">{{ s.scenic_name }}</div>
            <div class="scenic-num">{{ formatCount(s.comments) }}<small> 条评论</small></div>
            <div class="scenic-sub">作品 {{ formatCount(s.works) }} · {{ s.channelCount }} 个平台</div>
          </div>
        </div>

        <!-- 最近的任务 -->
        <div class="section-head">
          <h3>最近的任务</h3>
          <span class="link" role="button" tabindex="0" @click="go('/tasks')" @keyup.enter="go('/tasks')">
            全部任务 <el-icon><ArrowRight /></el-icon>
          </span>
        </div>
        <div class="task-table">
          <div class="tr th">
            <span>任务</span><span>景区</span><span>平台</span><span>状态</span><span class="num">新增评论</span>
          </div>
          <div v-for="t in recentTasks" :key="t.task_id" class="tr" role="button" tabindex="0"
               @click="go(`/tasks/${t.task_id}`)" @keyup.enter="go(`/tasks/${t.task_id}`)">
            <span class="task-name" :title="t.task_name">{{ t.task_name }}</span>
            <span class="muted ellipsis">{{ scenicName(t.scenic_id) }}</span>
            <span class="muted ellipsis">{{ t.channels.map(channelLabel).join('、') }}</span>
            <span><span class="status" :class="`st-${TASK_STATUS_TYPES[t.status] || 'info'}`">
              {{ TASK_STATUS_LABELS[t.status] || t.status }}</span></span>
            <span class="num">{{ formatCount(t.stat_new_comments) }}</span>
          </div>
          <div v-if="!recentTasks.length" class="tr-empty">
            还没有任务。<span class="link" role="button" tabindex="0" @click="go('/tasks/new')"
                          @keyup.enter="go('/tasks/new')">建一个</span>
          </div>
        </div>
      </div>

      <!-- ============ 右 ============ -->
      <div class="col-side">
        <!-- 深色卡：采集账号（盒子自己维护） -->
        <div class="dark-card">
          <div class="card-label light">本机采集账号</div>
          <div class="dark-number">{{ usableAccounts }}<small> / {{ accounts.length }} 可用</small></div>
          <ul class="channel-list">
            <li v-for="c in loginChannels" :key="c.value">
              <span>{{ c.label }}</span>
              <span :class="c.count ? 'ok' : 'miss'">{{ c.count ? `${c.count} 个` : '还没有' }}</span>
            </li>
          </ul>
          <p class="dark-note">采集在这台盒子上跑，要登录的平台需要先在这里加号、扫码登录。</p>
          <button type="button" class="cream-btn" @click="go('/accounts')">
            <el-icon><Plus /></el-icon> 添加采集账号
          </button>
        </div>

        <!-- 标注复核 -->
        <div class="side-card" role="button" tabindex="0" @click="go('/labeling')" @keyup.enter="go('/labeling')">
          <div class="side-head">
            <span class="card-label">标注审核</span>
            <el-icon class="side-arrow"><ArrowRight /></el-icon>
          </div>
          <template v-if="labelStats && labelStats.total">
            <div class="side-number">{{ formatCount(labelStats.labeled) }}<small> / {{ formatCount(labelStats.total) }} 已标注</small></div>
            <div class="senti-bar" aria-hidden="true">
              <span class="pos" :style="{ flex: labelStats.positive || 0 }" />
              <span class="neu" :style="{ flex: labelStats.neutral || 0 }" />
              <span class="neg" :style="{ flex: labelStats.negative || 0 }" />
            </div>
            <div class="senti-legend">
              <span><i class="pos" />正面 {{ formatCount(labelStats.positive) }}</span>
              <span><i class="neu" />中性 {{ formatCount(labelStats.neutral) }}</span>
              <span><i class="neg" />负面 {{ formatCount(labelStats.negative) }}</span>
            </div>
          </template>
          <p v-else class="muted side-empty">还没有标注结果。采集到评论后在这里看、改 AI 标注。</p>
        </div>

        <!-- 和主同步 -->
        <div class="side-card mint">
          <div class="card-label">数据存在主上</div>
          <div class="assign-row"><span>和主同步</span><b>{{ sync?.ok === false ? '连不上' : sync?.ok ? '正常' : '等待中' }}</b></div>
          <div v-if="sync?.last_ok_at" class="assign-row"><span>上次同步</span><b>{{ sync.last_ok_at.slice(5, 16) }}</b></div>
          <div class="assign-row"><span>分到的景区</span><b>{{ assigned.length }} 个</b></div>
          <p class="muted side-empty">采到的数据直接报给主，标注由主来做；这里看到和改的都是主上的数据。景区、关键字、采集目标由主维护。</p>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter, type RouteLocationRaw } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowRight, LocationFilled, Plus, Refresh } from '@element-plus/icons-vue'
import {
  accountApi, dataApi, editionApi, labelingApi, taskApi,
  type Account, type ChannelOption, type EditionAssignment, type LabelStats, type OverviewRow,
  type Task, type TrendPoint,
} from '../api'
import { TASK_STATUS_LABELS, TASK_STATUS_TYPES, channelLabel, formatCount } from '../constants'
import { isAdmin } from '../composables/auth'

const router = useRouter()
function go(to: RouteLocationRaw) {
  router.push(to)
}

const loading = ref(false)
const assignment = ref<EditionAssignment | null>(null)
const overview = ref<OverviewRow[]>([])
const recentTasks = ref<Task[]>([])
const accounts = ref<Account[]>([])
const channels = ref<ChannelOption[]>([])
const labelStats = ref<LabelStats | null>(null)
const trend = ref<TrendPoint[]>([])
const days = ref(14)
const focusIndex = ref(-1)
const defaultFocus = ref(-1)

const assigned = computed(() => assignment.value?.scenics || [])

function scenicName(id?: string | null): string {
  if (!id) return '—'
  return assigned.value.find((s) => s.scenic_id === id)?.scenic_name || id
}

// ---------------- 景区卡片 ----------------
const scenicCards = computed(() => assigned.value.map((s) => {
  const row = overview.value.find((r) => r.scenic_id === s.scenic_id)
  const times = (row?.channels || []).map((c) => c.last_crawl).filter(Boolean) as string[]
  return {
    ...s,
    works: row?.total_works || 0,
    comments: row?.total_comments || 0,
    channelCount: (row?.channels || []).filter((c) => c.work_cnt || c.comment_cnt).length,
    lastCrawl: times.length ? `${relTime(times.sort().pop() as string)}采过` : '还没采过',
  }
}))

// ---------------- 账号 ----------------
const usableAccounts = computed(() => accounts.value.filter((a) => a.enabled && a.status === 'active').length)
const loginChannels = computed(() => channels.value.filter((c) => c.need_login).map((c) => ({
  ...c,
  count: accounts.value.filter((a) => a.channel === c.value && a.enabled && a.status === 'active').length,
})))

// ---------------- 趋势图 ----------------
const W = 600
const H = 160
const trendComments = computed(() => trend.value.reduce((s, p) => s + p.comments, 0))
const trendWorks = computed(() => trend.value.reduce((s, p) => s + p.works, 0))
const todayComments = computed(() => trend.value[trend.value.length - 1]?.comments || 0)

/** 平滑曲线：Catmull-Rom 转三次贝塞尔（和总部版概览同一个画法） */
function smooth(points: [number, number][]): string {
  if (!points.length) return ''
  let d = `M${points[0][0]},${points[0][1]}`
  for (let i = 0; i < points.length - 1; i += 1) {
    const p0 = points[i - 1] || points[i]
    const p1 = points[i]
    const p2 = points[i + 1]
    const p3 = points[i + 2] || p2
    d += ` C${p1[0] + (p2[0] - p0[0]) / 6},${p1[1] + (p2[1] - p0[1]) / 6}`
      + ` ${p2[0] - (p3[0] - p1[0]) / 6},${p2[1] - (p3[1] - p1[1]) / 6} ${p2[0]},${p2[1]}`
  }
  return d
}

const chart = computed(() => {
  const values = trend.value.map((p) => p.comments)
  if (!values.length) return { line: '', area: '', focus: null as null | Record<string, any> }
  const max = Math.max(1, ...values)
  const step = values.length > 1 ? W / (values.length - 1) : 0
  const pts = values.map((v, i) => [i * step, 24 + (H - 34) * (1 - v / max)] as [number, number])
  const line = smooth(pts)
  const idx = focusIndex.value >= 0 && focusIndex.value < pts.length ? focusIndex.value : pts.length - 1
  const [x, y] = pts[idx]
  return {
    line,
    area: `${line} L${W},${H} L0,${H} Z`,
    focus: { px: (x / W) * 100, py: (y / H) * 100, value: values[idx], label: shortDate(trend.value[idx].date) },
  }
})

function shortDate(date: string): string {
  const [, m, d] = date.split('-')
  return `${Number(m)}/${Number(d)}`
}

function showAxisLabel(i: number): boolean {
  const n = trend.value.length
  const every = n > 20 ? 5 : n > 10 ? 2 : 1
  return i % every === 0 || i === n - 1
}

function relTime(value: string): string {
  const t = new Date(String(value).replace(' ', 'T')).getTime()
  if (Number.isNaN(t)) return ''
  const diff = Math.max(0, (Date.now() - t) / 1000)
  if (diff < 3600) return `${Math.max(1, Math.floor(diff / 60))} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`
  return String(value).slice(0, 10)
}

async function loadTrend() {
  trend.value = await dataApi.trend(days.value).catch(() => [])
  // 默认停在采得最多的那天：今天常常还没开始采，停在 0 上没什么可看
  const values = trend.value.map((p) => p.comments)
  const max = Math.max(0, ...values)
  defaultFocus.value = max > 0 ? values.lastIndexOf(max) : values.length - 1
  focusIndex.value = defaultFocus.value
}

function setDays(d: number) {
  if (days.value === d) return
  days.value = d
  loadTrend()
}

// ---------------- 和主同步 ----------------
const sync = computed(() => assignment.value?.sync)
const syncing = ref(false)
const syncClass = computed(() => (sync.value?.ok ? 'ok' : sync.value?.ok === false ? 'bad' : 'idle'))
const syncText = computed(() => {
  const st = sync.value
  if (!st?.master_configured) return '还没连到主'
  if (st.ok === false) return '连不上主'
  if (st.ok) return `已连接主 · ${st.last_ok_at.slice(11, 16)} 同步`
  return '正在连主…'
})

async function syncNow() {
  syncing.value = true
  try {
    await editionApi.sync()
    ElMessage.success('已和主同步')
    await load()
  } finally {
    syncing.value = false
  }
}

// ---------------- 加载 ----------------
async function load() {
  loading.value = true
  try {
    const [assign, overviewRows, taskPage, accountPage, channelList, stats] = await Promise.all([
      editionApi.assignment(),
      dataApi.overview(),
      taskApi.list({ page_size: 6, sort: 'recent' }),
      accountApi.list({}).catch(() => ({ items: [] as Account[], total: 0, page: 1, page_size: 0 })),
      accountApi.channels(),
      labelingApi.stats().catch(() => null),
      loadTrend(),
    ])
    assignment.value = assign
    overview.value = overviewRows
    recentTasks.value = taskPage.items
    accounts.value = accountPage.items
    channels.value = channelList
    labelStats.value = stats
  } finally {
    loading.value = false
  }
}

// 最近的任务每 30 秒自己刷新：定时任务跑起来、跑完不用手动刷新页面
async function refreshTasks() {
  if (document.hidden) return
  try {
    recentTasks.value = (await taskApi.list({ page_size: 6, sort: 'recent' }, true)).items
  } catch {
    /* 下一轮再试 */
  }
}

let taskTimer: number | undefined
onMounted(() => {
  load()
  taskTimer = window.setInterval(refreshTasks, 30_000)
})
onUnmounted(() => window.clearInterval(taskTimer))
</script>

<style scoped>
.box-home { min-height: 400px; }

.head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin: 4px 0 22px;
}
.head-title { margin: 0; font-size: 30px; font-weight: 700; letter-spacing: 0.3px; color: var(--box-ink); }
.head-sub { margin: 4px 0 0; font-size: 13px; color: var(--smc-text-secondary); display: flex; align-items: center; gap: 6px; }
.sync-dot { width: 7px; height: 7px; border-radius: 50%; background: #b4b8c0; }
.sync-dot.ok { background: #22c55e; }
.sync-dot.bad { background: #ef4444; }
.head-actions { display: flex; gap: 8px; }

.grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: 22px;
  align-items: start;
}
.col-main, .col-side { display: flex; flex-direction: column; gap: 18px; min-width: 0; }

.card-label { font-size: 13px; color: var(--smc-text-secondary); font-weight: 500; }
.card-label.light { color: rgba(255, 255, 255, 0.6); }

/* ---------------- 淡蓝趋势卡 ---------------- */
.trend-card {
  background: var(--box-sky);
  border-radius: 26px;
  padding: 22px 24px 14px;
}
.trend-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.trend-number { font-size: 34px; font-weight: 700; color: var(--box-ink); line-height: 1.2; margin-top: 4px; }
.trend-meta { font-size: 13px; color: #5a6478; display: flex; align-items: center; gap: 8px; }
.dot-sep { width: 3px; height: 3px; border-radius: 50%; background: currentColor; opacity: 0.6; }
.seg { display: inline-flex; padding: 3px; border-radius: 999px; background: rgba(255, 255, 255, 0.65); }
.seg-btn {
  border: none; background: transparent; cursor: pointer; font-size: 12px; color: #5a6478;
  padding: 5px 12px; border-radius: 999px; white-space: nowrap;
}
.seg-btn.is-on { background: var(--box-ink); color: #fff; }
.chart-wrap { position: relative; height: 160px; margin-top: 8px; }
.chart { width: 100%; height: 100%; display: block; overflow: visible; }
.hover-cols { position: absolute; inset: 0; display: flex; }
.hover-cols span { flex: 1; }
.focus-line {
  position: absolute; top: 0; bottom: 0; width: 1px; background: rgba(29, 31, 38, 0.18);
  transform: translateX(-0.5px); pointer-events: none;
}
.focus-dot {
  position: absolute; width: 12px; height: 12px; border-radius: 50%; background: #fff;
  border: 3px solid var(--box-ink); transform: translate(-50%, -50%); pointer-events: none;
}
.focus-tip {
  position: absolute; transform: translate(10px, -130%); white-space: nowrap; pointer-events: none;
  background: var(--box-ink); color: #fff; font-size: 12px; padding: 5px 10px; border-radius: 10px;
}
.focus-tip.is-left { transform: translate(calc(-100% - 10px), -130%); }
.axis { display: flex; justify-content: space-between; font-size: 11px; color: #7a8497; margin-top: 6px; }
.axis span { flex: 1; text-align: center; }
.axis span:first-child { text-align: left; }
.axis span:last-child { text-align: right; }

/* ---------------- 区块标题 ---------------- */
.section-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: -6px; }
.section-head h3 { margin: 0; font-size: 17px; color: var(--box-ink); }
.link {
  display: inline-flex; align-items: center; gap: 4px; font-size: 13px; color: var(--smc-text-secondary);
  cursor: pointer;
}
.link:hover { color: var(--box-ink); }

/* ---------------- 景区马卡龙卡 ---------------- */
.scenic-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 14px; }
.scenic-card {
  border-radius: 22px; padding: 16px 18px; cursor: pointer; transition: transform 0.15s ease;
  min-width: 0;
}
.scenic-card:hover { transform: translateY(-2px); }
.scenic-card:focus-visible { outline: 2px solid var(--box-ink); outline-offset: 2px; }
.tone-0 { background: var(--box-lavender); }
.tone-1 { background: var(--box-mint); }
.tone-2 { background: var(--box-cream); }
.scenic-top { display: flex; align-items: center; justify-content: space-between; }
.scenic-icon {
  width: 34px; height: 34px; border-radius: 50%; background: #fff; display: inline-flex;
  align-items: center; justify-content: center; color: var(--box-ink);
}
.scenic-when { font-size: 11px; color: rgba(29, 31, 38, 0.55); }
.scenic-name {
  margin-top: 14px; font-size: 14px; font-weight: 600; color: var(--box-ink);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.scenic-num { font-size: 24px; font-weight: 700; color: var(--box-ink); margin-top: 2px; }
.scenic-num small { font-size: 12px; font-weight: 500; color: rgba(29, 31, 38, 0.6); }
.scenic-sub { font-size: 12px; color: rgba(29, 31, 38, 0.6); }

/* ---------------- 任务表 ---------------- */
.task-table { border: 1px solid var(--smc-border); border-radius: 20px; overflow: hidden; }
.tr {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 1.3fr) minmax(0, 1.4fr) 110px 90px;
  gap: 12px;
  align-items: center;
  padding: 12px 18px;
  font-size: 13px;
  border-top: 1px solid var(--smc-border);
  cursor: pointer;
}
.tr:hover:not(.th) { background: #f7f8fa; }
.tr.th { border-top: none; cursor: default; font-size: 12px; color: var(--smc-text-secondary); background: #f7f8fa; }
.tr .num { text-align: right; font-variant-numeric: tabular-nums; }
.task-name { font-weight: 600; color: var(--box-ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status { display: inline-block; font-size: 12px; padding: 2px 10px; border-radius: 999px; }
.st-success { background: var(--box-mint); color: #1f6b3d; }
.st-warning { background: var(--box-cream); color: #7a5a12; }
.st-danger { background: #fde2e2; color: #b42318; }
.st-info { background: #eef0f4; color: #5a6070; }
.tr-empty { padding: 22px; text-align: center; font-size: 13px; color: var(--smc-text-secondary); border-top: 1px solid var(--smc-border); }

/* ---------------- 右栏 ---------------- */
.dark-card {
  background: var(--box-ink);
  color: #fff;
  border-radius: 26px;
  padding: 22px;
}
.dark-number { font-size: 34px; font-weight: 700; margin-top: 4px; }
.dark-number small { font-size: 13px; font-weight: 500; color: rgba(255, 255, 255, 0.6); }
.channel-list { list-style: none; margin: 14px 0 0; padding: 0; }
.channel-list li {
  display: flex; justify-content: space-between; font-size: 13px; padding: 7px 0;
  border-top: 1px solid rgba(255, 255, 255, 0.08); color: rgba(255, 255, 255, 0.8);
}
.channel-list .ok { color: #9be3b4; }
.channel-list .miss { color: #f6c98a; }
.dark-note { font-size: 12px; line-height: 1.7; color: rgba(255, 255, 255, 0.55); margin: 12px 0 16px; }
.cream-btn {
  width: 100%; height: 42px; border: none; border-radius: 999px; cursor: pointer;
  background: var(--box-cream); color: var(--box-ink); font-size: 14px; font-weight: 600;
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
}
.cream-btn:hover { filter: brightness(1.04); }
.side-card {
  border: 1px solid var(--smc-border);
  border-radius: 22px;
  padding: 18px 20px;
  cursor: pointer;
}
.side-card.mint { background: #f3faf5; border-color: transparent; cursor: default; }
.side-head { display: flex; align-items: center; justify-content: space-between; }
.side-arrow { color: var(--smc-text-secondary); }
.side-number { font-size: 24px; font-weight: 700; color: var(--box-ink); margin-top: 6px; }
.side-number small { font-size: 12px; font-weight: 500; color: var(--smc-text-secondary); }
.side-empty { font-size: 12px; line-height: 1.7; margin: 8px 0 0; }
.senti-bar { display: flex; height: 8px; border-radius: 999px; overflow: hidden; margin-top: 12px; background: #eef0f4; gap: 2px; }
.senti-legend { display: flex; gap: 12px; font-size: 12px; color: var(--smc-text-secondary); margin-top: 8px; flex-wrap: wrap; }
.senti-legend i { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }
.pos { background: #5cc489; }
.neu { background: #b7bdca; }
.neg { background: #ef7b7b; }
.assign-row { display: flex; justify-content: space-between; font-size: 13px; margin-top: 8px; }
.assign-row span { color: var(--smc-text-secondary); }
.assign-row b { color: var(--box-ink); font-weight: 600; }

/* ---------------- 空状态 ---------------- */
.empty-card {
  border-radius: 28px; background: var(--box-sky); padding: 48px 32px; text-align: center;
  max-width: 720px; margin: 24px auto;
}
.empty-card h3 { margin: 14px 0 8px; font-size: 20px; color: var(--box-ink); }
.empty-card p { font-size: 13px; line-height: 1.8; color: #5a6478; margin: 0 auto 18px; max-width: 520px; }
.empty-icon {
  width: 56px; height: 56px; border-radius: 50%; background: #fff; margin: 0 auto;
  display: flex; align-items: center; justify-content: center; font-size: 24px; color: var(--box-ink);
}

@media (max-width: 1100px) {
  .grid { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 640px) {
  .trend-card { padding: 18px 16px 12px; }
  .trend-head { flex-direction: column-reverse; }
  .trend-number { font-size: 28px; }
  /* 窄屏横轴只留首尾两天，中间的挤不下 */
  .axis span:not(:first-child):not(:last-child) { visibility: hidden; }
  .tr { grid-template-columns: minmax(0, 1fr) 90px; }
  .tr > :nth-child(2), .tr > :nth-child(3), .tr > :nth-child(5) { display: none; }
}
</style>
