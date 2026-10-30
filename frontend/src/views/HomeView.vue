<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <div v-loading="loading" class="home">
    <div class="page-header">
      <h2 class="page-title">概览</h2>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
    </div>

    <div class="home-grid">
      <!-- ============ 左：主区 ============ -->
      <div class="main-col">
        <div class="hero-row">
          <!-- 采集概览：紫色渐变卡 + 每日采集趋势 -->
          <div class="hero-card">
            <div class="hero-head">
              <span class="hero-title">采集概览</span>
              <el-select v-model="days" size="small" class="hero-select" @change="loadTrend">
                <el-option :value="7" label="近 7 天" />
                <el-option :value="14" label="近 14 天" />
                <el-option :value="30" label="近 30 天" />
              </el-select>
            </div>

            <div class="chart-wrap">
              <svg class="chart" :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none">
                <defs>
                  <linearGradient id="trendFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stop-color="#fff" stop-opacity="0.35" />
                    <stop offset="100%" stop-color="#fff" stop-opacity="0" />
                  </linearGradient>
                </defs>
                <path v-if="chart.area" :d="chart.area" fill="url(#trendFill)" />
                <path v-if="chart.line" :d="chart.line" fill="none" stroke="#fff" stroke-width="2.5"
                      stroke-linecap="round" vector-effect="non-scaling-stroke" />
                <!-- 选中那一天的竖条高亮 -->
                <rect v-if="chart.focus" :x="chart.focus.x - 16" y="0" width="32" :height="H" rx="10"
                      fill="#fff" fill-opacity="0.12" />
              </svg>
              <!-- 圆点和数字气泡用 HTML 叠上去：SVG 被拉伸时圆会变椭圆 -->
              <template v-if="chart.focus">
                <span class="focus-dot" :style="{ left: `${chart.focus.px}%`, top: `${chart.focus.py}%` }" />
                <span class="focus-tip" :class="{ flip: chart.focus.px > 72 }"
                      :style="{ left: `${chart.focus.px}%`, top: `${chart.focus.py}%` }">
                  <b>{{ formatCount(chart.focus.value) }}</b>
                  <small>{{ chart.focus.label }} 评论</small>
                </span>
              </template>
              <div v-if="!trendTotal" class="chart-empty">这段时间还没有采到数据</div>
            </div>
            <div class="chart-axis">
              <button
                v-for="(p, i) in trend" :key="p.date" type="button" class="axis-label"
                :class="{ active: i === focusIndex, faint: !showAxisLabel(i) }"
                @click="focusIndex = i"
              >{{ showAxisLabel(i) || i === focusIndex ? shortDate(p.date) : '' }}</button>
            </div>

            <div class="hero-stats">
              <div class="hero-stat">
                <div class="hs-label">景区</div>
                <div class="hs-value">{{ stats.scenics }}</div>
                <div class="hs-sub">在采集</div>
              </div>
              <div class="hero-stat is-main">
                <div class="hs-label">评论总数</div>
                <div class="hs-value">{{ formatCount(stats.comments) }}</div>
                <div class="hs-sub">近 {{ days }} 天新增 {{ formatCount(trendComments) }}</div>
              </div>
              <div class="hero-stat">
                <div class="hs-label">作品总数</div>
                <div class="hs-value">{{ formatCount(stats.works) }}</div>
                <div class="hs-sub">近 {{ days }} 天新增 {{ formatCount(trendWorks) }}</div>
              </div>
            </div>
          </div>

          <!-- 右边两张彩色小卡 -->
          <div class="side-cards">
            <div role="link" tabindex="0" class="mini-card is-violet" @click="go({ path: '/tasks', query: { status: 'running' } })" @keydown.enter="go({ path: '/tasks', query: { status: 'running' } })">
              <span class="mini-icon"><el-icon :size="22"><VideoPlay /></el-icon></span>
              <div>
                <div class="mini-title">运行中任务</div>
                <div class="mini-sub">共 {{ stats.tasks }} 个任务</div>
              </div>
              <span class="mini-num">{{ stats.runningTasks }}</span>
            </div>
            <div role="link" tabindex="0" class="mini-card is-pink" @click="go('/data')" @keydown.enter="go('/data')">
              <div class="pink-top">
                <span class="mini-icon light"><el-icon :size="20"><ChatDotRound /></el-icon></span>
                <div class="mini-title">今日新增评论</div>
              </div>
              <div class="pink-body">
                <div>
                  <div class="pink-label">{{ todayLabel }}</div>
                  <div class="pink-num">{{ formatCount(todayComments) }}</div>
                  <div class="pink-label">作品 {{ formatCount(todayWorks) }}</div>
                </div>
                <span class="pink-go"><el-icon><ArrowRight /></el-icon></span>
              </div>
              <svg class="pink-wave" viewBox="0 0 200 60" preserveAspectRatio="none">
                <path :d="sparkPath" fill="rgba(255,255,255,0.22)" />
              </svg>
            </div>
          </div>
        </div>

        <!-- 采集量前三的景区 -->
        <div class="section-head">
          <span>采集量靠前的景区</span>
          <button type="button" class="more" @click="go('/scenics')">全部景区</button>
        </div>
        <div v-if="topScenics.length" class="scenic-row">
          <div role="link" tabindex="0" v-for="(s, i) in topScenics" :key="s.scenic_id" class="scenic-card" @click="go({ path: '/data', query: { scenic_id: s.scenic_id } })" @keydown.enter="go({ path: '/data', query: { scenic_id: s.scenic_id } })">
            <span class="scenic-badge" :class="`tone-${i}`"><el-icon :size="22"><LocationFilled /></el-icon></span>
            <div class="scenic-name">{{ s.scenic_name }}</div>
            <div class="scenic-sub">{{ s.channels.length }} 个平台 · 作品 {{ formatCount(s.total_works) }}</div>
            <div class="progress-head">
              <span>评论占比</span>
              <b>{{ share(s.total_comments) }}%</b>
            </div>
            <div class="progress-bar"><i :style="{ width: `${share(s.total_comments)}%` }" /></div>
            <div class="scenic-foot">
              <span>{{ formatCount(s.total_comments) }} / {{ formatCount(stats.comments) }} 条</span>
              <span class="foot-chip">{{ lastCrawlText(s) }}</span>
            </div>
          </div>
        </div>
        <el-empty v-else class="white-empty" description="还没有采集到数据" :image-size="72">
          <el-button type="primary" @click="$router.push('/scenics')">添加景区</el-button>
        </el-empty>
      </div>

      <!-- ============ 右：动态栏 ============ -->
      <aside class="side-panel">
        <div class="panel-head">
          <span class="panel-title"><el-icon><Bell /></el-icon>最近动态</span>
          <button type="button" class="more" @click="go(panelTab === 'tasks' ? '/tasks' : '/accounts')">全部</button>
        </div>
        <div class="segmented">
          <button type="button" :class="{ on: panelTab === 'tasks' }" @click="panelTab = 'tasks'">任务</button>
          <button type="button" :class="{ on: panelTab === 'platforms' }" @click="panelTab = 'platforms'">平台</button>
        </div>

        <div v-if="panelTab === 'tasks'" class="feed">
          <div v-if="!recentTasks.length" class="feed-empty">
            还没有任务
            <el-button size="small" type="primary" @click="$router.push('/tasks/new')">新建任务</el-button>
          </div>
          <div role="link" tabindex="0" v-for="t in recentTasks" :key="t.task_id" class="feed-item" @click="go(`/tasks/${t.task_id}`)" @keydown.enter="go(`/tasks/${t.task_id}`)">
            <span class="feed-avatar" :style="{ background: CHANNEL_COLORS[t.channels?.[0] || ''] || 'var(--smc-primary)' }">
              {{ channelLabel(t.channels?.[0] || '').slice(0, 1) || '任' }}
            </span>
            <div class="feed-main">
              <div class="feed-name">{{ t.task_name }}</div>
              <div class="feed-sub">{{ TASK_STATUS_LABELS[t.status] || t.status }} · 评论 {{ t.stat_new_comments }}</div>
              <div class="feed-time">{{ relTime(t.last_run_time || t.create_time) }}</div>
            </div>
            <span class="feed-state" :class="`st-${t.status}`" />
          </div>
        </div>

        <div v-else class="feed">
          <div v-for="c in channelStatus" :key="c.value" class="feed-item static">
            <span class="feed-avatar logo-avatar">
              <img v-if="PLATFORM_LOGOS[c.value]" :src="PLATFORM_LOGOS[c.value]" :alt="c.label" />
              <b v-else :style="{ color: CHANNEL_COLORS[c.value] }">{{ c.label.slice(0, 1) }}</b>
            </span>
            <div class="feed-main">
              <div class="feed-name">{{ c.label }}</div>
              <div class="feed-sub">{{ c.note }}</div>
            </div>
            <span v-if="c.needLogin" class="state-chip" :class="c.hasAccount ? 'ok' : 'warn'">
              {{ c.hasAccount ? '已配号' : '缺账号' }}
            </span>
            <span v-else class="state-chip info">免登录</span>
          </div>
        </div>

        <div class="panel-divider" />
        <div class="panel-head">
          <span class="panel-title"><el-icon><Connection /></el-icon>集群</span>
          <button type="button" class="more" @click="go('/cluster')">查看</button>
        </div>
        <div class="machines">
          <div role="link" tabindex="0" v-for="n in clusterNodes" :key="n.node_id" class="machine" @click="go({ path: '/tasks', query: { node: n.node_id } })" @keydown.enter="go({ path: '/tasks', query: { node: n.node_id } })">
            <span class="m-dot" :class="n.online ? 'ok' : 'bad'" />
            <span class="m-name">{{ n.node_name }}</span>
            <el-tag v-if="n.is_leader" size="small" effect="dark" round>主机</el-tag>
            <span class="m-load">{{ n.online && n.role !== 'master' ? `${n.running_count}/${n.max_running}` : (n.online ? '协调' : '离线') }}</span>
          </div>
          <div v-if="!clusterNodes.length" class="feed-empty">单机运行</div>
        </div>
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter, type RouteLocationRaw } from 'vue-router'
import {
  ArrowRight, Bell, ChatDotRound, Connection, LocationFilled, Refresh, VideoPlay,
} from '@element-plus/icons-vue'
import {
  accountApi, dataApi, scenicApi, taskApi,
  type Account, type ChannelOption, type OverviewRow, type Task, type TrendPoint,
} from '../api'
import {
  CHANNEL_COLORS, TASK_STATUS_LABELS, channelLabel, formatCount,
} from '../constants'
import { clusterNodes, loadCluster } from '../composables/cluster'

