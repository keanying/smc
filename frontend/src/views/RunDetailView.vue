<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!-- 运行详情：一次执行的结局、采集量、当时的参数、这次的完整日志。运行中会自动追加日志 -->
  <div v-loading="loading">
    <div class="page-header">
      <h2 class="page-title">
        运行详情
        <span v-if="run" class="title-sub mono">{{ run.run_no }}</span>
      </h2>
      <div>
        <el-button v-if="run?.task_exists" @click="$router.push(`/tasks/${run.task_id}`)">去任务</el-button>
        <el-button @click="$router.push(backTo)">返回运行记录</el-button>
        <el-button
          v-if="run && run.run_status !== 'running'" type="danger" plain :loading="deleting" @click="removeRun"
        >删除</el-button>
      </div>
    </div>

    <template v-if="run">
      <el-alert
        v-if="run.error_message" :type="run.run_status === 'waiting' ? 'warning' : 'error'"
        :closable="false" show-icon :title="statusHint" :description="run.error_message" class="run-alert"
      />

      <!-- 完成了但日志里有错误：比如某个平台没账号被跳过——状态是「完成」，其实这个平台没采 -->
      <el-alert
        v-else-if="run.error_log_qty" type="warning" :closable="false" show-icon class="run-alert"
        :title="`这次运行完成了，但有 ${run.error_log_qty} 条错误日志${run.warn_log_qty ? `、${run.warn_log_qty} 条警告` : ''}`"
      >
        <el-button size="small" text type="warning" @click="onlyProblems = true">只看警告和错误</el-button>
      </el-alert>

      <div class="run-grid">
        <el-card shadow="never" header="这次运行">
          <el-descriptions :column="2" border size="small" class="run-desc">
            <el-descriptions-item label="状态">
              <el-tag :type="RUN_STATUS_TYPES[run.run_status] || 'info'" size="small" disable-transitions>
                <span v-if="run.run_status === 'running'" class="running-dot" />{{ run.run_status_label }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="触发方式">
              {{ run.trigger_label || '—' }}<span v-if="run.trigger_hint" class="muted small">（{{ run.trigger_hint }}）</span>
            </el-descriptions-item>
            <el-descriptions-item label="任务">{{ run.task_name || run.task_id }}</el-descriptions-item>
            <el-descriptions-item label="目标">{{ run.scenic_name || run.scenic_id || '—' }}</el-descriptions-item>
            <el-descriptions-item label="平台">
              <ChannelTag v-for="c in run.channels" :key="c" :channel="c" class="ch-gap" />
            </el-descriptions-item>
            <el-descriptions-item label="机器">{{ run.node_id || '—' }}</el-descriptions-item>
            <el-descriptions-item label="开始">{{ formatTime(run.start_time) }}</el-descriptions-item>
            <el-descriptions-item label="结束">{{ run.end_time ? formatTime(run.end_time) : '运行中…' }}</el-descriptions-item>
            <el-descriptions-item label="耗时" :span="2">{{ durationText(run.duration_seconds) }}</el-descriptions-item>
          </el-descriptions>
        </el-card>

        <el-card shadow="never" header="采集量">
          <div class="stat-row">
            <div class="stat-box"><div class="stat-num">{{ run.new_work_qty }}</div><div class="muted">新增作品</div></div>
            <div class="stat-box"><div class="stat-num">{{ run.updated_work_qty }}</div><div class="muted">更新作品</div></div>
            <div class="stat-box"><div class="stat-num">{{ run.new_comment_qty }}</div><div class="muted">新增评论</div></div>
            <div class="stat-box"><div class="stat-num">{{ run.updated_comment_qty }}</div><div class="muted">更新评论</div></div>
          </div>
          <el-collapse v-if="run.params_snapshot" class="params-collapse">
            <el-collapse-item title="这次按什么参数跑的（运行当时的任务参数）" name="p">
              <pre class="params-pre">{{ JSON.stringify(run.params_snapshot, null, 2) }}</pre>
            </el-collapse-item>
          </el-collapse>
        </el-card>
      </div>

      <el-card shadow="never" class="log-card">
        <template #header>
          <div class="log-head">
            <span>这次运行的日志 <span class="muted small">（{{ logs.length }} 条）</span></span>
            <el-checkbox v-model="onlyProblems" size="small">只看警告和错误</el-checkbox>
          </div>
        </template>
        <div ref="terminal" class="log-terminal">
          <div v-if="!shownLogs.length" class="muted">
            {{ logs.length ? '没有警告和错误' : (run.run_status === 'running' ? '等待日志…' : '这次运行没有日志') }}
          </div>
          <div v-for="log in shownLogs" :key="log.id" class="log-line">
            <span class="log-time">{{ String(log.log_time).slice(5, 19).replace('T', ' ') }}</span>
            <span v-if="log.channel" class="log-channel">[{{ channelLabel(log.channel) }}]</span>
            <span :class="`log-${log.level}`">{{ log.message }}</span>
          </div>
        </div>
      </el-card>
    </template>
    <el-empty v-else-if="!loading" description="运行记录不存在" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { runApi, type TaskLog, type TaskRunDetail } from '../api'
import { channelLabel, formatTime } from '../constants'
import ChannelTag from '../components/ChannelTag.vue'
import { toast } from '../composables/toast'

const RUN_STATUS_TYPES: Record<string, 'success' | 'warning' | 'info' | 'danger' | 'primary'> = {
  running: 'primary', completed: 'success', failed: 'danger', canceled: 'info',
  waiting: 'warning', interrupted: 'warning',
}

const route = useRoute()
const router = useRouter()
const deleting = ref(false)
const runNo = String(route.params.runNo || '')
const run = ref<TaskRunDetail | null>(null)
const logs = ref<TaskLog[]>([])
const loading = ref(true)
const onlyProblems = ref(false)
const terminal = ref<HTMLElement | null>(null)
/** 返回时带上从哪个任务过来的筛选 */
const backTo = computed(() => (route.query.from === 'task' && run.value
  ? { path: '/runs', query: { task_id: run.value.task_id, task_name: run.value.task_name } } : '/runs'))

const shownLogs = computed(() => (onlyProblems.value
  ? logs.value.filter((l) => ['WARN', 'WARNING', 'ERROR'].includes(String(l.level).toUpperCase()))
  : logs.value))

const statusHint = computed(() => ({
  failed: '这次运行失败了', canceled: '这次运行被取消', waiting: '这次没跑完：在排队等账号，到点自动再跑一次',
  interrupted: '这次运行中途中断（运行中服务重启）',
} as Record<string, string>)[run.value?.run_status || ''] || '说明')

function durationText(seconds: number): string {
  const n = Math.max(0, Number(seconds) || 0)
  if (n < 60) return `${n} 秒`
  if (n < 3600) return `${Math.floor(n / 60)} 分 ${n % 60} 秒`
  return `${Math.floor(n / 3600)} 小时 ${Math.floor((n % 3600) / 60)} 分`
}

/** 拉日志：一次最多 2000 条，多了接着翻；只往后追加 */
async function loadLogs() {
  const atBottom = !terminal.value
    || terminal.value.scrollHeight - terminal.value.scrollTop - terminal.value.clientHeight < 40
  for (let i = 0; i < 20; i += 1) {
    const after = logs.value.length ? logs.value[logs.value.length - 1].id : 0
    const page = await runApi.logs(runNo, after, 2000)
    if (!page.items.length) break
    logs.value.push(...page.items)
    if (page.items.length < 2000) break
  }
  // 原来就在底部的话跟着滚到底（像终端）；往上翻着看的不打扰
  if (atBottom) {
    await nextTick()
    terminal.value?.scrollTo({ top: terminal.value.scrollHeight })
  }
}

async function removeRun() {
  if (!run.value) return
  try {
    await ElMessageBox.confirm(
      '会一起删掉这次运行自己的日志（任务本身和采到的数据不受影响），删了找不回来。',
      '删除这条运行记录', { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' })
  } catch {
    return
  }
  deleting.value = true
  try {
    const r = await runApi.remove([run.value.run_no])
    toast.success('已删除这条运行记录', r.deleted_logs ? { description: `连同 ${r.deleted_logs} 条日志` } : undefined)
    router.replace(backTo.value)
  } finally {
    deleting.value = false
  }
}

async function refresh(silent = false) {
  run.value = await runApi.detail(runNo, silent)
  await loadLogs()
}

let timer: number | undefined
onMounted(async () => {
  try {
    await refresh()
  } finally {
    loading.value = false
  }
  // 还在跑：3 秒刷一次状态和新日志，跑完就停
  timer = window.setInterval(async () => {
    if (run.value?.run_status !== 'running') {
      window.clearInterval(timer)
      return
    }
    await refresh(true)
  }, 3000)
})
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<style scoped>
.title-sub { margin-left: 10px; font-size: 13px; font-weight: normal; color: var(--smc-text-secondary); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.small { font-size: 12px; }
.run-alert { margin-bottom: 16px; }
.run-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 520px), 1fr));
  gap: 16px;
  align-items: start;
  margin-bottom: 16px;
}
.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.stat-box {
  padding: 14px 12px; border-radius: 10px; background: var(--el-fill-color-light); text-align: center;
}
.stat-num { font-size: 24px; font-weight: 700; color: var(--smc-text); line-height: 1.2; margin-bottom: 4px; }
.params-collapse { margin-top: 14px; border-top: none; }
.params-pre {
  margin: 0; max-height: 320px; overflow: auto; font-size: 12px; line-height: 1.6;
  background: var(--el-fill-color-lighter); padding: 10px 12px; border-radius: 8px;
}
.log-head { display: flex; align-items: center; justify-content: space-between; }
.log-card .log-terminal { max-height: calc(var(--vh100) - 420px); min-height: 240px; }
.running-dot {
  display: inline-block; width: 6px; height: 6px; margin-right: 5px; border-radius: 50%;
  background: currentColor; vertical-align: 1px; animation: run-pulse 1.2s ease-in-out infinite;
}
@keyframes run-pulse { 50% { opacity: 0.25; } }
</style>
