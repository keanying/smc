<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!-- 运行记录：任务每执行一次一条。点一行进运行详情（这次的结局、采集量、参数、完整日志） -->
  <div>
    <div class="page-header">
      <h2 class="page-title">
        运行记录
        <InfoTip :width="340">
          任务每执行一次记一条：手动执行、定时到点、排队恢复、集群派发都算。<br>
          点一行看这次运行的详情——结局、采集量、当时按什么参数跑的、这次的完整日志。
        </InfoTip>
      </h2>
      <div class="header-actions">
        <!-- 勾选了才出现：批量删除（运行中的勾不上） -->
        <el-button v-if="selected.length" type="danger" plain :icon="Delete" :loading="deleting" @click="removeSelected">
          删除选中（{{ selected.length }}）
        </el-button>
        <el-button :icon="Refresh" @click="load()">刷新</el-button>
      </div>
    </div>

    <div class="toolbar">
      <el-input
        v-model="query.keyword" placeholder="任务名称 / 运行编号" clearable style="width: 220px"
        :prefix-icon="Search" @keyup.enter="reload" @clear="reload"
      />
      <el-select v-model="query.status" placeholder="状态" clearable style="width: 130px" @change="reload">
        <el-option v-for="o in statusOptions" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <el-select v-model="query.trigger" placeholder="触发方式" clearable style="width: 140px" @change="reload">
        <el-option v-for="o in triggerOptions" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <el-select v-model="query.channel" placeholder="平台" clearable style="width: 120px" @change="reload">
        <el-option v-for="(label, value) in CHANNEL_LABELS" :key="value" :label="label" :value="value" />
      </el-select>
      <el-select v-model="query.scenic_id" placeholder="目标" clearable filterable style="width: 180px" @change="reload">
        <el-option v-for="s in scenics" :key="s.scenic_id" :label="s.scenic_name" :value="s.scenic_id" />
      </el-select>
      <el-date-picker
        v-model="dateRange" type="daterange" value-format="YYYY-MM-DD" range-separator="至"
        start-placeholder="开始日期" end-placeholder="结束日期" class="run-range" @change="reload"
      />
      <el-tag v-if="query.task_id" closable class="task-filter" @close="clearTaskFilter">
        只看任务：{{ taskFilterName || query.task_id }}
      </el-tag>
      <el-switch v-model="autoRefresh" active-text="自动刷新" style="margin-left: auto" />
    </div>

    <el-table
      stripe :data="runs" v-loading="loading" class="data-table run-table" row-key="run_no"
      max-height="calc(var(--vh100) - 290px)" @row-click="onRowClick" @selection-change="onSelect"
    >
      <!-- 运行中的不能删（跑完要回写结局），勾不上 -->
      <el-table-column type="selection" width="44" :selectable="(row: TaskRun) => row.run_status !== 'running'" />
      <el-table-column label="开始时间" width="190">
        <template #default="{ row }">
          <div class="nowrap">{{ formatTime(row.start_time) }}</div>
          <div class="muted mono small">{{ row.run_no }}</div>
        </template>
      </el-table-column>
      <el-table-column label="任务" min-width="190">
        <template #default="{ row }">
          <span class="task-name">{{ row.task_name || row.task_id }}</span>
          <div class="muted small">{{ row.scenic_name || row.scenic_id || '—' }}</div>
        </template>
      </el-table-column>
      <el-table-column label="平台" width="112">
        <template #default="{ row }">
          <ChannelTag v-for="c in row.channels" :key="c" :channel="c" block class="ch-gap" />
        </template>
      </el-table-column>
      <el-table-column label="触发方式" width="110">
        <template #default="{ row }">
          <el-tooltip :disabled="!row.trigger_hint" :content="row.trigger_hint" placement="top" :show-after="200">
            <span class="trigger">{{ row.trigger_label || '—' }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="RUN_STATUS_TYPES[row.run_status] || 'info'" size="small" disable-transitions>
            <span v-if="row.run_status === 'running'" class="running-dot" />{{ row.run_status_label }}
          </el-tag>
          <!-- 排队期间反复重试都记在这一条上 -->
          <div v-if="row.wait_retry_qty" class="muted small nowrap">排队重试 {{ row.wait_retry_qty }} 次</div>
        </template>
      </el-table-column>
      <el-table-column label="耗时" width="110">
        <template #default="{ row }">{{ durationText(row.duration_seconds) }}</template>
      </el-table-column>
      <el-table-column label="采集量" width="200">
        <template #default="{ row }">
          <div class="nowrap">作品 <b>{{ row.new_work_qty }}</b><span class="muted"> 新增 · {{ row.updated_work_qty }} 更新</span></div>
          <div class="nowrap">评论 <b>{{ row.new_comment_qty }}</b><span class="muted"> 新增 · {{ row.updated_comment_qty }} 更新</span></div>
        </template>
      </el-table-column>
      <el-table-column label="说明" min-width="220">
        <template #default="{ row }">
          <span v-if="row.error_message" class="err-text" :title="row.error_message">{{ row.error_message }}</span>
          <span v-else-if="row.error_log_qty || row.warn_log_qty" class="warn-text">
            有 <template v-if="row.error_log_qty">{{ row.error_log_qty }} 个错误</template><template
              v-if="row.error_log_qty && row.warn_log_qty">、</template><template
              v-if="row.warn_log_qty">{{ row.warn_log_qty }} 个警告</template>，点开看日志
          </span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column v-if="showNode" label="机器" width="120" prop="node_id" />
      <el-table-column label="操作" width="96" align="center" fixed="right">
        <template #default="{ row }">
          <div class="row-actions" @click.stop>
            <IconAction icon="view" tip="运行详情" @click="openRun(row)" />
            <IconAction
              icon="delete" :tip="row.run_status === 'running' ? '还在运行，结束后才能删' : '删除'"
              :disabled="row.run_status === 'running'" @click="removeOne(row)"
            />
          </div>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="还没有运行记录：任务执行一次就会在这里出现一条" :image-size="80" />
      </template>
    </el-table>

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
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { Delete, Refresh, Search } from '@element-plus/icons-vue'
import { runApi, scenicApi, type RunDeleteResult, type ScenicOption, type TaskRun } from '../api'
import { CHANNEL_LABELS, formatTime } from '../constants'
import ChannelTag from '../components/ChannelTag.vue'
import { toast } from '../composables/toast'

/** 运行状态 → 标签颜色 */
const RUN_STATUS_TYPES: Record<string, 'success' | 'warning' | 'info' | 'danger' | 'primary'> = {
  running: 'primary', completed: 'success', failed: 'danger', canceled: 'info',
  waiting: 'warning', interrupted: 'warning',
}

const route = useRoute()
const router = useRouter()
const runs = ref<TaskRun[]>([])
const total = ref(0)
const loading = ref(false)
const autoRefresh = ref(true)
const scenics = ref<ScenicOption[]>([])
const statusOptions = ref<Array<{ value: string; label: string }>>([])
const triggerOptions = ref<Array<{ value: string; label: string }>>([])
const dateRange = ref<[string, string] | null>(null)
const query = reactive({
  keyword: '', status: '', trigger: '', channel: '', scenic_id: '',
  task_id: String(route.query.task_id || ''), page: 1, page_size: 20,
})
/** 从任务页带 task_id 进来时，标签上显示任务名 */
const taskFilterName = ref(String(route.query.task_name || ''))
/** 有多台机器跑过才显示「机器」列 */
const showNode = computed(() => new Set(runs.value.map((r) => r.node_id)).size > 1)

function durationText(seconds: number): string {
  const n = Math.max(0, Number(seconds) || 0)
  if (n < 60) return `${n} 秒`
  if (n < 3600) return `${Math.floor(n / 60)} 分 ${n % 60} 秒`
  return `${Math.floor(n / 3600)} 小时 ${Math.floor((n % 3600) / 60)} 分`
}

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const result = await runApi.list({
      ...query,
      start_date: dateRange.value?.[0] || '', end_date: dateRange.value?.[1] || '',
    }, silent)
    runs.value = result.items
    total.value = result.total
    statusOptions.value = result.status_options
    triggerOptions.value = result.trigger_options
  } finally {
    loading.value = false
  }
}

