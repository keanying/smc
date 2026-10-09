<template>
  <div class="collect-params">
    <el-form :model="model" label-width="120px" label-position="left" size="default">
      <el-form-item label="设置方式">
        <el-radio-group v-model="perChannel" size="small">
          <el-radio-button :value="false">所有平台统一</el-radio-button>
          <el-radio-button :value="true">按平台分别设置</el-radio-button>
        </el-radio-group>
      </el-form-item>

      <!-- ============ 统一模式 ============ -->
      <template v-if="!perChannel">
        <el-divider content-position="left">采集数量</el-divider>

        <el-form-item v-if="workChannels.length" label="每关键字作品数">
          <el-input-number v-model="model.max_works" :min="0" :max="10000" style="width: 130px" />
          <span class="muted hint">抖音/快手/小红书/微博</span>
        </el-form-item>
        <el-form-item v-if="workChannels.length" label="每作品评论数">
          <el-input-number v-model="model.max_comments_per_work" :min="0" :max="100000" style="width: 130px" />
        </el-form-item>
        <el-form-item v-if="poiChannels.length" label="景区点评数">
          <el-input-number v-model="model.max_comments" :min="0" :max="100000" style="width: 130px" />
          <span class="muted hint">携程/同程没有作品，只有点评</span>
        </el-form-item>
        <el-form-item v-if="poiChannels.length" label="最多翻几页">
          <el-input-number v-model="model.max_pages" :min="0" :max="5000" style="width: 130px" />
          <span class="muted hint">0 = 一直翻到没有数据（首次全量拉取用这个）</span>
        </el-form-item>

        <template v-if="engineChannels.length">
          <el-divider content-position="left">采集模式</el-divider>
          <el-form-item label="采集模式">
            <el-radio-group v-model="model.collect_engine" size="small">
              <el-radio-button v-for="item in COLLECT_ENGINES" :key="item.value" :value="item.value">
                {{ item.label }}
              </el-radio-button>
            </el-radio-group>
            <div class="muted hint-block">{{ engineHint(model.collect_engine) }}</div>
            <div class="muted hint-block">
              仅对 {{ engineChannels.map(channelLabel).join('/') }} 有效；
              微博/携程/同程只有接口一条路。
            </div>
          </el-form-item>
        </template>

        <template v-if="workChannels.length">
          <el-divider content-position="left">搜索筛选</el-divider>
          <!--
            这里不能写 v-model="model"：model 是 reactive() 出来的常量，
            v-model 生成的更新逻辑是「整个赋值一遍」，对 reactive 常量是无效的，
            表现就是下拉框选了没反应。改成显式 Object.assign 合并。
          -->
          <FilterFields
            :model-value="model" :channels="workChannels" :options-map="optionsMap"
            @update:model-value="(v: Params) => Object.assign(model, v)"
          />
        </template>
      </template>

      <!-- ============ 按平台模式 ============ -->
      <template v-else>
        <el-empty v-if="!selectedChannels.length" :image-size="50" description="先在左边选择采集平台" />

        <div v-for="channel in selectedChannels" :key="channel" class="channel-block">
          <div class="channel-title">
            <el-tag size="small" :color="CHANNEL_COLORS[channel]" style="color: #fff; border: none">
              {{ channelLabel(channel) }}
            </el-tag>
            <span class="muted">
              {{ POI_CHANNELS.includes(channel) ? '只有景区点评一层' : '作品 + 作品下的评论两层' }}
            </span>
          </div>

          <template v-if="!POI_CHANNELS.includes(channel)">
            <el-form-item label="每关键字作品数">
              <el-input-number
                :model-value="valueOf(channel, 'max_works')" :min="0" :max="10000"
                style="width: 130px" @update:model-value="(v: number) => setValue(channel, 'max_works', v)"
              />
            </el-form-item>
            <el-form-item label="每作品评论数">
              <el-input-number
                :model-value="valueOf(channel, 'max_comments_per_work')" :min="0" :max="100000"
                style="width: 130px"
                @update:model-value="(v: number) => setValue(channel, 'max_comments_per_work', v)"
              />
            </el-form-item>
            <el-form-item label="采集评论">
              <el-switch
                :model-value="valueOf(channel, 'collect_comments')"
                @update:model-value="(v: boolean) => setValue(channel, 'collect_comments', v)"
              />
            </el-form-item>

            <el-form-item v-if="BROWSER_ENGINE_CHANNELS.includes(channel)" label="采集模式">
              <el-radio-group
                :model-value="valueOf(channel, 'collect_engine')" size="small"
                @update:model-value="(v: any) => setValue(channel, 'collect_engine', v)"
              >
                <el-radio-button v-for="item in COLLECT_ENGINES" :key="item.value" :value="item.value">
                  {{ item.label }}
                </el-radio-button>
              </el-radio-group>
              <div class="muted hint-block">{{ engineHint(valueOf(channel, 'collect_engine')) }}</div>
            </el-form-item>

            <FilterFields
              :model-value="overrideProxy(channel)" :channels="[channel]" :options-map="optionsMap"
              @update:model-value="(v: Params) => applyOverride(channel, v)"
            />
          </template>

          <template v-else>
            <el-form-item label="采集点评数">
              <el-input-number
                :model-value="valueOf(channel, 'max_comments')" :min="0" :max="100000"
                style="width: 130px" @update:model-value="(v: number) => setValue(channel, 'max_comments', v)"
              />
            </el-form-item>
            <el-form-item label="最多翻几页">
              <el-input-number
                :model-value="valueOf(channel, 'max_pages')" :min="0" :max="5000"
                style="width: 130px" @update:model-value="(v: number) => setValue(channel, 'max_pages', v)"
              />
              <span class="muted hint">0 = 不限</span>
            </el-form-item>
          </template>
        </div>
      </template>

      <!-- 内容过滤是任务级的（不分平台），所以放在两种设置方式之外 -->
      <template v-if="workChannels.length">
        <el-divider content-position="left">内容过滤</el-divider>
        <el-form-item label="内容过滤">
          <el-switch v-model="model.content_filter_enabled" />
          <span class="muted hint">
            {{ model.content_filter_enabled ? '只存相关内容' : '搜到什么存什么' }}
          </span>
          <div class="muted hint-block">
            平台搜索结果里夹着推荐和广告——搜「八大处」能搜出「点斑祛斑」。
            关掉就全部存下来，评论也照采。
          </div>
        </el-form-item>
        <el-form-item v-if="model.content_filter_enabled" label="词表在哪配">
          <div class="muted hint-block" style="margin-top: 0">
            <b>附关键字</b>（命中就留存）和<b>过滤关键字</b>（命中就丢弃）现在配在
            <b>景区管理 → 选中景区 → 附关键字 / 过滤关键字</b> 里，一个景区配一次、所有任务共用。
            <div style="margin-top: 4px">
              以前每建一个任务都要重填一遍：同一个景区跑十个任务就填十遍，
              改一次要改十处，漏改一处两个任务采出来的东西就不一样了。
            </div>
            <div style="margin-top: 4px">
              判定顺序：<b>主关键字</b>拿去搜 → <b>附关键字</b>决定留不留 → <b>过滤关键字</b>决定丢不丢。
            </div>
          </div>
        </el-form-item>
      </template>

      <el-divider content-position="left">通用</el-divider>

      <el-form-item v-if="!perChannel" label="采集评论">
        <el-switch v-model="model.collect_comments" />
        <span class="muted hint">携程/同程不受此项影响</span>
      </el-form-item>
      <el-form-item label="采集子评论">
        <el-switch v-model="model.enable_sub_comments" />
      </el-form-item>
      <el-form-item label="最深评论层级">
        <el-input-number
          v-model="model.max_comment_level" :min="1" :max="5"
          style="width: 130px" :disabled="!model.enable_sub_comments"
        />
      </el-form-item>
      <el-form-item label="指定账号">
        <el-input v-model="model.account_name" placeholder="留空自动轮换" style="width: 220px" />
      </el-form-item>
    </el-form>

    <div class="muted" style="font-size: 12px">
      数量填 0 表示不限制。评论量大时建议先设个上限试跑一轮。
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { taskApi, type ChannelFilterOptions } from '../api'
import {
  BROWSER_ENGINE_CHANNELS, CHANNEL_COLORS, COLLECT_ENGINES, POI_CHANNELS,
  channelLabel, normalizeEngine,
} from '../constants'
import FilterFields from './FilterFields.vue'

