<template>
  <el-dialog
    v-model="visible" title="编辑任务" width="760px" destroy-on-close
    :close-on-click-modal="false" @open="onOpen"
  >
    <!-- 本轮已经开始的平台仍用启动时的配置，改动从下一轮开始生效——不需要重新建任务 -->
    <el-alert
      v-if="task?.is_running" type="warning" :closable="false" show-icon
      style="margin-bottom: 16px"
      title="任务运行中，改动从下一轮开始生效"
    />

    <el-form :model="form" label-width="110px">
      <el-form-item label="任务名称">
        <el-input v-model="form.task_name" />
      </el-form-item>

      <el-form-item label="采集平台">
        <!-- 单选，理由同新建任务页 -->
        <el-radio-group v-model="channel">
          <el-radio-button v-for="c in channelOptions" :key="c.value" :value="c.value">
            {{ c.label }}
            <span v-if="POI_CHANNELS.includes(c.value)" class="muted">（点评）</span>
          </el-radio-button>
        </el-radio-group>
        <!-- 改版前建的老任务可能选了多个平台，保存后只保留选中的那一个 -->
        <div v-if="form.channels.length > 1" class="legacy-warn">
          原任务选了 {{ form.channels.length }} 个平台，保存后只保留选中的一个
        </div>
      </el-form-item>

      <div class="form-section">运行模式</div>

      <el-form-item label="调度模式">
        <el-radio-group v-model="form.schedule_type" @change="previewSchedule">
          <el-radio-button value="once">立即执行一次</el-radio-button>
          <el-radio-button value="at">定时一次</el-radio-button>
          <el-radio-button value="interval">固定间隔</el-radio-button>
          <el-radio-button value="cron">cron 表达式</el-radio-button>
        </el-radio-group>
      </el-form-item>

      <el-form-item v-if="form.schedule_type === 'at'" label="执行时刻">
        <el-date-picker
          v-model="form.schedule_at" type="datetime" placeholder="选择日期时间"
          value-format="YYYY-MM-DDTHH:mm:ss" @change="previewSchedule"
        />
      </el-form-item>

      <el-form-item v-if="form.schedule_type === 'interval'" label="间隔">
        <el-input-number v-model="intervalValue" :min="1" style="width: 130px" @change="previewSchedule" />
        <el-select v-model="intervalUnit" style="width: 90px; margin-left: 8px" @change="previewSchedule">
          <el-option label="分钟" :value="60" />
          <el-option label="小时" :value="3600" />
          <el-option label="天" :value="86400" />
        </el-select>
      </el-form-item>

      <el-form-item v-if="form.schedule_type === 'cron'">
        <template #label>
          cron 表达式<InfoTip>
            5 段 = 分 时 日 月 周<br>
            6 段 = 秒 分 时 日 月 周（Quartz 风格，秒在最前）
          </InfoTip>
        </template>
        <el-input
          v-model="form.cron_expression" placeholder="0 2 * * *"
          class="mono" style="width: 240px" @input="previewSchedule"
        />
        <el-select
          placeholder="常用预设" style="width: 170px; margin-left: 8px"
          @change="(v: string) => { form.cron_expression = v; previewSchedule() }"
        >
          <el-option v-for="p in CRON_PRESETS" :key="p.value" :label="p.label" :value="p.value" />
        </el-select>
      </el-form-item>

      <el-form-item v-if="form.schedule_type !== 'once'">
        <template #label>
          启用定时<InfoTip content="关掉后任务保留，但不再自动触发" />
        </template>
        <el-switch v-model="form.schedule_enabled" />
      </el-form-item>

      <el-form-item v-if="nextRuns.length" label="下次执行">
        <el-tag v-for="(t, i) in nextRuns" :key="i" type="info" effect="plain" class="mono" style="margin: 0 6px 4px 0">
          {{ t }}
        </el-tag>
      </el-form-item>
      <el-form-item v-if="scheduleError" label=" ">
        <el-alert type="error" :closable="false" :title="scheduleError" />
      </el-form-item>
    </el-form>

    <div class="form-section">采集参数</div>
    <CollectParamsForm
      ref="paramsForm" v-model="params"
      :channels="form.channels" :initial="task?.params || null"
    />

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { taskApi, type ChannelOption, type Task } from '../api'
import { CRON_PRESETS, POI_CHANNELS } from '../constants'
import CollectParamsForm from './CollectParamsForm.vue'