// 平台品牌图标（src/assets/icons 下的 svg）；携程 / 同程没有，退回平台名首字
import douyinIcon from '../assets/icons/douyin.svg'
import kuaishouIcon from '../assets/icons/kuaishou.svg'
import xiaohongshuIcon from '../assets/icons/xiaohongshu.svg'
import weiboIcon from '../assets/icons/weibo.svg'

const PLATFORM_LOGOS: Record<string, string> = {
  douyin: douyinIcon, kuaishou: kuaishouIcon, xiaohongshu: xiaohongshuIcon, weibo: weiboIcon,
}
const CHANNEL_NOTES: Record<string, string> = {
  douyin: '关键字 / 主页 / 两级评论',
  kuaishou: '关键字 / 主页 / 两级评论',
  xiaohongshu: '关键字 / 主页 / 两级评论',
  weibo: '关键字 / 主页 / 两级评论',
  ctrip: '按 POI_ID 采景区点评',
  tongcheng: '按 sid 采景区点评',
  qunar: '按景区采点评',
}

// 卡片用点击跳转、不用 <a> 链接：<a> 鼠标一放上去浏览器左下角就冒出一串网址，很突兀
const router = useRouter()
function go(to: RouteLocationRaw) {
  router.push(to)
}

const loading = ref(false)
const overview = ref<OverviewRow[]>([])
const recentTasks = ref<Task[]>([])
const accounts = ref<Account[]>([])
const channels = ref<ChannelOption[]>([])
const trend = ref<TrendPoint[]>([])
const days = ref(14)
const focusIndex = ref(-1)
const panelTab = ref<'tasks' | 'platforms'>('tasks')
const stats = reactive({ scenics: 0, works: 0, comments: 0, tasks: 0, runningTasks: 0 })

