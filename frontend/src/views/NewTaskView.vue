<template>
  <div>
    <div class="page-header">
      <h2 class="page-title">
        新建采集任务
        <InfoTip content="选景区 → 选平台 → 带入关键字或 POI → 设定执行方式" />
      </h2>
      <el-button @click="$router.back()">返回</el-button>
    </div>

    <el-row :gutter="16">
      <el-col :span="15">
        <el-card shadow="never">
          <el-form :model="form" label-width="110px">
            <el-form-item label="任务名称" required>
              <el-input v-model="form.task_name" placeholder="如：西湖-全平台-每日采集" />
            </el-form-item>

            <el-form-item required>
              <template #label>
                所属景区<InfoTip content="采到的每条数据都会带上该景区的 ID 和名称" />
              </template>
              <el-select
                v-model="form.scenic_id" filterable placeholder="选择景区"
                style="width: 100%" @change="onScenicChange"
              >
                <el-option
                  v-for="s in scenics" :key="s.scenic_id"
                  :label="`${s.scenic_name}（${s.scenic_id}）`" :value="s.scenic_id"
                />
              </el-select>
            </el-form-item>

            <el-form-item required>
              <template #label>
                采集平台<InfoTip content="一个任务一个平台。采多个平台就建多个任务，互不影响、可分别重跑。" />
              </template>
              <!-- 一个任务只跑一个平台：不同平台的采集方式、账号占用、
                   排队规则都不一样，混在一个任务里没法单独重跑，也说不清是哪个平台失败 -->
              <el-radio-group v-model="channel" @change="onChannelsChange">
                <el-radio-button v-for="c in channels" :key="c.value" :value="c.value">
                  {{ c.label }}
                  <span v-if="POI_CHANNELS.includes(c.value)" class="muted">（点评）</span>
                </el-radio-button>
              </el-radio-group>
            </el-form-item>

            <el-form-item>
              <template #label>
                采集方式<InfoTip v-if="hasPoiChannel" content="携程/同程固定走景区 POI 点评采集，不受此项影响" />
              </template>
              <el-radio-group v-model="form.collect_type">
                <el-radio-button value="keyword">关键字搜索</el-radio-button>
                <el-radio-button value="creator">指定用户主页</el-radio-button>
              </el-radio-group>
            </el-form-item>

            <!-- 关键字 -->
            <el-form-item v-if="form.collect_type === 'keyword'">
              <template #label>
                搜索关键字<InfoTip content="留空则执行时自动带入该景区所有启用的关键字" />
              </template>
              <div style="width: 100%">
                <div class="toolbar" style="margin-bottom: 8px">
                  <el-button
                    type="primary" plain :icon="Download"
                    :disabled="!form.scenic_id" :loading="loadingKeywords"
                    @click="pullKeywords"
                  >
                    从景区带入（最多 {{ keywordLimit }} 个）
                  </el-button>
                  <span class="muted" style="font-size: 12px">
                    已选 {{ form.keywords.length }} 个
                  </span>
                  <el-button v-if="form.keywords.length" link type="danger" @click="form.keywords = []">
                    清空
                  </el-button>
                </div>
                <el-select
                  v-model="form.keywords" multiple filterable allow-create
                  default-first-option placeholder="从景区带入，或直接输入后回车新增"
                  style="width: 100%" :max-collapse-tags="12" collapse-tags collapse-tags-tooltip
                >
                  <el-option v-for="k in scenicKeywords" :key="k" :label="k" :value="k" />
                </el-select>
              </div>
            </el-form-item>

            <!-- 主页目标 -->
            <el-form-item v-if="form.collect_type === 'creator'">
              <template #label>
                主页目标<InfoTip content="执行时自动使用该景区配置的主页目标，无需在此选择" />
              </template>
              <div style="width: 100%">
                <div v-if="!creatorTargets.length" class="form-warn">
                  该景区还没配置主页目标，请先到「景区管理 → 采集目标」添加
                </div>
                <el-table v-else :data="creatorTargets" size="small" border>
                  <el-table-column label="平台" width="90">
                    <template #default="{ row }">{{ channelLabel(row.channel) }}</template>
                  </el-table-column>
                  <el-table-column prop="target_id" label="用户ID" min-width="200" class-name="mono" />
                  <el-table-column prop="target_name" label="备注" width="140" />
                </el-table>
              </div>
            </el-form-item>

            <!-- POI 目标提示 -->
            <el-form-item v-if="hasPoiChannel" label="POI 目标">
              <div style="width: 100%">
                <el-table v-if="poiTargets.length" :data="poiTargets" size="small" border>
                  <el-table-column label="平台" width="90">
                    <template #default="{ row }">{{ channelLabel(row.channel) }}</template>
                  </el-table-column>
                  <el-table-column prop="target_id" label="POI_ID / sid" min-width="160" class-name="mono" />
                  <el-table-column prop="target_name" label="备注" width="140" />
                </el-table>
                <div v-else class="form-error">
                  该景区没配 POI_ID / sid，任务会被拒绝创建。请先到「景区管理 → 采集目标」添加
                </div>
              </div>
            </el-form-item>

            <el-form-item>
              <template #label>
                账号分组<InfoTip>
                  多账号轮换采集：选了分组只用该组账号；不选则在该平台全部可用账号里轮换。<br>
                  分组在「账号管理」里维护。
                </InfoTip>
              </template>
              <el-select
                v-model="form.account_group" clearable filterable
                placeholder="不选 = 该平台全部可用账号轮换" style="width: 240px"
              >
                <el-option v-for="g in accountGroups" :key="g.name" :label="g.name" :value="g.name">
                  <span>{{ g.name }}</span>
                  <span class="muted" style="float: right; font-size: 12px">
                    {{ g.active }}/{{ g.total }} 可用
                  </span>
                </el-option>
              </el-select>
            </el-form-item>

            <div class="form-section">执行方式</div>

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
              <el-input-number
                v-model="intervalValue" :min="1" style="width: 130px" @change="previewSchedule"
              />
              <el-select v-model="intervalUnit" style="width: 90px; margin-left: 8px" @change="previewSchedule">
                <el-option label="分钟" :value="60" />
                <el-option label="小时" :value="3600" />
                <el-option label="天" :value="86400" />
              </el-select>
            </el-form-item>

            <template v-if="form.schedule_type === 'cron'">
              <el-form-item>
                <template #label>
                  cron 表达式<InfoTip>
                    5 段 = 分 时 日 月 周<br>
                    6 段 = 秒 分 时 日 月 周（Quartz 风格，秒在最前）
                  </InfoTip>
                </template>
                <el-input
                  v-model="form.cron_expression" placeholder="0 2 * * *"
                  class="mono" style="width: 260px" @input="previewSchedule"
                />
                <el-select
                  placeholder="常用预设" style="width: 180px; margin-left: 8px"
                  @change="(v: string) => { form.cron_expression = v; previewSchedule() }"
                >
                  <el-option v-for="p in CRON_PRESETS" :key="p.value" :label="p.label" :value="p.value" />
                </el-select>
              </el-form-item>
            </template>

            <el-form-item v-if="nextRuns.length" label="下次执行">
              <div>
                <el-tag v-for="(t, i) in nextRuns" :key="i" type="info" effect="plain" class="mono" style="margin: 0 6px 4px 0">
                  {{ t }}
                </el-tag>
              </div>
            </el-form-item>
            <el-form-item v-if="scheduleError" label=" ">
              <el-alert type="error" :closable="false" :title="scheduleError" />
            </el-form-item>
          </el-form>
        </el-card>
      </el-col>

      <el-col :span="9">
        <el-card shadow="never" header="采集参数">
          <CollectParamsForm v-model="params" :channels="form.channels" />
        </el-card>

        <el-card shadow="never" style="margin-top: 16px">
          <el-button
            type="primary" size="large" style="width: 100%"
            :loading="creating" @click="submit"
          >
            {{ form.schedule_type === 'once' ? '创建并立即执行' : '创建任务' }}
          </el-button>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Download } from '@element-plus/icons-vue'
