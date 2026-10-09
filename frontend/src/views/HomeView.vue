<template>
  <div v-loading="loading">
    <div class="page-header">
      <h2 class="page-title">概览</h2>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
    </div>

    <!-- 核心指标：一条细边框横栏，靠数字层级说话，不用彩色图标块 -->
    <div class="kpi-strip">
      <div class="kpi">
        <div class="kpi-label">景区</div>
        <div class="kpi-value">{{ stats.scenics }}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">
          作品
          <InfoTip content="含携程/同程的景区点评集合：这两个平台没有作品概念，评论挂在按景区生成的集合上。" />
        </div>
        <div class="kpi-value">{{ formatCount(stats.works) }}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">评论</div>
        <div class="kpi-value">{{ formatCount(stats.comments) }}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">运行中 / 全部任务</div>
        <div class="kpi-value">
          {{ stats.runningTasks }}<span class="kpi-sub"> / {{ stats.tasks }}</span>
        </div>
      </div>
    </div>

    <!-- 平台接入状态：平铺展示 -->
    <el-card shadow="never" style="margin-bottom: 20px">
      <template #header>
        <span class="card-title">
          平台接入状态
          <InfoTip content="需登录的平台要有已启用、状态正常的账号才算「已配账号」；免登录平台直接可采。" />
        </span>
      </template>
      <div class="platform-grid">
        <div v-for="c in channelStatus" :key="c.value" class="platform-card">
          <div class="platform-icon">
            <img
              v-if="PLATFORM_LOGOS[c.value]" class="platform-logo"
              :src="PLATFORM_LOGOS[c.value]" :alt="c.label"
            />
            <span v-else class="platform-letter" :style="{ color: CHANNEL_COLORS[c.value] }">
              {{ c.label.slice(0, 1) }}
            </span>
          </div>
          <div class="platform-info">
            <div class="platform-name">{{ c.label }}</div>
            <div v-if="c.note" class="platform-note">{{ c.note }}</div>
          </div>
          <el-tag
            v-if="c.needLogin" size="small" :type="c.hasAccount ? 'success' : 'warning'"
            effect="plain" class="platform-status"
          >
            {{ c.hasAccount ? '已配账号' : '缺账号' }}
          </el-tag>
          <el-tag v-else size="small" type="info" effect="plain" class="platform-status">免登录</el-tag>
        </div>
      </div>
    </el-card>

    <el-row :gutter="20">
      <el-col :span="14">
        <el-card shadow="never" header="各景区采集量" class="fill-card">
          <el-empty v-if="!overview.length" description="还没有采集到数据" :image-size="72">
            <el-button type="primary" @click="$router.push('/scenics')">添加景区</el-button>
          </el-empty>
          <el-table v-else :data="overview" size="small">
            <el-table-column prop="scenic_name" label="景区" min-width="130" />
            <el-table-column label="平台分布（评论数）" min-width="300">
              <template #default="{ row }">
                <span v-for="c in row.channels" :key="c.channel" class="dist-item">
                  <i class="dist-dot" :style="{ background: CHANNEL_COLORS[c.channel] }" />
                  {{ channelLabel(c.channel) }}
                  <b>{{ formatCount(c.comment_cnt) }}</b>
                </span>
              </template>
            </el-table-column>
            <el-table-column label="作品" width="80" align="right">
              <template #default="{ row }">{{ formatCount(row.total_works) }}</template>
            </el-table-column>
            <el-table-column label="评论" width="80" align="right">
              <template #default="{ row }">{{ formatCount(row.total_comments) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>

      <el-col :span="10">
        <el-card shadow="never" header="最近任务" class="fill-card">
          <el-empty v-if="!recentTasks.length" description="还没有任务" :image-size="72">
            <el-button type="primary" @click="$router.push('/tasks/new')">新建任务</el-button>
          </el-empty>
          <div v-else>
            <div
              v-for="task in recentTasks" :key="task.task_id"
              class="task-row" @click="$router.push(`/tasks/${task.task_id}`)"
            >
              <div style="flex: 1; min-width: 0">
                <div class="task-name">{{ task.task_name }}</div>
                <div class="task-meta">
                  {{ formatTime(task.create_time) }}
                  · 作品 {{ task.stat_new_works }} · 评论 {{ task.stat_new_comments }}
                </div>
              </div>
              <el-tag :type="TASK_STATUS_TYPES[task.status]" size="small">
                {{ TASK_STATUS_LABELS[task.status] || task.status }}
              </el-tag>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import {
  accountApi, dataApi, scenicApi, taskApi,
  type Account, type ChannelOption, type OverviewRow, type Task,
} from '../api'
import {
  CHANNEL_COLORS, TASK_STATUS_LABELS, TASK_STATUS_TYPES,
  channelLabel, formatCount, formatTime,
} from '../constants'

const loading = ref(false)
const overview = ref<OverviewRow[]>([])
const recentTasks = ref<Task[]>([])
const accounts = ref<Account[]>([])
const channels = ref<ChannelOption[]>([])
const stats = reactive({ scenics: 0, works: 0, comments: 0, tasks: 0, runningTasks: 0 })