type Params = Record<string, unknown>

const props = defineProps<{
  modelValue: Params
  channels: string[]
  /** 编辑已有任务时的初始值。挂载时自动回填，不用外部再算时机调 load() */
  initial?: Params | null
}>()
const emit = defineEmits<{ 'update:modelValue': [Params] }>()

/** 顶层默认值，同时也是「按平台」模式下没写平台值时的回退 */
const model = reactive<Record<string, any>>({
  max_works: 100,
  max_comments_per_work: 500,
  max_comments: 1000,
  max_pages: 0,
  collect_comments: true,
  enable_sub_comments: true,
  max_comment_level: 3,
  account_name: '',
  // api / hybrid / human，见 constants.COLLECT_ENGINES
  collect_engine: 'hybrid',
  // 内容过滤（见后端 core/content_filter.py）。默认开 + 只按搜索关键字，
  // 也就是改造前的行为——默认关掉会让老任务突然开始存一堆广告。
  content_filter_enabled: true,
  content_filter_mode: 'keyword',
  content_filter_extra: '',
  sort: 'general',
  publish_within: 'unlimited',
  start_date: '',
  end_date: '',
})

const perChannel = ref(false)
const overrides = reactive<Record<string, Record<string, unknown>>>({})
const optionsMap = ref<Record<string, ChannelFilterOptions>>({})

