<template>
  <div>
    <div class="page-header">
      <div>
        <h2 class="page-title">任务管理</h2>
        <p class="page-subtitle">支持立即执行、定时一次、固定间隔、cron 四种调度</p>
      </div>
      <el-button type="primary" :icon="Plus" @click="$router.push('/tasks/new')">新建任务</el-button>
    </div>

    <div class="toolbar">
      <el-input
        v-model="query.keyword" placeholder="搜索任务名称" clearable style="width: 220px"
        :prefix-icon="Search" @keyup.enter="load" @clear="load"
      />
      <el-select v-model="query.status" placeholder="状态" clearable style="width: 130px" @change="load">
        <el-option v-for="(label, value) in TASK_STATUS_LABELS" :key="value" :label="label" :value="value" />
      </el-select>
      <el-select
        v-model="query.scenic_id" placeholder="景区" clearable filterable
        style="width: 200px" @change="load"
      >
        <el-option
          v-for="s in scenics" :key="s.scenic_id"
          :label="s.scenic_name" :value="s.scenic_id"
        />
      </el-select>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
      <el-switch v-model="autoRefresh" active-text="自动刷新" style="margin-left: auto" />
    </div>

    <el-table :data="tasks" v-loading="loading" border stripe>
      <el-table-column prop="task_name" label="任务名称" min-width="180">
        <template #default="{ row }">
          <el-link type="primary" @click="$router.push(`/tasks/${row.task_id}`)">
            {{ row.task_name }}
          </el-link>
        </template>
      </el-table-column>
      <el-table-column label="景区" width="140">
        <template #default="{ row }">
          <span>{{ scenicName(row.scenic_id) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="平台" min-width="180">
        <template #default="{ row }">
          <el-tag
            v-for="c in row.channels" :key="c" size="small"
            :color="CHANNEL_COLORS[c]" style="color: #fff; border: none; margin-right: 4px"
          >
            {{ channelLabel(c) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="调度" width="150">
        <template #default="{ row }">
          <div>{{ SCHEDULE_LABELS[row.schedule_type] || row.schedule_type }}</div>
          <div v-if="row.cron_expression" class="mono muted" style="font-size: 12px">
            {{ row.cron_expression }}
          </div>
          <div v-if="row.next_run_time" class="muted" style="font-size: 12px">
            下次 {{ formatTime(row.next_run_time) }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="110" align="center">
        <template #default="{ row }">
          <el-tag :type="TASK_STATUS_TYPES[row.status]" size="small">
            {{ TASK_STATUS_LABELS[row.status] || row.status }}
          </el-tag>
          <el-progress
            v-if="row.status === 'running'" :percentage="row.progress || 0"
            :stroke-width="4" :show-text="false" style="margin-top: 6px"
          />
        </template>
      </el-table-column>
      <el-table-column label="采集量" width="150">
        <template #default="{ row }">
          <div class="muted" style="font-size: 12px">
            作品 {{ row.stat_new_works }}<span v-if="row.stat_updated_works">/{{ row.stat_updated_works }}</span>
          </div>
          <div class="muted" style="font-size: 12px">
            评论 {{ row.stat_new_comments }}<span v-if="row.stat_updated_comments">/{{ row.stat_updated_comments }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="采集上限 / 搜索条件" width="280">
        <template #default="{ row }">
          <div v-for="limit in row.collect_limits || []" :key="limit.channel"
               class="muted" style="font-size: 12px">
            {{ channelLabel(limit.channel) }}
            <template v-if="limit.has_works">
              作品 {{ limit.max_works || '不限' }} · 评论 {{ limit.max_comments || '不限' }}
            </template>
            <template v-else>
              点评 {{ limit.max_comments || '不限' }} · {{ limit.max_pages || '不限' }} 页
            </template>
            <!-- 采集模式只对有拟人采集器的平台有意义 -->
            <el-tag
              v-if="limit.supports_browser_engine" size="small" effect="plain"
              :type="limit.collect_engine === 'human' ? 'warning' : 'info'"
              style="margin-left: 4px"
            >{{ engineLabel(limit.collect_engine) }}</el-tag>
          </div>
          <div v-for="f in row.search_filters || []" :key="`f-${f.channel}`"
               class="muted" style="font-size: 12px">
            {{ channelLabel(f.channel) }} {{ f.description }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="290" align="center">
        <template #default="{ row }">
          <el-button
            v-if="row.status !== 'running'" link type="primary"
            :icon="VideoPlay" @click="run(row)"
          >执行</el-button>
          <el-button v-else link type="warning" :icon="VideoPause" @click="cancel(row)">取消</el-button>

          <el-button link type="primary" :icon="Edit" @click="edit(row)">编辑</el-button>

          <el-button
            v-if="row.schedule_type !== 'once'" link
            :type="row.schedule_enabled ? 'info' : 'success'"
            @click="toggleSchedule(row)"
          >{{ row.schedule_enabled ? '暂停定时' : '启用定时' }}</el-button>

          <el-popconfirm title="删除任务及其日志？已采集的数据保留。" @confirm="remove(row)">
            <template #reference><el-button link type="danger">删除</el-button></template>
          </el-popconfirm>
        </template>
      </el-table-column>
      <template #empty><el-empty description="还没有任务" /></template>
    </el-table>

    <TaskEditDialog
      v-model="editVisible" :task="editing" :channel-options="channelOptions"
      @saved="onSaved"
    />

    <el-pagination
      v-if="total > query.page_size"
      style="margin-top: 16px; justify-content: flex-end"
      layout="total, prev, pager, next" :total="total"
      :page-size="query.page_size" :current-page="query.page"
      @current-change="(p: number) => { query.page = p; load() }"
    />
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Edit, Plus, Refresh, Search, VideoPause, VideoPlay } from '@element-plus/icons-vue'
import { scenicApi, taskApi, type ChannelOption, type ScenicOption, type Task } from '../api'
import { COLLECT_ENGINES, normalizeEngine } from '../constants'
import TaskEditDialog from '../components/TaskEditDialog.vue'
import {
  CHANNEL_COLORS, SCHEDULE_LABELS, TASK_STATUS_LABELS, TASK_STATUS_TYPES,
  channelLabel, formatTime,
} from '../constants'

const tasks = ref<Task[]>([])
const scenics = ref<ScenicOption[]>([])
const total = ref(0)
const loading = ref(false)
const autoRefresh = ref(true)

function engineLabel(value: unknown): string {
  const engine = normalizeEngine(value)
  return COLLECT_ENGINES.find((item) => item.value === engine)?.label || engine
}
const query = reactive({ keyword: '', status: '', scenic_id: '', page: 1, page_size: 20 })
const channelOptions = ref<ChannelOption[]>([])
const editVisible = ref(false)
const editing = ref<Task | null>(null)

let timer: number | undefined

function scenicName(scenicId?: string | null): string {
  if (!scenicId) return '-'
  return scenics.value.find((s) => s.scenic_id === scenicId)?.scenic_name || scenicId
}

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const result = await taskApi.list(query)
    tasks.value = result.items
    total.value = result.total
  } finally {
    loading.value = false
  }
}

async function run(row: Task) {
  await taskApi.run(row.task_id)
  ElMessage.success('已开始执行')
  await load(true)
}

async function cancel(row: Task) {
  await taskApi.cancel(row.task_id)
  ElMessage.success('已发送取消信号')
  await load(true)
}

function edit(row: Task) {
  editing.value = row
  editVisible.value = true
}

async function onSaved(updated: Task) {
  ElMessage.success(
    updated.is_running
      ? '已保存，任务正在运行，改动从下一轮开始生效'
      : '已保存',
  )
  await load(true)
}

async function toggleSchedule(row: Task) {
  await taskApi.toggleSchedule(row.task_id, !row.schedule_enabled)
  await load(true)
}

async function remove(row: Task) {
  await taskApi.remove(row.task_id)
  ElMessage.success('已删除')
  await load()
}

/**
 * 自动刷新的节奏：**有任务在跑才快**。
 *
 * 原来是固定 5 秒一次。可这个接口每次都要 COUNT + 拉 20 行（含四个 TEXT 列），
 * 还要给每行算一遍数量摘要和筛选摘要——页面开着不动也一直在打。
 * 大多数时候列表里根本没有 running 的任务，那种情况下 5 秒和 30 秒
 * 对用户完全没区别，所以按状态分档。
 */
function pollInterval(): number {
  const busy = tasks.value.some(
    (t) => t.status === 'running' || t.status === 'queued' || t.is_running,
  )
  return busy ? 5_000 : 30_000
}

function setupTimer() {
  window.clearTimeout(timer)
  if (!autoRefresh.value || editVisible.value) return
  // 用 setTimeout 递归而不是 setInterval：每一轮都要重新判断该用哪个节奏，
  // 任务一跑起来就自动切回 5 秒。
  const tick = async () => {
    await load(true)
    if (!autoRefresh.value || editVisible.value) return
    timer = window.setTimeout(tick, pollInterval())
  }
  timer = window.setTimeout(tick, pollInterval())
}

// 编辑弹窗开着的时候停掉自动刷新，避免用户正在改、底下数据被换掉
watch([autoRefresh, editVisible], setupTimer)

onMounted(async () => {
  // 下拉框走精简接口；任务列表不等它，并行拉
  const [scenicOptions, channelList] = await Promise.all([
    scenicApi.options(false),
    scenicApi.channels(),
    load(),
  ])
  scenics.value = scenicOptions
  channelOptions.value = channelList
  setupTimer()
})
onUnmounted(() => window.clearTimeout(timer))
</script>
