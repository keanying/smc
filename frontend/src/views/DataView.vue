<template>
  <div class="data-view">
    <div class="page-header">
      <div>
        <h2 class="page-title">数据中心</h2>
        <p class="page-subtitle">按景区 + 平台查看作品与评论</p>
      </div>
      <div>
        <el-button :icon="Download" @click="exportCsv('works')">导出作品</el-button>
        <el-button type="primary" :icon="Download" @click="exportCsv('comments')">导出评论</el-button>
      </div>
    </div>

    <!-- ============ 总览：列表往下滚时自动收窄，把高度让给作品区 ============ -->
    <div class="stat-row" :class="{ 'is-compact': compactHeader }">
      <div class="stat-box">
        <div class="stat-num">{{ formatCount(totals.works) }}</div>
        <div class="stat-cap">作品</div>
      </div>
      <div class="stat-box">
        <div class="stat-num">{{ formatCount(totals.comments) }}</div>
        <div class="stat-cap">评论</div>
      </div>
      <div class="stat-box">
        <div class="stat-num">{{ overview.length }}</div>
        <div class="stat-cap">有数据的景区</div>
      </div>
      <div class="stat-box stat-box-wide">
        <div class="stat-cap" style="margin-bottom: 8px">各平台分布（点一段即筛选）</div>

        <div v-if="channelTotals.length" class="dist-bar">
          <div
            v-for="c in channelTotals" :key="c.channel"
            class="dist-seg" :class="{ dimmed: query.channel && query.channel !== c.channel }"
            :style="{ width: `${c.percent}%`, background: CHANNEL_COLORS[c.channel] }"
            :title="`${channelLabel(c.channel)} ${formatCount(c.total)}（${c.percent}%）`"
            @click="filterBy(query.scenic_id, query.channel === c.channel ? '' : c.channel)"
          />
        </div>
        <div v-else class="muted" style="font-size: 12px">还没有采集到数据</div>

        <div class="dist-legend">
          <span
            v-for="c in channelTotals" :key="c.channel" class="legend-item"
            :class="{ active: query.channel === c.channel }"
            @click="filterBy(query.scenic_id, query.channel === c.channel ? '' : c.channel)"
          >
            <i class="channel-dot" :style="{ background: CHANNEL_COLORS[c.channel] }" />
            {{ channelLabel(c.channel) }}
            <b>{{ formatCount(c.total) }}</b>
          </span>
        </div>
      </div>
    </div>

    <!-- ============ 筛选 ============ -->
    <el-card shadow="never" class="filter-card">
      <div class="filter-row">
        <el-select v-model="query.scenic_id" placeholder="全部景区" clearable filterable
                   style="width: 180px" @change="loadWorks(true)">
          <el-option v-for="s in scenics" :key="s.scenic_id" :label="s.scenic_name" :value="s.scenic_id" />
        </el-select>
        <el-select v-model="query.channel" placeholder="全部平台" clearable
                   style="width: 130px" @change="loadWorks(true)">
          <el-option v-for="c in channels" :key="c.value" :label="c.label" :value="c.value" />
        </el-select>
        <el-input
          v-model="query.keyword" placeholder="搜索标题/正文/来源关键字" clearable
          style="width: 240px" :prefix-icon="Search"
          @keyup.enter="loadWorks(true)" @clear="loadWorks(true)"
        />
        <el-date-picker
          v-model="dateRange" type="daterange" range-separator="至"
          start-placeholder="发布起" end-placeholder="发布止"
          value-format="YYYY-MM-DD" style="width: 250px" @change="loadWorks(true)"
        />
        <el-checkbox v-model="query.include_synthetic" @change="loadWorks(true)">
          含携程/同程点评集合
        </el-checkbox>
        <div class="filter-spacer" />
        <el-button v-if="hasFilter" link @click="resetFilters">清空条件</el-button>
        <el-button type="primary" :icon="Search" @click="loadWorks(true)">查询</el-button>
      </div>
    </el-card>

    <!-- ============ 作品列表 ============ -->
    <div class="work-list-head">
      <span class="muted">
        共 <b>{{ worksTotal }}</b> 条作品 · 第 {{ query.page }} / {{ totalPages }} 页
      </span>
      <div class="muted head-tools">
        <span>展开评论后的窗口高度</span>
        <el-radio-group v-model="commentWindowHeight" size="small">
          <el-radio-button :value="280">矮</el-radio-button>
          <el-radio-button :value="380">中</el-radio-button>
          <el-radio-button :value="560">高</el-radio-button>
        </el-radio-group>
      </div>
    </div>

    <!--
      作品区是这个页面**唯一**滚动的地方：筛选条、统计、分页都钉在外面不动。
      样式里的 min-height 必须显式给长度（见 .work-list）——flex 子项默认
      min-height:auto，内容一多就把父容器撑破，overflow 根本不生效，
      表现就是"明明设了固定高度，还是整页在滚"。
    -->
    <div ref="workListEl" v-loading="loadingWorks" class="work-list" @scroll="onListScroll">
      <el-empty v-if="!works.length && !loadingWorks" description="没有符合条件的数据" />

      <article v-for="row in works" :key="`${row.channel}-${row.work_id}-${row.scenic_id}`" class="work-card">
        <div class="work-side">
          <el-tag size="small" :color="CHANNEL_COLORS[row.channel]" class="channel-tag">
            {{ channelLabel(row.channel) }}
          </el-tag>
          <span class="muted work-scenic">{{ row.scenic_name }}</span>
        </div>

        <div class="work-main">
          <div class="work-title">
            <span>{{ row.title || row.description || '（无标题）' }}</span>
            <el-link v-if="row.work_url" type="primary" :href="row.work_url" target="_blank"
                     :icon="Link" class="work-link" />
          </div>

          <div v-if="isSynthetic(row)" class="muted work-note">
            景区点评集合 · 携程/同程没有作品概念，这是评论的挂载点
          </div>
          <div v-else-if="row.description && row.description !== row.title" class="work-desc">
            {{ row.description }}
          </div>

          <MediaGallery :image-list="row.image_list" :video-list="row.video_list" :size="76" />

          <div class="work-meta">
            <span class="meta-author">{{ row.author_name || row.author_id || '未知作者' }}</span>
            <span>{{ formatTime(row.publish_time) }}</span>
            <span v-if="row.location">{{ row.location }}</span>
            <span v-if="row.source_keyword" class="meta-kw">#{{ row.source_keyword }}</span>
          </div>

          <div class="work-stats">
            <span title="点赞">👍 {{ formatCount(row.likes) }}</span>
            <span title="评论">💬 {{ formatCount(row.comment_cnt) }}</span>
            <span title="收藏">⭐ {{ formatCount(row.collection_cnt) }}</span>
            <span title="转发">↗ {{ formatCount(row.shares) }}</span>
            <el-button
              link type="primary" size="small" class="work-toggle"
              @click="toggleComments(row)"
            >
              {{ isOpen(row) ? '收起评论' : '展开评论' }}
              <el-icon><ArrowUp v-if="isOpen(row)" /><ArrowDown v-else /></el-icon>
            </el-button>
          </div>

          <div v-if="isOpen(row)" class="work-comments">
            <CommentTree
              :channel="row.channel" :work-id="row.work_id"
              :scenic-id="row.scenic_id" :max-level="5" :height="commentWindowHeight"
            />
          </div>
        </div>
      </article>
    </div>

    <div class="data-pager">
      <span class="muted pager-label">作品分页</span>

      <el-pagination
        background layout="total, sizes, prev, pager, next, jumper" :total="worksTotal"
        :page-sizes="[10, 20, 50, 100]" :page-size="query.page_size" :current-page="query.page"
        @size-change="(s: number) => { query.page_size = s; loadWorks(true) }"
        @current-change="onPageChange"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ArrowDown, ArrowUp, Download, Link, Search } from '@element-plus/icons-vue'