const selectedChannels = computed(() => props.channels)
const workChannels = computed(() => props.channels.filter((c) => !POI_CHANNELS.includes(c)))
const poiChannels = computed(() => props.channels.filter((c) => POI_CHANNELS.includes(c)))
/** 选了「采集模式」才有意义的平台 */
const engineChannels = computed(() =>
  props.channels.filter((c) => BROWSER_ENGINE_CHANNELS.includes(c)),
)

function engineHint(value: unknown): string {
  const engine = normalizeEngine(value)
  return COLLECT_ENGINES.find((item) => item.value === engine)?.hint || ''
}

/** 平台值没设时显示顶层默认，用户一眼能看出「不改就是这个数」 */
function valueOf(channel: string, key: string): any {
  const own = overrides[channel]?.[key]
  if (own !== undefined && own !== null && own !== '') return own
  return model[key]
}

function setValue(channel: string, key: string, value: unknown) {
  if (!overrides[channel]) overrides[channel] = {}
  overrides[channel][key] = value
}

/** 给 FilterFields 一个「平台值优先、否则用顶层默认」的视图 */
function overrideProxy(channel: string): Params {
  return {
    sort: valueOf(channel, 'sort'),
    publish_within: valueOf(channel, 'publish_within'),
    start_date: valueOf(channel, 'start_date'),
    end_date: valueOf(channel, 'end_date'),
  }
}

function applyOverride(channel: string, values: Params) {
  for (const [key, value] of Object.entries(values)) setValue(channel, key, value)
}

/** 拼出交给后端的 params；后端还会再 normalize 一次，这里不必完美 */
function build(): Params {
  const out: Params = { ...model }
  // 表单里摊平存（三个 el-form-item 各绑一个字段），交给后端时收成一个对象
  out.content_filter = {
    enabled: model.content_filter_enabled,
    mode: model.content_filter_mode,
    extra_keywords: model.content_filter_extra,
  }
  delete out.content_filter_enabled
  delete out.content_filter_mode
  delete out.content_filter_extra
  if (perChannel.value) {
    const channelParams: Record<string, Record<string, unknown>> = {}
    for (const channel of props.channels) {
      const values = overrides[channel]
      if (values && Object.keys(values).length) channelParams[channel] = { ...values }
    }
    if (Object.keys(channelParams).length) out.channel_params = channelParams
  }
  return out
}

/** 从已有任务的 params 回填表单（编辑任务时用） */
function load(source: Params) {
  for (const key of Object.keys(model)) {
    const value = source[key]
    if (value !== undefined && value !== null && value !== '') model[key] = value
  }
  // 改名前存的是 auto/browser，映射成 hybrid/human，否则单选框会一个都不选中
  model.collect_engine = normalizeEngine(model.collect_engine)

  // 内容过滤是个对象，摊回三个表单字段
  const filter = source.content_filter
  if (filter && typeof filter === 'object') {
    const raw = filter as Record<string, unknown>
    model.content_filter_enabled = raw.enabled !== false
    model.content_filter_mode = raw.mode === 'keyword_plus' ? 'keyword_plus' : 'keyword'
    model.content_filter_extra = String(raw.extra_keywords ?? '')
  }
  for (const key of Object.keys(overrides)) delete overrides[key]
  const incoming = source.channel_params
  if (incoming && typeof incoming === 'object') {
    for (const [channel, values] of Object.entries(incoming as Record<string, Params>)) {
      const copy: Params = { ...values }
      if (copy.collect_engine !== undefined) copy.collect_engine = normalizeEngine(copy.collect_engine)
      overrides[channel] = copy
    }
    perChannel.value = Object.keys(overrides).length > 0
  } else {
    perChannel.value = false
  }
}

onMounted(async () => {
  // 各平台支持哪些排序档位由后端说了算，前端不硬编码一份免得走偏
  try {
    const result = await taskApi.filterOptions()
    const map: Record<string, ChannelFilterOptions> = {}
    for (const item of result.channels) map[item.channel] = item
    optionsMap.value = map
  } catch {
    optionsMap.value = {}
  }
  if (props.initial) load(props.initial)
})

watch([() => ({ ...model }), perChannel, overrides], () => emit('update:modelValue', build()), {
  deep: true,
  immediate: true,
})

// 平台取消勾选后，它的残留配置不该再提交
watch(() => props.channels, (list) => {
  for (const channel of Object.keys(overrides)) {
    if (!list.includes(channel)) delete overrides[channel]
  }
})

defineExpose({ load, build })
</script>

<style scoped>
.hint {
  font-size: 12px;
  margin-left: 8px;
}

/* 选项下面的说明单独占一行，跟在 radio 后面会被挤成两截 */
.hint-block {
  font-size: 12px;
  line-height: 1.6;
  width: 100%;
}

.channel-block {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 12px 14px 0;
  margin-bottom: 12px;
  background: #fafafa;
}

.channel-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  margin-bottom: 10px;
}
</style>