// ---------------- 趋势图 ----------------
const W = 600
const H = 150

const trendComments = computed(() => trend.value.reduce((s, p) => s + p.comments, 0))
const trendWorks = computed(() => trend.value.reduce((s, p) => s + p.works, 0))
const trendTotal = computed(() => trendComments.value + trendWorks.value)
const today = computed(() => trend.value[trend.value.length - 1])
const todayComments = computed(() => today.value?.comments || 0)
const todayWorks = computed(() => today.value?.works || 0)
const todayLabel = computed(() => (today.value ? shortDate(today.value.date) : '今天'))

/** 平滑曲线：Catmull-Rom 转三次贝塞尔，比折线好看，又不会冲出数据范围太多 */
function smooth(points: [number, number][]): string {
  if (!points.length) return ''
  let d = `M${points[0][0]},${points[0][1]}`
  for (let i = 0; i < points.length - 1; i += 1) {
    const p0 = points[i - 1] || points[i]
    const p1 = points[i]
    const p2 = points[i + 1]
    const p3 = points[i + 2] || p2
    const c1x = p1[0] + (p2[0] - p0[0]) / 6
    const c1y = p1[1] + (p2[1] - p0[1]) / 6
    const c2x = p2[0] - (p3[0] - p1[0]) / 6
    const c2y = p2[1] - (p3[1] - p1[1]) / 6
    d += ` C${c1x},${c1y} ${c2x},${c2y} ${p2[0]},${p2[1]}`
  }
  return d
}