function reload() {
  query.page = 1
  load()
}

function clearTaskFilter() {
  query.task_id = ''
  taskFilterName.value = ''
  router.replace({ query: {} })
  reload()
}

function openRun(row: TaskRun) {
  router.push(`/runs/${row.run_no}`)
}

/** 点一行进详情；点的是勾选框那一格就只是勾选 */
function onRowClick(row: TaskRun, column: { type?: string }) {
  if (column?.type === 'selection') return
  openRun(row)
}

// ------------------------------------------------------------------ 删除
const selected = ref<TaskRun[]>([])
const deleting = ref(false)
function onSelect(rows: TaskRun[]) {
  selected.value = rows
}

async function removeRuns(runNos: string[], what: string) {
  try {
    await ElMessageBox.confirm(
      `${what}会一起删掉这些运行自己的日志（任务本身和采到的数据不受影响），删了找不回来。`,
      '删除运行记录', { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' })
  } catch {
    return
  }
  deleting.value = true
  try {
    const r: RunDeleteResult = await runApi.remove(runNos)
    const tail = [r.deleted_logs ? `连同 ${r.deleted_logs} 条日志` : '', r.skipped_running ? `${r.skipped_running} 条还在运行没删` : '']
      .filter(Boolean).join('，')
    if (r.skipped_running) toast.warning(`已删除 ${r.deleted} 条运行记录`, { description: tail })
    else toast.success(`已删除 ${r.deleted} 条运行记录`, tail ? { description: tail } : undefined)
    selected.value = []
    // 删完这一页空了就往前翻一页
    if (runs.value.length <= r.deleted && query.page > 1) query.page -= 1
    await load()
  } finally {
    deleting.value = false
  }
}

function removeOne(row: TaskRun) {
  return removeRuns([row.run_no], `删除运行 ${row.run_no}：`)
}

function removeSelected() {
  return removeRuns(selected.value.map((r) => r.run_no), `删除选中的 ${selected.value.length} 条运行记录：`)
}

// 有在跑的就 5 秒刷一次（静默，不闪加载圈）；全结束了就不刷
let timer: number | undefined
watch(autoRefresh, (on) => {
  window.clearInterval(timer)
  // 勾选着的时候不刷：刷新会把勾选清掉，正要删的时候勾全没了
  if (on) timer = window.setInterval(() => { if (!selected.value.length) load(true) }, 5000)
}, { immediate: true })
onBeforeUnmount(() => window.clearInterval(timer))

onMounted(async () => {
  const [options] = await Promise.all([scenicApi.options(false), load()])
  scenics.value = options
})
</script>

<style scoped>
.run-table :deep(.el-table__row) { cursor: pointer; }
.header-actions { display: flex; align-items: center; gap: 8px; }
.row-actions { display: inline-flex; align-items: center; gap: 2px; }
.trigger { white-space: nowrap; cursor: default; }
.nowrap { white-space: nowrap; }
.task-name { font-weight: 500; color: var(--el-color-primary); }
.small { font-size: 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.err-text {
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  color: var(--el-color-danger); font-size: 13px; line-height: 1.5;
}
.task-filter { margin-left: 4px; }
.warn-text { color: #d97706; font-size: 13px; }
/* 日期范围框别跟着工具栏拉满一整行 */
.toolbar :deep(.run-range.el-date-editor) { width: 260px; flex: none; }
.running-dot {
  display: inline-block; width: 6px; height: 6px; margin-right: 5px; border-radius: 50%;
  background: currentColor; vertical-align: 1px; animation: run-pulse 1.2s ease-in-out infinite;
}
@keyframes run-pulse { 50% { opacity: 0.25; } }
</style>