import {
  accountApi, scenicApi, settingsApi, taskApi,
  type AccountGroup, type ChannelOption, type ScenicOption, type Target,
} from '../api'
import { CRON_PRESETS, POI_CHANNELS, channelLabel } from '../constants'
import CollectParamsForm from '../components/CollectParamsForm.vue'

const route = useRoute()
const router = useRouter()

const scenics = ref<ScenicOption[]>([])
const channels = ref<ChannelOption[]>([])
const scenicKeywords = ref<string[]>([])
const allTargets = ref<Target[]>([])
const keywordLimit = ref(100)
const loadingKeywords = ref(false)
const creating = ref(false)
const nextRuns = ref<string[]>([])
const scheduleError = ref('')

const form = reactive({
  task_name: '',
  scenic_id: '',
  channels: [] as string[],
  collect_type: 'keyword',
  keywords: [] as string[],
  account_group: '',
  schedule_type: 'once',
  schedule_at: '' as string | null,
  cron_expression: '',
})

const accountGroups = ref<AccountGroup[]>([])

const intervalValue = ref(6)
const intervalUnit = ref(3600)

// 具体结构由 CollectParamsForm 组装（含 channel_params 平台级覆盖）
const params = ref<Record<string, unknown>>({})

/**
 * 平台单选。
 * 表单里仍然存数组（后端接口、CollectParamsForm、目标过滤全按数组写的），
 * 这里只是给单选控件包一层读写视图，避免为了改个控件把整条链路都动一遍。
 */
const channel = computed<string>({
  get: () => form.channels[0] || '',
  set: (value: string) => { form.channels = value ? [value] : [] },
})