// ⚠️ 这里原来写的是 FontAwesome 类名（fa-brands fa-tiktok 之类），
// 但项目从头到尾没有引入 FontAwesome——index.html 没有 CDN link，
// package.json 里没有这个依赖，main.ts 也没 import。
// 于是 <i class="fa-brands fa-tiktok"> 渲染出来就是个空元素，
// 表现就是"平台接入状态的图标没有"。
// 而 src/assets/icons/ 下本来就躺着现成的品牌 svg，之前没有任何地方引用它。
import douyinIcon from '../assets/icons/douyin.svg'
import kuaishouIcon from '../assets/icons/kuaishou.svg'
import xiaohongshuIcon from '../assets/icons/xiaohongshu.svg'
import weiboIcon from '../assets/icons/weibo.svg'

//: 没有品牌 svg 的平台（携程/同程）退回平台名首字，不留空白
const PLATFORM_LOGOS: Record<string, string> = {
  douyin: douyinIcon,
  kuaishou: kuaishouIcon,
  xiaohongshu: xiaohongshuIcon,
  weibo: weiboIcon,
}

const CHANNEL_NOTES: Record<string, string> = {
  douyin: '关键字 / 主页 / 两级评论',
  kuaishou: '关键字 / 主页 / 两级评论',
  xiaohongshu: '关键字 / 主页 / 两级评论',
  weibo: '关键字 / 主页 / 两级评论',
  ctrip: '按 POI_ID 采景区点评',
  tongcheng: '按 sid 采景区点评',
}

const channelStatus = computed(() =>
  channels.value.map((c) => ({
    value: c.value,
    label: c.label,
    note: CHANNEL_NOTES[c.value] || '',
    needLogin: !!c.need_login,
    hasAccount: accounts.value.some(
      (a) => a.channel === c.value && a.enabled && a.status === 'active',
    ),
  })),
)

async function load() {
  loading.value = true
  try {
    const [overviewRows, scenicPage, taskPage, accountPage, channelList] = await Promise.all([
      dataApi.overview(),
      scenicApi.list({ page_size: 1 }),
      taskApi.list({ page_size: 8 }),
      accountApi.list({}).catch(() => ({ items: [] as Account[], total: 0, page: 1, page_size: 0 })),
      accountApi.channels(),
    ])
    overview.value = overviewRows
    recentTasks.value = taskPage.items
    accounts.value = accountPage.items
    channels.value = channelList

    stats.scenics = scenicPage.total
    stats.tasks = taskPage.total
    stats.runningTasks = taskPage.items.filter((t) => t.status === 'running').length
    stats.works = overviewRows.reduce((sum, row) => sum + row.total_works, 0)
    stats.comments = overviewRows.reduce((sum, row) => sum + row.total_comments, 0)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
/* ---------- 核心指标横栏 ---------- */
.kpi-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  background: var(--smc-card-bg);
  border: 1px solid var(--smc-border);
  border-radius: var(--smc-radius);
  margin-bottom: 20px;
}
.kpi {
  padding: 18px 24px 20px;
  min-width: 0;
}
.kpi + .kpi { border-left: 1px solid var(--smc-border); }
.kpi-label {
  font-size: 13px;
  color: var(--smc-text-secondary);
  display: flex;
  align-items: center;
  height: 18px;
}
.kpi-value {
  margin-top: 10px;
  font-size: 30px;
  font-weight: 600;
  line-height: 1.1;
  letter-spacing: -0.5px;
  color: var(--smc-text);
  font-variant-numeric: tabular-nums;
}
.kpi-sub {
  font-size: 16px;
  font-weight: 500;
  color: var(--smc-text-tertiary);
  letter-spacing: 0;
}

.card-title { display: inline-flex; align-items: center; }
.fill-card { height: 100%; }

/* ---------- 平台接入状态 ---------- */
.platform-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}
.platform-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid var(--smc-border);
  border-radius: var(--smc-radius-sm);
  background: var(--smc-card-bg);
}
.platform-icon {
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: var(--smc-bg);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.platform-logo {
  width: 22px;
  height: 22px;
  object-fit: contain;
  display: block;
}
.platform-letter {
  font-size: 15px;
  font-weight: 600;
  line-height: 1;
}
.platform-info {
  flex: 1;
  min-width: 0;
}
.platform-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--smc-text);
}
.platform-note {
  font-size: 12px;
  color: var(--smc-text-secondary);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.platform-status { flex-shrink: 0; }

/* ---------- 各景区平台分布 ---------- */
.dist-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-right: 14px;
  font-size: 12px;
  color: var(--el-text-color-regular);
  white-space: nowrap;
}
.dist-item b { font-weight: 600; color: var(--smc-text); }
.dist-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  display: inline-block;
}

/* ---------- 最近任务 ---------- */
.task-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 11px 8px;
  margin: 0 -8px;
  border-radius: var(--smc-radius-sm);
  border-bottom: 1px solid var(--smc-border);
  cursor: pointer;
  transition: background-color 0.15s ease;
}
.task-row:last-child { border-bottom: none; }
.task-row:hover { background: var(--smc-bg); }
.task-name {
  font-size: 14px;
  color: var(--smc-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.task-meta {
  font-size: 12px;
  color: var(--smc-text-secondary);
  margin-top: 2px;
}

@media (max-width: 900px) {
  .kpi-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .kpi:nth-child(3) { border-left: none; }
  .kpi:nth-child(n + 3) { border-top: 1px solid var(--smc-border); }
}
</style>