import {
  dataApi, scenicApi,
  type ChannelOption, type OverviewRow, type ScenicOption, type Work,
} from '../api'
import { CHANNEL_COLORS, channelLabel, formatCount, formatTime } from '../constants'
import CommentTree from '../components/CommentTree.vue'
import MediaGallery from '../components/MediaGallery.vue'

const overview = ref<OverviewRow[]>([])
const works = ref<Work[]>([])
const worksTotal = ref(0)
const scenics = ref<ScenicOption[]>([])
const channels = ref<ChannelOption[]>([])
const loadingWorks = ref(false)
const dateRange = ref<[string, string] | null>(null)
/** 展开评论后那个滚动窗口的高度（px）。跟作品分页无关，别混一起 */
const commentWindowHeight = ref(380)
const workListEl = ref<HTMLElement | null>(null)
/** 作品区往下滚了就把顶部统计收窄，滚回顶再展开 */
const compactHeader = ref(false)
/** 展开了评论的作品，key 是 平台+作品ID+景区 */
const openComments = reactive(new Set<string>())

const query = reactive({
  scenic_id: '', channel: '', keyword: '',
  include_synthetic: true, page: 1, page_size: 20,
})

const totals = computed(() => ({
  works: overview.value.reduce((sum, row) => sum + (row.total_works || 0), 0),
  comments: overview.value.reduce((sum, row) => sum + (row.total_comments || 0), 0),
}))

