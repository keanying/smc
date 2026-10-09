<template>
  <div v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">概览</h2>
        <p class="page-subtitle">六个平台的采集情况一览</p>
      </div>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
    </div>

    <div class="stat-grid" style="margin-bottom: 20px">
      <div class="stat-card">
        <div class="stat-icon" style="background: #eef2ff; color: #4f6ef7">
          <el-icon><LocationFilled /></el-icon>
        </div>
        <div class="stat-value">{{ stats.scenics }}</div>
        <div class="stat-label">景区</div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background: #ecfdf5; color: #10b981">
          <el-icon><Document /></el-icon>
        </div>
        <div class="stat-value">{{ formatCount(stats.works) }}</div>
        <div class="stat-label">作品 / 点评集合</div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background: #fff7ed; color: #f59e0b">
          <el-icon><ChatDotRound /></el-icon>
        </div>
        <div class="stat-value">{{ formatCount(stats.comments) }}</div>
        <div class="stat-label">评论</div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background: #fdf2f8; color: #ec4899">
          <el-icon><VideoPlay /></el-icon>
        </div>
        <div class="stat-value">
          {{ stats.runningTasks }}<span class="muted" style="font-size: 16px"> / {{ stats.tasks }}</span>
        </div>
        <div class="stat-label">运行中 / 全部任务</div>
      </div>
    </div>

    <!-- 平台接入状态：平铺展示 -->
    <el-card shadow="never" header="平台接入状态" style="margin-bottom: 20px">
      <div class="platform-grid">
        <div v-for="c in channelStatus" :key="c.value" class="platform-card">
          <div class="platform-icon" :style="{ background: CHANNEL_COLORS[c.value] + '20', color: CHANNEL_COLORS[c.value] }">
            <img
              v-if="PLATFORM_LOGOS[c.value]" class="platform-logo"
              :src="PLATFORM_LOGOS[c.value]" :alt="c.label"
            />
            <span v-else class="platform-letter">{{ c.label.slice(0, 1) }}</span>
          </div>
          <div class="platform-info">
            <div class="platform-name">{{ c.label }}</div>
            <div class="platform-note">{{ c.note }}</div>
          </div>
          <div class="platform-status">
            <el-tag v-if="c.needLogin" size="small" :type="c.hasAccount ? 'success' : 'warning'" effect="plain">
              {{ c.hasAccount ? '已配账号' : '缺账号' }}
            </el-tag>
            <el-tag v-else size="small" type="info" effect="plain">免登录</el-tag>
          </div>
        </div>
      </div>
    </el-card>

    <el-row :gutter="20">
      <el-col :span="14">
        <el-card shadow="never" header="各景区采集量">
          <el-empty v-if="!overview.length" description="还没有采集到数据">
            <el-button type="primary" @click="$router.push('/scenics')">先去添加景区</el-button>
          </el-empty>
          <el-table v-else :data="overview" size="small">
            <el-table-column prop="scenic_name" label="景区" min-width="130" />
            <el-table-column label="平台分布" min-width="300">
              <template #default="{ row }">
                <el-tag
                  v-for="c in row.channels" :key="c.channel" size="small"
                  :color="CHANNEL_COLORS[c.channel]" style="color: #fff; border: none; margin: 2px 4px 2px 0"
                >
                  {{ channelLabel(c.channel) }} {{ c.comment_cnt }}
                </el-tag>
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
        <el-card shadow="never" header="最近任务">
          <el-empty v-if="!recentTasks.length" description="还没有任务" :image-size="70">
            <el-button type="primary" @click="$router.push('/tasks/new')">新建任务</el-button>
          </el-empty>
          <div v-else>
            <div
              v-for="task in recentTasks" :key="task.task_id"
              class="task-row" @click="$router.push(`/tasks/${task.task_id}`)"
            >
              <div style="flex: 1; min-width: 0">
                <div class="task-name">{{ task.task_name }}</div>
                <div class="muted" style="font-size: 12px">
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
import {
  ChatDotRound, Document, LocationFilled, Refresh, VideoPlay,
} from '@element-plus/icons-vue'
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
.task-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 0;
  border-bottom: 1px solid #f0f0f0;
  cursor: pointer;
  transition: background-color 0.2s ease;
}
.task-row:last-child { border-bottom: none; }
.task-row:hover { background: #fafbfc; }
.task-name {
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.channel-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 0;
  border-bottom: 1px solid #f5f5f5;
  transition: background-color 0.2s ease;
}
.channel-row:last-child { border-bottom: none; }
.channel-row:hover { background: #fafbfc; }

/* 平台接入状态平铺 */
.platform-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
}
.platform-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px;
  background: #f8fafc;
  border: 1px solid #e8ecf3;
  border-radius: 12px;
  transition: all 0.2s ease;
}
.platform-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(16, 24, 40, 0.08);
  border-color: #4f6ef7;
}
.platform-icon {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  flex-shrink: 0;
}
.platform-logo {
  width: 26px;
  height: 26px;
  object-fit: contain;
  display: block;
}
.platform-letter {
  font-size: 18px;
  font-weight: 600;
  line-height: 1;
}
.platform-info {
  flex: 1;
  min-width: 0;
}
.platform-name {
  font-size: 14px;
  font-weight: 600;
  color: #1f2733;
  margin-bottom: 4px;
}
.platform-note {
  font-size: 12px;
  color: #7a8699;
}
.platform-status {
  flex-shrink: 0;
}
</style>
