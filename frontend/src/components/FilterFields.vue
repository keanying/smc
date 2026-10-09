<template>
  <div>
    <!-- 排序：只在接口支持的平台上显示 -->
    <el-form-item v-if="sortOptions.length" label="排序依据">
      <el-select :model-value="model.sort" style="width: 180px" @update:model-value="set('sort', $event)">
        <el-option v-for="o in sortOptions" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <span v-if="mixedSorts" class="muted hint">所选平台的排序档位不同，这里取交集</span>
    </el-form-item>
    <el-form-item v-else-if="channels.length" label="排序依据">
      <span class="muted">接口不支持排序</span>
    </el-form-item>

    <el-form-item label="发布时间">
      <el-select
        :model-value="model.publish_within" style="width: 180px"
        @update:model-value="onPresetChange"
      >
        <el-option v-for="o in PUBLISH_WITHIN" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
    </el-form-item>

    <el-form-item v-if="model.publish_within === 'custom'" label="日期区间">
      <el-date-picker
        :model-value="dateRange" type="daterange" range-separator="至"
        start-placeholder="开始日期" end-placeholder="结束日期"
        value-format="YYYY-MM-DD" style="width: 260px"
        @update:model-value="onRangeChange"
      />
    </el-form-item>

    <el-form-item v-if="notes.length" label=" ">
      <div class="filter-notes">
        <div v-for="(note, i) in notes" :key="i">{{ note }}</div>
      </div>
    </el-form-item>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ChannelFilterOptions, FilterOption } from '../api'
import { channelLabel } from '../constants'

type Params = Record<string, any>

const props = defineProps<{
  modelValue: Params
  /** 这组筛选作用在哪些平台上 */
  channels: string[]
  optionsMap: Record<string, ChannelFilterOptions>
}>()
const emit = defineEmits<{ 'update:modelValue': [Params] }>()

const PUBLISH_WITHIN: FilterOption[] = [
  { value: 'unlimited', label: '不限' },
  { value: 'day', label: '一天内' },
  { value: 'week', label: '一周内' },
  { value: 'half_year', label: '半年内' },
  { value: 'custom', label: '自定义日期区间' },
]

const model = computed<Params>(() => props.modelValue || {})

function set(key: string, value: unknown) {
  emit('update:modelValue', { ...model.value, [key]: value })
}

/**
 * 多个平台共用一组筛选时，排序档位取交集——
 * 比如同时选了抖音和小红书，「最多收藏」只有小红书有，就不该出现在列表里，
 * 否则用户选了它，抖音那边会被静默降级成综合排序。
 */
const sortOptions = computed<FilterOption[]>(() => {
  const lists = props.channels
    .map((c) => props.optionsMap[c]?.sorts || [])
    .filter((list) => list.length > 0)
  if (!lists.length) return []

  const [first, ...rest] = lists
  return first.filter((o) => rest.every((list) => list.some((x) => x.value === o.value)))
})

/** 有平台支持排序、有平台不支持（或档位不同）时提示一下 */
const mixedSorts = computed(() => {
  const counts = props.channels.map((c) => (props.optionsMap[c]?.sorts || []).length)
  return new Set(counts).size > 1
})

const dateRange = computed<[string, string] | null>(() => {
  const start = model.value.start_date
  const end = model.value.end_date
  return start || end ? [start || '', end || ''] : null
})

function onPresetChange(value: string) {
  const next: Params = { ...model.value, publish_within: value }
  if (value !== 'custom') {
    next.start_date = ''
    next.end_date = ''
  }
  emit('update:modelValue', next)
}

function onRangeChange(value: [string, string] | null) {
  emit('update:modelValue', {
    ...model.value,
    publish_within: 'custom',
    start_date: value?.[0] || '',
    end_date: value?.[1] || '',
  })
}

/** 各平台的能力差异，直接说清楚，别让用户猜 */
const notes = computed(() => {
  const result: string[] = []
  for (const channel of props.channels) {
    const options = props.optionsMap[channel]
    if (!options) continue
    if (options.note) result.push(`${channelLabel(channel)}：${options.note}`)
    else if (model.value.publish_within === 'custom' && !options.time_native) {
      result.push(`${channelLabel(channel)}：接口不认自定义日期，会采回来本地过滤。`)
    }
  }
  return result
})
</script>

<style scoped>
.hint {
  font-size: 12px;
  margin-left: 8px;
}

.filter-notes {
  font-size: 12px;
  line-height: 1.7;
  color: #909399;
  background: #f8f8f9;
  border-left: 3px solid #dcdfe6;
  padding: 6px 10px;
  border-radius: 3px;
}
</style>