const hasPoiChannel = computed(() => form.channels.some((c) => POI_CHANNELS.includes(c)))
const poiTargets = computed(() =>
  allTargets.value.filter((t) => t.target_type === 'poi' && form.channels.includes(t.channel)),
)
const creatorTargets = computed(() =>
  allTargets.value.filter((t) => t.target_type === 'creator' && form.channels.includes(t.channel)),
)

async function onScenicChange() {
  scenicKeywords.value = []
  allTargets.value = []
  form.keywords = []
  if (!form.scenic_id) return
  const [keywords, targets] = await Promise.all([
    scenicApi.keywords(form.scenic_id, { enabled_only: true }),
    scenicApi.targets(form.scenic_id),
  ])
  scenicKeywords.value = keywords.map((k) => k.keyword)
  allTargets.value = targets

  const scenic = scenics.value.find((s) => s.scenic_id === form.scenic_id)
  if (scenic && !form.task_name) {
    form.task_name = `${scenic.scenic_name}-采集`
  }
}

function onChannelsChange() {
  // 只选了携程/同程时，采集方式固定为 POI，切到关键字没有意义
  if (form.channels.length && form.channels.every((c) => POI_CHANNELS.includes(c))) {
    form.collect_type = 'poi'
  } else if (form.collect_type === 'poi') {
    form.collect_type = 'keyword'
  }
}

async function pullKeywords() {
  if (!form.scenic_id) return
  loadingKeywords.value = true
  try {
    const rows = await scenicApi.keywords(form.scenic_id, {
      enabled_only: true, limit: keywordLimit.value,
    })
    if (!rows.length) {
      ElMessage.warning('该景区还没有启用的关键字，先去景区管理里添加')
      return
    }
    form.keywords = rows.map((r) => r.keyword)
    ElMessage.success(`已带入 ${rows.length} 个关键字`)
  } finally {
    loadingKeywords.value = false
  }
}

async function previewSchedule() {
  nextRuns.value = []
  scheduleError.value = ''
  if (form.schedule_type === 'once') {
    nextRuns.value = ['创建后立即执行']
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
      count: 5,
    })
    nextRuns.value = result.next_runs
  } catch (error) {
    scheduleError.value = (error as Error).message
  }
}

async function submit() {
  if (!form.task_name.trim()) return ElMessage.warning('填一下任务名称')
  if (!form.scenic_id) return ElMessage.warning('选一个景区')
  if (!form.channels.length) return ElMessage.warning('请选择采集平台')

  creating.value = true
  try {
    const payload: Record<string, unknown> = {
      task_name: form.task_name.trim(),
      scenic_id: form.scenic_id,
      channels: form.channels,
      collect_type: form.collect_type,
      keywords: form.keywords,
      params: {
        ...params.value,
        ...(form.account_group ? { account_group: form.account_group } : {}),
      },
      schedule_type: form.schedule_type,
      timezone: 'Asia/Shanghai',
    }
    if (form.schedule_type === 'at') payload.schedule_at = form.schedule_at
    if (form.schedule_type === 'interval') {
      payload.schedule_interval_seconds = intervalValue.value * intervalUnit.value
    }
    if (form.schedule_type === 'cron') payload.cron_expression = form.cron_expression.trim()

    const task = await taskApi.create(payload)
    ElMessage.success('任务已创建')
    router.push({ name: 'task-detail', params: { taskId: task.task_id } })
  } finally {
    creating.value = false
  }
}

watch(() => form.collect_type, previewSchedule)

onMounted(async () => {
  const [scenicOptions, channelList, settings, groups] = await Promise.all([
    scenicApi.options(true),
    scenicApi.channels(),
    settingsApi.get().catch(() => null),
    accountApi.groups().catch(() => []),
  ])
  scenics.value = scenicOptions
  channels.value = channelList
  accountGroups.value = groups
  keywordLimit.value = settings?.config?.crawl?.default_keyword_limit ?? 100

  const preset = route.query.scenic_id as string | undefined
  if (preset) {
    form.scenic_id = preset
    await onScenicChange()
  }
  previewSchedule()
})
</script>


<style scoped>
/* 标签里的说明图标：label 是 flex 容器，图标默认会顶到上沿，这里让它和文字居中对齐 */
:deep(.el-form-item__label .info-tip) { align-self: center; }
.form-section {
  margin: 8px 0 18px;
  padding-top: 18px;
  border-top: 1px solid var(--smc-border);
  font-size: 13px;
  font-weight: 600;
  color: var(--smc-text);
}
.form-warn,
.form-error {
  font-size: 13px;
  line-height: 1.6;
  padding: 8px 12px;
  border-radius: var(--smc-radius-sm);
}
.form-warn { color: #a86a0c; background: #fdf6ec; }
.form-error { color: #c4403b; background: #fef0f0; }
</style>
