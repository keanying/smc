<template>
  <div v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">{{ task?.task_name || '任务详情' }}</h2>
        <p class="page-subtitle mono">{{ taskId }}</p>
      </div>
      <div>
        <el-button @click="$router.push('/tasks')">返回列表</el-button>
        <el-button :icon="Edit" @click="editVisible = true">编辑</el-button>
        <el-button
          v-if="task && task.status !== 'running'" type="primary"
          :icon="VideoPlay" @click="run"
        >立即执行</el-button>
        <el-button v-else type="warning" :icon="VideoPause" @click="cancel">取消执行</el-button>
      </div>
    </div>

    <el-row :gutter="16">
      <el-col :span="16">
        <!-- 实时画面放在日志上面：无头模式下"日志说做了什么、画面说页面现在什么样"，
             两个挨着看才对得上。没有正在跑的浏览器时它自己会收成一行说明。 -->
        <LiveScreen :task-id="taskId" :running="task?.status === 'running'" />

        <el-card shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between">
              <span>实时日志</span>
              <div style="display: flex; align-items: center; gap: 10px">
                <el-tag :type="connected ? 'success' : 'info'" size="small" effect="plain">
                  {{ connected ? '已连接' : '未连接' }}
                </el-tag>
                <el-checkbox v-model="autoScroll" label="自动滚动" size="small" />
                <el-button link size="small" @click="clearLogs">清空</el-button>
              </div>
            </div>
          </template>

          <div ref="terminal" class="log-terminal">
            <div v-if="!logs.length" class="muted">暂无日志。任务开始执行后会实时显示在这里。</div>
            <div v-for="(log, index) in logs" :key="index" class="log-line">
              <span class="log-time">{{ log.log_time.slice(11, 19) }}</span>
              <span v-if="log.channel" class="log-channel">[{{ channelLabel(log.channel) }}]</span>
              <span :class="`log-${log.level}`">{{ log.message }}</span>
            </div>
          </div>
        </el-card>
      </el-col>

      <el-col :span="8">
        <el-card shadow="never" header="任务状态">
          <el-descriptions :column="1" size="small" border>
            <el-descriptions-item label="状态">
              <el-tag :type="TASK_STATUS_TYPES[task?.status || '']" size="small">
                {{ TASK_STATUS_LABELS[task?.status || ''] || task?.status }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="进度">
              <el-progress :percentage="task?.progress || 0" :stroke-width="10" />
            </el-descriptions-item>
            <el-descriptions-item label="景区">{{ task?.scenic_id || '-' }}</el-descriptions-item>
            <el-descriptions-item label="平台">
              <el-tag
                v-for="c in task?.channels || []" :key="c" size="small"
                :color="CHANNEL_COLORS[c]" style="color: #fff; border: none; margin-right: 4px"
              >{{ channelLabel(c) }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="采集方式">{{ task?.collect_type }}</el-descriptions-item>
            <el-descriptions-item label="调度">
              {{ SCHEDULE_LABELS[task?.schedule_type || ''] }}
              <span v-if="task?.cron_expression" class="mono">（{{ task.cron_expression }}）</span>
            </el-descriptions-item>
            <el-descriptions-item label="下次执行">{{ formatTime(task?.next_run_time) }}</el-descriptions-item>
            <el-descriptions-item label="已运行次数">{{ task?.runs_count || 0 }}</el-descriptions-item>
          </el-descriptions>

          <div class="stat-grid" style="margin-top: 16px; grid-template-columns: 1fr 1fr">
            <div class="stat-card">
              <div class="stat-value">{{ task?.stat_new_works || 0 }}</div>
              <div class="stat-label">新增作品</div>
            </div>
            <div class="stat-card">
              <div class="stat-value">{{ task?.stat_new_comments || 0 }}</div>
              <div class="stat-label">新增评论</div>
            </div>
            <div class="stat-card">
              <div class="stat-value">{{ task?.stat_updated_works || 0 }}</div>
              <div class="stat-label">更新作品</div>
            </div>
            <div class="stat-card">
              <div class="stat-value">{{ task?.stat_updated_comments || 0 }}</div>
              <div class="stat-label">更新评论</div>
            </div>
          </div>

          <el-alert
            v-if="task?.error" type="error" :closable="false" style="margin-top: 14px"
            title="最近一次错误" :description="task.error"
          />
        </el-card>

        <el-card shadow="never" style="margin-top: 16px">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span>采集上限</span>
              <el-button link type="primary" size="small" @click="editVisible = true">修改</el-button>
            </div>
          </template>
          <el-table :data="task?.collect_limits || []" size="small" border>
            <el-table-column label="平台" width="80">
              <template #default="{ row }">{{ channelLabel(row.channel) }}</template>
            </el-table-column>
            <el-table-column label="采集模式" width="90">
              <template #default="{ row }">
                <el-tag
                  v-if="row.supports_browser_engine" size="small" effect="plain"
                  :type="row.collect_engine === 'human' ? 'warning' : 'info'"
                >{{ engineLabel(row.collect_engine) }}</el-tag>
                <span v-else class="muted">接口</span>
              </template>
            </el-table-column>
            <el-table-column label="作品数" width="80" align="right">
              <template #default="{ row }">
                <span v-if="row.has_works">{{ row.max_works || '不限' }}</span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="评论数" align="right">
              <template #default="{ row }">
                {{ row.max_comments || '不限' }}
                <el-tag v-if="row.customized" size="small" type="warning" effect="plain">单独设置</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="翻页" width="70" align="right">
              <template #default="{ row }">
                <span v-if="!row.has_works">{{ row.max_pages || '不限' }}</span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <template #empty><span class="muted">按全局默认采集</span></template>
          </el-table>

          <template v-if="task?.search_filters?.length">
            <el-divider content-position="left" style="margin: 14px 0 8px">搜索条件</el-divider>
            <div v-for="row in task.search_filters" :key="row.channel"
                 class="muted" style="font-size: 12px; line-height: 1.9">
              <b>{{ channelLabel(row.channel) }}</b>：{{ row.description }}
            </div>
          </template>
          <template v-if="task?.content_filter">
            <el-divider content-position="left" style="margin: 14px 0 8px">内容过滤</el-divider>
            <div class="muted" style="font-size: 12px; line-height: 1.9">
              {{ task.content_filter.description }}
            </div>
          </template>
          <div class="muted" style="font-size: 12px; margin-top: 6px">
            携程/同程没有作品，只有景区点评，所以作品数一栏为空。
          </div>
        </el-card>

        <el-card shadow="never" style="margin-top: 16px" header="关键字">
          <div v-if="task?.keywords?.length">
            <el-tag
              v-for="k in task.keywords" :key="k" size="small"
              style="margin: 0 6px 6px 0"
            >{{ k }}</el-tag>
          </div>
          <span v-else class="muted">未指定（执行时从景区自动带入）</span>
        </el-card>
      </el-col>
    </el-row>

    <TaskEditDialog
      v-model="editVisible" :task="task" :channel-options="channelOptions"
      @saved="onSaved"
    />
  </div>
</template>

<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Edit, VideoPause, VideoPlay } from '@element-plus/icons-vue'
import { scenicApi, taskApi, type ChannelOption, type Task, type TaskLog } from '../api'
import LiveScreen from '../components/LiveScreen.vue'
import TaskEditDialog from '../components/TaskEditDialog.vue'
import { TaskLogSocket } from '../api/ws'
import {
  CHANNEL_COLORS, COLLECT_ENGINES, SCHEDULE_LABELS, TASK_STATUS_LABELS, TASK_STATUS_TYPES,
  channelLabel, formatTime, normalizeEngine,
} from '../constants'

function engineLabel(value: unknown): string {
  const engine = normalizeEngine(value)
  return COLLECT_ENGINES.find((item) => item.value === engine)?.label || engine
}

const route = useRoute()
const taskId = route.params.taskId as string

const task = ref<Task | null>(null)
const logs = ref<TaskLog[]>([])
const loading = ref(false)
const connected = ref(false)
const autoScroll = ref(true)
const terminal = ref<HTMLElement | null>(null)
const editVisible = ref(false)
const channelOptions = ref<ChannelOption[]>([])

let socket: TaskLogSocket | null = null
let timer: number | undefined

async function loadTask() {
  task.value = await taskApi.detail(taskId)
}

async function loadHistory() {
  // WebSocket 只推新日志，历史部分要先拉一次补齐
  logs.value = await taskApi.logs(taskId, 0, 1000)
  scrollToBottom()
}

function scrollToBottom() {
  if (!autoScroll.value) return
  nextTick(() => {
    if (terminal.value) terminal.value.scrollTop = terminal.value.scrollHeight
  })
}

async function run() {
  await taskApi.run(taskId)
  ElMessage.success('已开始执行')
  await loadTask()
}

async function cancel() {
  await taskApi.cancel(taskId)
  ElMessage.success('已发送取消信号')
  await loadTask()
}

async function onSaved(updated: Task) {
  task.value = updated
  ElMessage.success(
    updated.is_running ? '已保存，任务正在运行，改动从下一轮开始生效' : '已保存',
  )
}

async function clearLogs() {
  await taskApi.clearLogs(taskId)
  logs.value = []
  ElMessage.success('日志已清空')
}

onMounted(async () => {
  loading.value = true
  try {
    await Promise.all([
      loadTask(),
      loadHistory(),
      scenicApi.channels().then((list) => { channelOptions.value = list }),
    ])
  } finally {
    loading.value = false
  }

  socket = new TaskLogSocket(
    taskId,
    (log) => {
      // 服务端连上时会补推最近的缓存日志，按 时间+内容 去重避免和历史重复
      const key = `${log.log_time}|${log.message}`
      const exists = logs.value.some((l) => `${l.log_time}|${l.message}` === key)
      if (!exists) {
        logs.value.push(log as TaskLog)
        if (logs.value.length > 3000) logs.value.splice(0, 1000)
        scrollToBottom()
      }
    },
    (state) => { connected.value = state },
  )
  socket.open()

  // 详情页同时开着 WebSocket（推日志）和这个轮询（拉任务状态）。
  // 日志走 WS 已经是实时的，任务状态没必要也 5 秒一拉——
  // 那个接口每次都要把 keywords/targets/params/error 四个 TEXT 列读回来。
  // 任务跑完之后状态不会再变，就更没必要一直拉了。
  const tick = () => {
    // 编辑弹窗开着时不刷新：否则 task 被换掉，弹窗里正在改的内容会跟着跳
    if (!editVisible.value) loadTask()
    const running = task.value?.status === 'running' || task.value?.status === 'queued'
    timer = window.setTimeout(tick, running ? 5_000 : 30_000)
  }
  timer = window.setTimeout(tick, 5_000)
})

onUnmounted(() => {
  socket?.close()
  window.clearTimeout(timer)
})
</script>