/** 各平台合计 + 占比，给顶部那条彩色分布用 */
const channelTotals = computed(() => {
  const map = new Map<string, number>()
  for (const row of overview.value) {
    for (const c of row.channels || []) {
      map.set(c.channel, (map.get(c.channel) || 0) + (c.work_cnt || 0) + (c.comment_cnt || 0))
    }
  }
  const rows = [...map.entries()].map(([channel, total]) => ({ channel, total }))
  const sum = rows.reduce((acc, r) => acc + r.total, 0) || 1
  return rows
    .map((r) => ({
      ...r,
      // 太窄的段点不中，给个下限
      percent: Math.max(3, Math.round((r.total / sum) * 1000) / 10),
    }))
    .sort((a, b) => b.total - a.total)
})

const totalPages = computed(
  () => Math.max(1, Math.ceil(worksTotal.value / (query.page_size || 20))),
)

/**
 * 收起/展开用两个不同的阈值（40 / 8）。
 * 只用一个阈值的话，收起来腾出的高度会让内容回弹到阈值之下，
 * 于是又展开、又收起——鼠标不动列表自己抖。
 */
function onListScroll(event: Event) {
  const top = (event.target as HTMLElement).scrollTop
  if (!compactHeader.value && top > 40) compactHeader.value = true
  else if (compactHeader.value && top < 8) compactHeader.value = false
}

const hasFilter = computed(
  () => !!(query.scenic_id || query.channel || query.keyword || dateRange.value),
)

function rowKey(row: Work): string {
  return `${row.channel}-${row.work_id}-${row.scenic_id}`
}

function isOpen(row: Work): boolean {
  return openComments.has(rowKey(row))
}

function toggleComments(row: Work) {
  const key = rowKey(row)
  if (openComments.has(key)) openComments.delete(key)
  else openComments.add(key)
}

function isSynthetic(row: Work): boolean {
  // 后端在 SQL 里算好了这个布尔（见 data_repo.WORK_LIST_COLUMNS）。
  // 列表接口不再返回 extra_content —— 那是个几十 KB 的原始 JSON，
  // 为了一个布尔把它拉过来，整页要多几 MB。
  return row.is_synthetic === true
}

async function loadOverview() {
  overview.value = await dataApi.overview()
}

async function loadWorks(reset = false) {
  if (reset) query.page = 1
  // 换页/换条件时把展开的评论收起来，否则会看到上一页残留的展开状态
  openComments.clear()
  loadingWorks.value = true
  try {
    const result = await dataApi.works({
      ...query,
      start_time: dateRange.value?.[0] ? `${dateRange.value[0]} 00:00:00` : undefined,
      end_time: dateRange.value?.[1] ? `${dateRange.value[1]} 23:59:59` : undefined,
    })
    works.value = result.items
    worksTotal.value = result.total
    if (reset) scrollListToTop()
  } finally {
    loadingWorks.value = false
  }
}

function onPageChange(page: number) {
  query.page = page
  loadWorks()
  scrollListToTop()
}

/** 翻页/换筛选条件后回到列表顶部。
 *  注意滚的是**列表容器**不是 window —— 外层 .app-main 才是页面的滚动容器，
 *  window.scrollTo 在这个布局下什么也不会发生。 */
function scrollListToTop() {
  workListEl.value?.scrollTo({ top: 0, behavior: 'smooth' })
  compactHeader.value = false
}

function filterBy(scenicId: string, channel: string) {
  query.scenic_id = scenicId
  query.channel = channel
  loadWorks(true)
}

function resetFilters() {
  query.scenic_id = ''
  query.channel = ''
  query.keyword = ''
  dateRange.value = null
  loadWorks(true)
}

function exportCsv(kind: 'works' | 'comments') {
  dataApi.exportCsv(kind, {
    scenic_id: query.scenic_id,
    channel: query.channel,
    start_time: dateRange.value?.[0] ? `${dateRange.value[0]} 00:00:00` : '',
    end_time: dateRange.value?.[1] ? `${dateRange.value[1]} 23:59:59` : '',
  })
}

onMounted(async () => {
  // ⚠️ 这四件事**互不依赖**，全部并行。
  // 原来是"先等景区列表和平台列表回来，再去拉概览和作品"，
  // 而景区那个请求当时要几百次聚合查询——首屏就卡在一个只用来
  // 填下拉框的请求上。现在下拉框走精简接口，且不再挡着数据加载。
  const [scenicOptions, channelList] = await Promise.all([
    scenicApi.options(false),
    scenicApi.channels(),
    loadOverview(),
    loadWorks(true),
  ])
  scenics.value = scenicOptions
  channels.value = channelList
})
</script>