function plot(values: number[], width: number, height: number, padTop: number, padBottom: number) {
  const max = Math.max(1, ...values)
  const step = values.length > 1 ? width / (values.length - 1) : 0
  return values.map((v, i) => [
    i * step,
    padTop + (height - padTop - padBottom) * (1 - v / max),
  ] as [number, number])
}

const chart = computed(() => {
  const values = trend.value.map((p) => p.comments)
  if (!values.length) return { line: '', area: '', focus: null as null | Record<string, any> }
  const pts = plot(values, W, H, 28, 10)
  const line = smooth(pts)
  const area = `${line} L${W},${H} L0,${H} Z`
  const idx = focusIndex.value >= 0 && focusIndex.value < pts.length ? focusIndex.value : pts.length - 1
  const [x, y] = pts[idx]
  return {
    line, area,
    focus: {
      x, y, px: (x / W) * 100, py: (y / H) * 100,
      value: values[idx], label: shortDate(trend.value[idx].date),
    },
  }
})

/** 右边粉色卡片底下的小波浪：同一组数据的迷你版 */
const sparkPath = computed(() => {
  const values = trend.value.map((p) => p.comments)
  if (values.length < 2) return ''
  const pts = plot(values, 200, 60, 10, 0)
  return `${smooth(pts)} L200,60 L0,60 Z`
})

function shortDate(date: string): string {
  const [, m, d] = date.split('-')
  return `${Number(m)}/${Number(d)}`
}

/** 横轴标签太挤时隔几个显示一个 */
function showAxisLabel(i: number): boolean {
  const n = trend.value.length
  const every = n > 20 ? 5 : n > 10 ? 2 : 1
  return i % every === 0 || i === n - 1
}

// ---------------- 景区卡片 ----------------
const topScenics = computed(() =>
  [...overview.value].sort((a, b) => b.total_comments - a.total_comments).slice(0, 3))

function share(n: number): number {
  return stats.comments ? Math.round((n / stats.comments) * 100) : 0
}

function lastCrawlText(s: OverviewRow): string {
  const times = s.channels.map((c) => c.last_crawl).filter(Boolean) as string[]
  if (!times.length) return '还没采过'
  const latest = times.sort().pop() as string
  return `${relTime(latest)}采过`
}

// ---------------- 右栏 ----------------
const channelStatus = computed(() =>
  channels.value.map((c) => ({
    value: c.value,
    label: c.label,
    note: CHANNEL_NOTES[c.value] || '',
    needLogin: !!c.need_login,
    hasAccount: accounts.value.some((a) => a.channel === c.value && a.enabled && a.status === 'active'),
  })),
)