const props = defineProps<{
  modelValue: boolean
  task: Task | null
  channelOptions: ChannelOption[]
}>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  saved: [Task]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const form = reactive({
  task_name: '',
  channels: [] as string[],
  schedule_type: 'once',
  schedule_at: '' as string | null,
  cron_expression: '',
  schedule_enabled: true,
})
/**
 * 平台单选的读写视图。form.channels 仍是数组（接口按数组收），
 * 改版前建的多平台老任务读进来会有多个值，这里显示第一个，
 * 保存时按单选的结果写回——模板里会提示用户这件事。
 */
const channel = computed<string>({
  get: () => form.channels[0] || '',
  set: (value: string) => { form.channels = value ? [value] : [] },
})

const intervalValue = ref(6)
const intervalUnit = ref(3600)
const params = ref<Record<string, unknown>>({})
const paramsForm = ref<InstanceType<typeof CollectParamsForm> | null>(null)
const nextRuns = ref<string[]>([])
const scheduleError = ref('')
const saving = ref(false)

/** 把秒数还原成「6 小时」这种好读的形式，而不是让用户看 21600 */
function splitInterval(seconds?: number | null) {
  const total = Number(seconds || 0)
  if (!total) return { value: 6, unit: 3600 }
  for (const unit of [86400, 3600, 60]) {
    if (total % unit === 0) return { value: total / unit, unit }
  }
  return { value: Math.max(1, Math.round(total / 60)), unit: 60 }
}

function onOpen() {
  const task = props.task
  if (!task) return
  form.task_name = task.task_name
  form.channels = [...(task.channels || [])]
  form.schedule_type = task.schedule_type
  form.schedule_at = task.schedule_at ? String(task.schedule_at).replace(' ', 'T').slice(0, 19) : ''
  form.cron_expression = task.cron_expression || ''
  form.schedule_enabled = Boolean(task.schedule_enabled)

  const interval = splitInterval(task.schedule_interval_seconds)
  intervalValue.value = interval.value
  intervalUnit.value = interval.unit

  // 采集数量部分由 CollectParamsForm 自己按 :initial 回填（destroy-on-close
  // 的弹窗里子组件挂载时机不好保证，交给它自己在 onMounted 里做更可靠）
  previewSchedule()
}

async function previewSchedule() {
  nextRuns.value = []
  scheduleError.value = ''
  if (form.schedule_type === 'once') {
    nextRuns.value = ['保存后按「立即执行」触发']
    return
  }
  if (form.schedule_type === 'cron' && !form.cron_expression.trim()) return
  if (form.schedule_type === 'at' && !form.schedule_at) return
  try {
    const result = await taskApi.previewSchedule({
      schedule_type: form.schedule_type,
      cron_expression: form.cron_expression,
      schedule_interval_seconds: intervalValue.value * intervalUnit.value,
      schedule_at: form.schedule_at || undefined,
      count: 3,
    })
    nextRuns.value = result.next_runs
  } catch (error) {
    scheduleError.value = (error as Error).message
  }
}

async function save() {
  if (!props.task) return
  if (!form.task_name.trim()) return ElMessage.warning('填一下任务名称')
  if (!form.channels.length) return ElMessage.warning('请选择采集平台')

  saving.value = true
  try {
    const payload: Record<string, unknown> = {
      task_name: form.task_name.trim(),
      channels: form.channels,
      params: paramsForm.value?.build() ?? params.value,
      schedule_type: form.schedule_type,
      schedule_enabled: form.schedule_type === 'once' ? false : form.schedule_enabled,
    }
    if (form.schedule_type === 'at') payload.schedule_at = form.schedule_at
    if (form.schedule_type === 'interval') {
      payload.schedule_interval_seconds = intervalValue.value * intervalUnit.value
    }
    if (form.schedule_type === 'cron') payload.cron_expression = form.cron_expression.trim()

    const updated = await taskApi.patch(props.task.task_id, payload)
    visible.value = false
    emit('saved', updated)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
/* 标签里的说明图标：label 是 flex 容器，图标默认会顶到上沿，这里让它和文字居中对齐 */
:deep(.el-form-item__label .info-tip) { align-self: center; }
.form-section {
  margin: 4px 0 16px;
  padding-top: 16px;
  border-top: 1px solid var(--smc-border);
  font-size: 13px;
  font-weight: 600;
  color: var(--smc-text);
}
.legacy-warn {
  width: 100%;
  margin-top: 6px;
  font-size: 12px;
  color: #a86a0c;
}
</style>
