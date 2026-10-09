<template>
  <el-dialog
    v-model="visible" width="820px" destroy-on-close
    :close-on-click-modal="false" @open="onOpen"
  >
    <template #header>
      <span class="el-dialog__title">试搜</span>
      <InfoTip
        content="用真实的采集链路（同一套登录态、代理、签名）跑一个关键字，只取一条就停，不写库。平台经常「不报错但也不给数据」，这里能看到请求到底发生了什么。"
      />
      <span class="dialog-sub">{{ channelLabel(channel) }}{{ accountName ? ` / ${accountName}` : '' }}</span>
    </template>

    <div class="toolbar">
      <el-input
        v-model="keyword" placeholder="输入一个关键字，如：天山天池"
        style="width: 240px" clearable @keyup.enter="run"
      />
      <el-input
        v-if="isPoiChannel" v-model="targetId"
        placeholder="POI_ID / sid" style="width: 160px" class="mono"
      />
      <el-button type="primary" :loading="running" :disabled="!canRun" @click="run">
        开始试搜
      </el-button>
    </div>

    <el-empty v-if="!report && !running" description="填个关键字，点「开始试搜」" :image-size="60" />

    <template v-if="report">
      <el-result
        :icon="report.ok ? 'success' : 'error'"
        :title="report.ok
          ? `通了，拿到 ${report.found} 条`
          : `没拿到数据（卡在：${report.stage}）`"
        :sub-title="`用时 ${report.elapsed_seconds}s`"
        class="probe-result"
      />

      <el-alert
        v-if="report.error" type="error" :closable="false" show-icon
        :title="report.error" style="margin-bottom: 12px"
      />

      <el-descriptions :column="2" size="small" border style="margin-bottom: 12px">
        <el-descriptions-item label="代理">
          <span v-if="report.proxy?.proxy_url" class="mono">{{ report.proxy.proxy_url }}</span>
          <span v-else class="muted">未启用</span>
        </el-descriptions-item>
        <el-descriptions-item label="设备指纹">
          {{ report.proxy?.fingerprint || '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="HTTP 状态">
          <el-tag v-if="report.http?.status" size="small"
                  :type="report.http.status === 200 ? 'success' : 'danger'">
            {{ report.http.status }}
          </el-tag>
          <span v-else class="muted">没发出请求</span>
        </el-descriptions-item>
        <el-descriptions-item label="响应大小">
          {{ report.http?.length != null ? `${report.http.length} 字符` : '-' }}
        </el-descriptions-item>
      </el-descriptions>

      <template v-if="report.sample">
        <div class="section-title">取到的第一条</div>
        <el-descriptions :column="1" size="small" border style="margin-bottom: 16px">
          <el-descriptions-item label="ID">
            <span class="mono">{{ report.sample.work_id }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="内容">{{ report.sample.title }}</el-descriptions-item>
          <el-descriptions-item label="作者">{{ report.sample.author }}</el-descriptions-item>
          <el-descriptions-item label="发布时间">{{ report.sample.publish_time }}</el-descriptions-item>
        </el-descriptions>
      </template>

      <div class="section-title">过程日志</div>
      <div class="probe-logs">
        <div v-for="(line, i) in report.logs" :key="i" :class="`log-${line.level}`">
          {{ line.message }}
        </div>
        <div v-if="!report.logs.length" class="muted">（没有日志）</div>
      </div>

      <el-collapse v-if="report.http?.snippet" class="probe-raw-collapse">
        <el-collapse-item title="原始响应片段（前 600 字符）">
          <pre class="probe-raw">{{ report.http.snippet }}</pre>
          <div class="muted" style="font-size: 12px">
            请求地址：<span class="mono">{{ report.http.url }}</span>
          </div>
        </el-collapse-item>
      </el-collapse>
    </template>

    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button v-if="report" @click="copyReport">复制报告</el-button>
      <el-button type="primary" :loading="running" :disabled="!canRun" @click="run">
        {{ report ? '再试一次' : '开始试搜' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { accountApi, type ProbeReport } from '../api'
import { POI_CHANNELS, channelLabel } from '../constants'

const props = defineProps<{
  modelValue: boolean
  channel: string
  accountName: string
}>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const keyword = ref('')
const targetId = ref('')
const report = ref<ProbeReport | null>(null)
const running = ref(false)

const isPoiChannel = computed(() => POI_CHANNELS.includes(props.channel))
const canRun = computed(
  () => !!keyword.value.trim() && (!isPoiChannel.value || !!targetId.value.trim()),
)

function onOpen() {
  report.value = null
}

async function run() {
  running.value = true
  try {
    report.value = await accountApi.probe(
      props.channel, props.accountName, keyword.value.trim(), targetId.value.trim(),
    )
  } catch (error) {
    // 后端已经把错误弹出来了，这里只把报告清掉避免显示旧结果
    report.value = null
  } finally {
    running.value = false
  }
}

/** 出问题时把整份报告贴给人看，比截图省事 */
async function copyReport() {
  if (!report.value) return
  const text = JSON.stringify(report.value, null, 2)
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('报告已复制到剪贴板')
  } catch {
    ElMessage.warning('浏览器不允许自动复制，请手动选中下面的内容')
  }
}
</script>

<style scoped>
.dialog-sub {
  margin-left: 10px;
  font-size: 13px;
  color: var(--smc-text-secondary);
}
.probe-result { padding: 4px 0 12px; }
.probe-result :deep(.el-result__icon svg) { width: 40px; height: 40px; }
.probe-result :deep(.el-result__title) { margin-top: 10px; }
.probe-result :deep(.el-result__title p) { font-size: 16px; font-weight: 600; }
.probe-raw-collapse { margin-top: 16px; }
/* 这里是白底卡片，不能复用日志终端那套深色配色 */
.probe-logs :deep(.log-warn) { color: #e6a23c; }
.probe-logs :deep(.log-error) { color: #f56c6c; }

.probe-logs {
  padding: 10px 12px;
  border: 1px solid var(--smc-border);
  border-radius: var(--smc-radius-sm);
  background: #fafbfc;
  max-height: 220px;
  overflow-y: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.8;
}

.probe-raw {
  max-height: 240px;
  overflow: auto;
  background: #fafbfc;
  padding: 10px 12px;
  border-radius: var(--smc-radius-sm);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0 0 8px;
}
</style>