function relTime(value?: string | null): string {
  if (!value) return ''
  const t = new Date(String(value).replace(' ', 'T')).getTime()
  if (Number.isNaN(t)) return ''
  const diff = Math.max(0, (Date.now() - t) / 1000)
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`
  return String(value).slice(0, 10)
}

// ---------------- 加载 ----------------
async function loadTrend() {
  trend.value = await dataApi.trend(days.value).catch(() => [])
  // 默认高亮采得最多的那天：今天常常还没开始采，停在 0 上没什么可看
  const values = trend.value.map((p) => p.comments)
  const max = Math.max(0, ...values)
  focusIndex.value = max > 0 ? values.lastIndexOf(max) : trend.value.length - 1
}

async function load() {
  loading.value = true
  try {
    const [overviewRows, scenicPage, taskPage, runningPage, accountPage, channelList] = await Promise.all([
      dataApi.overview(),
      scenicApi.list({ page_size: 1 }),
      taskApi.list({ page_size: 6, sort: 'recent' }),
      taskApi.list({ status: 'running', page_size: 1 }),
      accountApi.list({}).catch(() => ({ items: [] as Account[], total: 0, page: 1, page_size: 0 })),
      accountApi.channels(),
      loadTrend(),
      loadCluster(),
    ])
    overview.value = overviewRows
    recentTasks.value = taskPage.items
    accounts.value = accountPage.items
    channels.value = channelList
    stats.scenics = scenicPage.total
    stats.tasks = taskPage.total
    stats.runningTasks = runningPage.total
    stats.works = overviewRows.reduce((sum, row) => sum + row.total_works, 0)
    stats.comments = overviewRows.reduce((sum, row) => sum + row.total_comments, 0)
  } finally {
    loading.value = false
  }
}

/**
 * 「最近动态」和运行中任务数每 30 秒自己刷新一次：定时任务在后台跑起来、跑完，
 * 页面开着不动也能看到（不用点「刷新」）。悄悄刷，失败了不弹错。
 */
async function refreshFeed() {
  if (document.hidden) return
  try {
    const [taskPage, runningPage] = await Promise.all([
      taskApi.list({ page_size: 6, sort: 'recent' }, true),
      taskApi.list({ status: 'running', page_size: 1 }, true),
    ])
    recentTasks.value = taskPage.items
    stats.tasks = taskPage.total
    stats.runningTasks = runningPage.total
  } catch {
    /* 下一轮再试 */
  }
}

let feedTimer: number | undefined
onMounted(() => {
  load()
  feedTimer = window.setInterval(refreshFeed, 30_000)
})
onUnmounted(() => window.clearInterval(feedTimer))
</script>

<style scoped>
.home-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 22px;
  align-items: start;
}
.main-col { min-width: 0; }

/* ---------- 采集概览（紫色渐变） ---------- */
.hero-row { display: grid; grid-template-columns: minmax(0, 1.65fr) minmax(220px, 1fr); gap: 20px; }
.hero-card {
  position: relative;
  border-radius: 24px;
  padding: 20px 22px 0;
  color: #fff;
  background:
    radial-gradient(500px 220px at 100% 0%, rgba(255, 255, 255, 0.12), transparent 60%),
    var(--smc-gradient);
  box-shadow: 0 18px 40px rgba(93, 78, 201, 0.32);
  overflow: hidden;
}
.hero-head { display: flex; align-items: center; justify-content: space-between; }
.hero-title { font-size: 17px; font-weight: 700; letter-spacing: 0.3px; }
.hero-select { width: 104px; }
.hero-select :deep(.el-select__wrapper) {
  background: rgba(255, 255, 255, 0.14);
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.35) !important;
  border-radius: 10px;
}
.hero-select :deep(.el-select__placeholder), .hero-select :deep(.el-select__caret),
.hero-select :deep(.el-select__selected-item) { color: #fff; }

.chart-wrap { position: relative; height: 150px; margin: 10px -22px 0; }
.chart { width: 100%; height: 100%; display: block; }
.chart-empty {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  font-size: 13px; color: rgba(255, 255, 255, 0.75);
}
.focus-dot {
  position: absolute; width: 14px; height: 14px; border-radius: 50%;
  background: var(--smc-accent); border: 3px solid #fff; transform: translate(-50%, -50%);
  box-shadow: 0 4px 10px rgba(0, 0, 0, 0.2);
}
.focus-tip {
  position: absolute; transform: translate(14px, -50%); white-space: nowrap;
  background: rgba(40, 32, 90, 0.82); border-radius: 10px; padding: 6px 10px; line-height: 1.2;
  display: flex; flex-direction: column;
}
.focus-tip.flip { transform: translate(calc(-100% - 14px), -50%); }
.focus-tip b { font-size: 15px; }
.focus-tip small { font-size: 11px; opacity: 0.8; }

.chart-axis { display: flex; justify-content: space-between; gap: 2px; padding: 8px 0 4px; }
.axis-label {
  border: none; background: transparent; color: rgba(255, 255, 255, 0.7);
  font-size: 11px; padding: 3px 6px; border-radius: 8px; cursor: pointer; min-width: 0;
}
.axis-label.active { background: #fff; color: var(--smc-primary); font-weight: 700; }

.hero-stats {
  display: grid; grid-template-columns: 1fr 1.15fr 1fr; margin: 10px -22px 0;
  border-top: 1px solid rgba(255, 255, 255, 0.16);
}
.hero-stat { text-align: center; padding: 16px 8px 18px; }
.hero-stat.is-main {
  background: rgba(255, 255, 255, 0.12);
  border-radius: 22px 22px 0 0;
  margin-top: -14px; padding-top: 30px;
}
.hs-label { font-size: 12px; opacity: 0.8; }
.hs-value { font-size: 26px; font-weight: 800; margin: 4px 0 2px; letter-spacing: 0.3px; }
.hero-stat.is-main .hs-value { font-size: 30px; }
.hs-sub { font-size: 11px; opacity: 0.75; }

/* ---------- 右侧两张彩色卡 ---------- */
.side-cards { display: flex; flex-direction: column; gap: 18px; }
.mini-card {
  cursor: pointer;
  border-radius: 24px; color: #fff; text-decoration: none; position: relative; overflow: hidden;
  transition: transform 0.18s ease, box-shadow 0.18s ease;
}
.mini-card:hover { transform: translateY(-2px); }
.mini-card.is-violet {
  display: flex; align-items: center; gap: 14px; padding: 18px 20px;
  background: var(--smc-gradient); box-shadow: 0 14px 30px rgba(93, 78, 201, 0.28);
}
.mini-icon {
  width: 48px; height: 48px; border-radius: 16px; flex: 0 0 auto;
  display: inline-flex; align-items: center; justify-content: center;
  background: rgba(255, 255, 255, 0.2); box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.25);
}
.mini-title { font-size: 15px; font-weight: 700; }
.mini-sub { font-size: 12px; opacity: 0.8; margin-top: 2px; }
.mini-num { margin-left: auto; font-size: 30px; font-weight: 800; }
.mini-card.is-pink {
  flex: 1; padding: 18px 20px; display: flex; flex-direction: column; justify-content: space-between;
  background: var(--smc-gradient-accent); box-shadow: 0 14px 30px rgba(242, 100, 138, 0.3); min-height: 200px;
}
.pink-top { display: flex; align-items: center; gap: 12px; position: relative; z-index: 1; }
.pink-body { display: flex; align-items: flex-end; justify-content: space-between; position: relative; z-index: 1; }
.pink-label { font-size: 12px; opacity: 0.85; }
.pink-num { font-size: 32px; font-weight: 800; line-height: 1.15; margin: 2px 0; }
.pink-go {
  width: 32px; height: 32px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
  border: 2px solid rgba(255, 255, 255, 0.8);
}
.pink-wave { position: absolute; left: 0; right: 0; bottom: 0; width: 100%; height: 55%; }

/* ---------- 景区卡片 ---------- */
.section-head {
  display: flex; align-items: center; justify-content: space-between;
  margin: 26px 2px 14px; font-size: 15px; font-weight: 700;
}
.more {
  font-size: 12px; font-weight: 500; color: var(--smc-primary); text-decoration: none;
  border: none; background: none; padding: 0; cursor: pointer; font-family: inherit;
}
.more:hover { text-decoration: underline; }
.scenic-row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px; padding-top: 22px; }
.scenic-card {
  cursor: pointer;
  position: relative; background: #fff; border-radius: 22px; padding: 38px 18px 16px;
  box-shadow: var(--smc-shadow); text-align: center; color: inherit; text-decoration: none;
  transition: box-shadow 0.18s ease, transform 0.18s ease;
}
.scenic-card:hover { box-shadow: var(--smc-shadow-hover); transform: translateY(-2px); }
.scenic-badge {
  position: absolute; top: -22px; left: 50%; transform: translateX(-50%);
  width: 50px; height: 50px; border-radius: 16px; color: #fff;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--smc-gradient); box-shadow: 0 10px 22px rgba(93, 78, 201, 0.35);
}
.scenic-badge.tone-1 { background: linear-gradient(145deg, #8f84ea, #6c5dd3); }
.scenic-badge.tone-2 { background: linear-gradient(145deg, #a79ff0, #7d6fe2); }
.scenic-name { font-size: 15px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.scenic-sub { font-size: 12px; color: var(--smc-text-secondary); margin: 4px 0 16px; }
.progress-head { display: flex; justify-content: space-between; font-size: 12px; color: var(--smc-text-secondary); }
.progress-head b { color: var(--smc-text); }
.progress-bar { height: 6px; border-radius: 6px; background: #eef0f5; margin: 6px 0 14px; overflow: hidden; }
.progress-bar i { display: block; height: 100%; border-radius: 6px; background: linear-gradient(90deg, #34d399, #10b981); }
.scenic-foot { display: flex; align-items: center; justify-content: space-between; font-size: 12px; color: var(--smc-text-secondary); }
.foot-chip {
  background: #ffeef3; color: var(--smc-accent); border-radius: 10px; padding: 4px 10px; font-weight: 600;
  white-space: nowrap;
}
.white-empty { background: #fff; border-radius: 22px; box-shadow: var(--smc-shadow); }

/* ---------- 右栏 ---------- */
.side-panel {
  background: #fff; border-radius: 24px; padding: 20px 18px; box-shadow: var(--smc-shadow);
  position: sticky; top: 0;
}
.panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.panel-title { display: inline-flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 700; }
.panel-title .el-icon { color: var(--smc-primary); }
.segmented {
  display: grid; grid-template-columns: 1fr 1fr; gap: 4px; padding: 4px;
  background: #f1f0f9; border-radius: 14px; margin-bottom: 12px;
}
.segmented button {
  border: none; background: transparent; border-radius: 10px; padding: 7px 0;
  font-size: 13px; color: var(--smc-text-secondary); cursor: pointer; font-weight: 600;
}
.segmented button.on { background: var(--smc-gradient); color: #fff; box-shadow: 0 6px 14px rgba(93, 78, 201, 0.3); }
.feed { display: flex; flex-direction: column; }
.feed-item {
  display: flex; align-items: center; gap: 12px; padding: 10px 6px; border-radius: 14px;
  color: inherit; text-decoration: none; transition: background 0.15s;
}
.feed-item:not(.static) { cursor: pointer; }
.feed-item:hover:not(.static) { background: #f7f6fd; }
.feed-avatar {
  width: 38px; height: 38px; border-radius: 50%; flex: 0 0 auto; color: #fff; font-weight: 700;
  display: inline-flex; align-items: center; justify-content: center; font-size: 14px;
}
.logo-avatar { background: #f4f3fb; }
.logo-avatar img { width: 22px; height: 22px; }
.feed-main { flex: 1; min-width: 0; }
.feed-name { font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.feed-sub { font-size: 12px; color: var(--smc-text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.feed-time { font-size: 11px; color: var(--smc-text-tertiary); }
.feed-state { width: 10px; height: 10px; border-radius: 50%; background: #c9c6dd; flex: 0 0 auto; }
.feed-state.st-running { background: #f59e0b; box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.18); }
.feed-state.st-completed { background: #22c55e; }
.feed-state.st-failed { background: #ef4444; }
.feed-state.st-waiting, .feed-state.st-queued { background: var(--smc-primary); }
.feed-empty { font-size: 13px; color: var(--smc-text-secondary); padding: 12px 6px; display: flex; gap: 10px; align-items: center; }
.state-chip { font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 8px; white-space: nowrap; }
.state-chip.ok { background: #e8f8ef; color: #16a34a; }
.state-chip.warn { background: #fff4e5; color: #d97706; }
.state-chip.info { background: #f1f0f9; color: var(--smc-text-secondary); }
.panel-divider { height: 1px; background: var(--smc-border); margin: 14px 0 16px; }
.machines {
  display: flex; flex-direction: column; gap: 8px; padding: 12px;
  border-radius: 18px; background: linear-gradient(160deg, #f4f3fc, #fbfaff);
}
.machine {
  cursor: pointer;
  display: flex; align-items: center; gap: 8px; font-size: 13px; color: inherit; text-decoration: none;
  padding: 8px 10px; border-radius: 12px; background: #fff; box-shadow: 0 2px 8px rgba(76, 64, 160, 0.06);
}
.m-dot { width: 8px; height: 8px; border-radius: 50%; }
.m-dot.ok { background: #22c55e; }
.m-dot.bad { background: #ef4444; }
.m-name { flex: 1; min-width: 0; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.m-load { font-size: 12px; color: var(--smc-text-secondary); }

@media (max-width: 1280px) {
  .home-grid { grid-template-columns: minmax(0, 1fr); }
  .side-panel { position: static; }
}
@media (max-width: 960px) {
  .hero-row { grid-template-columns: minmax(0, 1fr); }
  .scenic-row { grid-template-columns: minmax(0, 1fr); gap: 34px; }
}
</style>