<style scoped>
/*
  整页布局：外层 .app-main 是滚动容器，但这个页面自己撑满不滚，
  只让作品区滚。这样筛选条永远在顶上、分页永远在底下，
  不用一路往回滚才能改条件。
*/
.data-view {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.data-view > .page-header,
.data-view > .stat-row,
.data-view > .filter-card,
.data-view > .work-list-head,
.data-view > .data-pager {
  flex: 0 0 auto;
}

/* ---------- 顶部统计 ---------- */
.stat-row {
  display: grid;
  grid-template-columns: repeat(3, 130px) 1fr;
  gap: 12px;
  margin-bottom: 14px;
  transition: gap 0.18s ease;
}

/* 往下滚时收窄：数字和分布条还在，只是不占那么多高度 */
.stat-row.is-compact {
  grid-template-columns: repeat(3, 108px) 1fr;
  gap: 8px;
  margin-bottom: 10px;
}

.stat-row.is-compact .stat-box {
  padding: 6px 12px;
}

.stat-row.is-compact .stat-num {
  font-size: 17px;
}

.stat-row.is-compact .stat-box-wide .stat-cap:first-child,
.stat-row.is-compact .dist-legend {
  display: none;
}

.stat-box {
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 14px 16px;
}

.stat-num {
  font-size: 24px;
  font-weight: 600;
  line-height: 1.2;
  color: #303133;
}

.stat-cap {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}

.stat-box-wide {
  display: flex;
  flex-direction: column;
  justify-content: center;
}

.channel-bar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

/* 彩色堆叠分布条：一眼看出各平台占比，点一段就筛 */
.dist-bar {
  display: flex;
  height: 12px;
  border-radius: 6px;
  overflow: hidden;
  background: #f0f2f5;
}

.dist-seg {
  cursor: pointer;
  transition: opacity 0.15s, filter 0.15s;
}

.dist-seg:hover {
  filter: brightness(1.12);
}

.dist-seg.dimmed {
  opacity: 0.28;
}

.dist-legend {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  margin-top: 8px;
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: #606266;
  cursor: pointer;
}

.legend-item:hover,
.legend-item.active {
  color: var(--el-color-primary);
}

.legend-item.active b {
  color: var(--el-color-primary);
}

.channel-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  display: inline-block;
}

/* ---------- 筛选 ---------- */
.filter-card {
  margin-bottom: 12px;
}

.filter-card :deep(.el-card__body) {
  padding: 14px 16px;
}

.filter-row {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}

.filter-spacer {
  flex: 1 1 auto;
}

/* ---------- 作品卡片 ---------- */
.work-list-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  font-size: 13px;
  flex-wrap: wrap;
  gap: 8px;
}

.head-tools {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

/*
  min-height 必须**显式**写一个长度值。flex 子项的默认 min-height 是 auto，
  意思是"不能小于内容"——内容一多就把父容器顶破，overflow-y 形同虚设，
  页面又退回整页滚。写成 160px 同时还保证列表不会被压到只剩一条缝。
*/
.work-list {
  flex: 1 1 auto;
  min-height: 160px;
  overflow-y: auto;
  overflow-x: hidden;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  background: #fafafa;
  padding: 10px 10px 1px;
}

.work-card {
  display: flex;
  gap: 14px;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 10px;
  transition: box-shadow 0.15s;
}

.work-card:hover {
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.06);
}

.work-side {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  width: 84px;
  flex: 0 0 84px;
}

.channel-tag {
  color: #fff;
  border: none;
}

.work-scenic {
  font-size: 12px;
  line-height: 1.4;
  word-break: break-all;
}

.work-main {
  flex: 1 1 auto;
  min-width: 0;
}

.work-title {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 15px;
  font-weight: 500;
  line-height: 1.5;
  color: #303133;
  word-break: break-word;
}

.work-link {
  flex: 0 0 auto;
  margin-top: 3px;
}

.work-note,
.work-desc {
  font-size: 13px;
  line-height: 1.6;
  margin-top: 4px;
  word-break: break-word;
}

.work-desc {
  color: #606266;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.work-meta {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 12px;
  color: #909399;
  margin-top: 6px;
}

.meta-author {
  color: #606266;
  font-weight: 500;
}

.meta-kw {
  color: var(--el-color-primary);
}

.work-stats {
  display: flex;
  gap: 16px;
  align-items: center;
  font-size: 12px;
  color: #909399;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed #f0f0f0;
}

.work-toggle {
  margin-left: auto;
}

.work-comments {
  margin-top: 10px;
}

/* ---------- 分页 ---------- */
.data-pager {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 12px;
  flex-wrap: wrap;
  gap: 10px;
}

.pager-label {
  font-size: 12px;
}

@media (max-width: 1100px) {
  .stat-row {
    grid-template-columns: repeat(3, 1fr);
  }

  .stat-box-wide {
    grid-column: 1 / -1;
  }
}

/*
  屏幕太矮时（笔记本 + 打开了开发者工具之类），钉住头尾会把作品区压到只剩两三行，
  反而更难用。这种情况退回整页滚动。
*/
@media (max-height: 620px) {
  .data-view {
    height: auto;
  }

  .work-list {
    overflow: visible;
    min-height: 0;
  }
}
</style>
