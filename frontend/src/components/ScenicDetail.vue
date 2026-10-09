<template>
  <el-tabs v-model="tab">
    <!-- 主关键字 -->
    <el-tab-pane name="keywords">
      <template #label>主关键字 <el-badge :value="keywords.length" :max="999" type="primary" /></template>

      <el-alert type="info" :closable="false" style="margin-bottom: 12px">
        <b>主关键字拿去平台上搜</b>，决定「搜到什么」。
        搜回来的内容还要过两道：先看<b>附关键字</b>决定留不留，再看<b>过滤关键字</b>决定丢不丢。
        <span class="muted">（这三道只对抖音 / 快手 / 小红书 / 微博生效；携程、同程是按 POI 拉点评的，用不上关键字）</span>
      </el-alert>

      <div class="toolbar">
        <el-input
          v-model="keywordInput" placeholder="输入关键字，回车添加；支持逗号/换行批量粘贴"
          style="width: 420px" clearable @keyup.enter="addKeywords"
        />
        <el-button type="primary" :loading="savingKeyword" @click="addKeywords">添加</el-button>
        <span class="muted" style="font-size: 12px">
          新建任务时默认最多带入 100 个启用的关键字
        </span>
      </div>

      <div v-if="keywords.length" class="keyword-list">
        <el-tag
          v-for="item in keywords" :key="item.id"
          :type="item.enabled ? 'primary' : 'info'"
          closable size="large" style="margin: 0 8px 8px 0; cursor: pointer"
          @close="removeKeyword(item.id)"
          @click="toggleKeyword(item)"
        >
          {{ item.keyword }}
          <span v-if="!item.enabled" class="muted">（已停用）</span>
        </el-tag>
        <div class="muted" style="font-size: 12px; margin-top: 4px">
          点击标签可切换启用/停用，点 × 删除
        </div>
      </div>
      <el-empty v-else description="还没有关键字" :image-size="70" />
    </el-tab-pane>

    <!-- 附关键字 -->
    <el-tab-pane name="aux">
      <template #label>附关键字 <el-badge :value="auxWords.length" :max="999" type="success" /></template>

      <el-alert type="success" :closable="false" style="margin-bottom: 12px">
        <b>命中就留存</b>：搜回来的内容，标题/正文/标签里含<b>主关键字或任意一个附关键字</b>就存下来，
        都不含就丢弃。
        <div class="muted" style="margin-top: 4px">
          用来兜住"别名"。实测搜「盘山风景区」，20 条笔记里只有 1 条写了完整名字，
          其余都只写「盘山」——不配附关键字的话，那 19 条会被当成不相关全丢掉。
        </div>
      </el-alert>

      <div class="toolbar">
        <el-input
          v-model="auxInput" placeholder="输入附关键字，回车添加；支持逗号/换行批量粘贴"
          style="width: 420px" clearable @keyup.enter="addAux"
        />
        <el-button type="success" :loading="savingAux" @click="addAux">添加</el-button>
        <span class="muted" style="font-size: 12px">
          最多 {{ MAX_AUX }} 个（当前 {{ auxWords.length }}）
        </span>
      </div>

      <div v-if="auxWords.length" class="keyword-list">
        <el-tag
          v-for="item in auxWords" :key="item.id"
          :type="item.enabled ? 'success' : 'info'"
          closable size="large" style="margin: 0 8px 8px 0; cursor: pointer"
          @close="removeWord(item.id)" @click="toggleWord(item)"
        >
          {{ item.word }}
          <span v-if="!item.enabled" class="muted">（已停用）</span>
        </el-tag>
        <div class="muted" style="font-size: 12px; margin-top: 4px">
          点击标签可切换启用/停用，点 × 删除
        </div>
      </div>
      <el-empty v-else description="还没有附关键字（不配的话只按主关键字判定留存）" :image-size="70" />
    </el-tab-pane>

    <!-- 过滤关键字 -->
    <el-tab-pane name="exclude">
      <template #label>过滤关键字 <el-badge :value="excludeWords.length" :max="999" type="danger" /></template>

      <el-alert type="warning" :closable="false" style="margin-bottom: 12px">
        <b>命中就丢，一票否决</b>：上面两道都过了的内容，只要标题/正文/标签里出现任意一个过滤关键字，照样丢弃。
        <div class="muted" style="margin-top: 4px">
          用来挡广告和蹭热度的，比如「代运营」「加微信」「涨粉」。
          <b>不配就完全不起作用</b>，不会因为列表是空的就把内容拦下来。
        </div>
      </el-alert>

      <div class="toolbar">
        <el-input
          v-model="excludeInput" placeholder="输入过滤关键字，回车添加；支持逗号/换行批量粘贴"
          style="width: 420px" clearable @keyup.enter="addExclude"
        />
        <el-button type="danger" :loading="savingExclude" @click="addExclude">添加</el-button>
        <span class="muted" style="font-size: 12px">
          最多 {{ MAX_EXCLUDE }} 个（当前 {{ excludeWords.length }}）
        </span>
      </div>

      <div v-if="excludeWords.length" class="keyword-list">
        <el-tag
          v-for="item in excludeWords" :key="item.id"
          :type="item.enabled ? 'danger' : 'info'"
          closable size="large" style="margin: 0 8px 8px 0; cursor: pointer"
          @close="removeWord(item.id)" @click="toggleWord(item)"
        >
          {{ item.word }}
          <span v-if="!item.enabled" class="muted">（已停用）</span>
        </el-tag>
        <div class="muted" style="font-size: 12px; margin-top: 4px">
          点击标签可切换启用/停用，点 × 删除
        </div>
      </div>
      <el-empty v-else description="还没有过滤关键字（不配则不生效）" :image-size="70" />
    </el-tab-pane>

    <!-- 采集目标 -->
    <el-tab-pane name="targets">
      <template #label>采集目标 <el-badge :value="targets.length" :max="999" type="primary" /></template>

      <el-alert type="info" :closable="false" style="margin-bottom: 12px">
        <b>POI</b>：携程填 POI_ID，同程填 sid —— 这两个平台只有景区点评，没有作品。<br />
        <b>主页</b>：抖音填 sec_user_id 或主页链接，快手填 user_id，小红书填 user_id，微博填 uid。
      </el-alert>

      <div class="toolbar">
        <el-select v-model="targetForm.channel" placeholder="平台" style="width: 120px">
          <el-option v-for="c in channels" :key="c.value" :label="c.label" :value="c.value" />
        </el-select>
        <el-select v-model="targetForm.target_type" style="width: 110px">
          <el-option label="POI 点评" value="poi" />
          <el-option label="用户主页" value="creator" />
        </el-select>
        <!-- POI 类型优先从档案里挑：手填 POI ID 最常见的两个问题是名字打错和
             ID 少复制一位，而这两个都要等到任务跑出 0 条才会发现。
             档案里没有（还没采过那个省）仍然可以手填，所以两条路都留着。 -->
        <el-select
          v-if="targetForm.target_type === 'poi' && ARCHIVE_CHANNELS.includes(targetForm.channel)"
          v-model="targetForm.target_id" filterable remote clearable
          :remote-method="searchArchive" :loading="searchingArchive"
          placeholder="输入景区名搜档案，或直接填 POI ID"
          allow-create default-first-option style="width: 300px"
          @change="onArchivePick"
        >
          <el-option
            v-for="a in archiveOptions" :key="a.poi_id"
            :label="`${a.poi_name}｜${[a.province, a.city_name].filter(Boolean).join(' ')}｜${a.poi_id}`"
            :value="a.poi_id"
          />
          <template #empty>
            <div class="archive-empty">
              档案里没搜到——去<b>景区档案</b>采一次，或者直接把 POI ID 打进来
            </div>
          </template>
        </el-select>
        <el-input
          v-else v-model="targetForm.target_id" :placeholder="targetPlaceholder"
          style="width: 300px" clearable
        />
        <el-input v-model="targetForm.target_name" placeholder="备注名（可选）" style="width: 160px" clearable />
        <el-button type="primary" :loading="savingTarget" @click="saveTarget">添加</el-button>
      </div>

      <el-table :data="targets" size="small" border>
        <el-table-column label="平台" width="100">
          <template #default="{ row }">
            <el-tag size="small" :color="CHANNEL_COLORS[row.channel]" style="color: #fff; border: none">
              {{ channelLabel(row.channel) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="90">
          <template #default="{ row }">
            {{ row.target_type === 'poi' ? 'POI 点评' : '用户主页' }}
          </template>
        </el-table-column>
        <el-table-column prop="target_id" label="目标ID" min-width="200" class-name="mono" />
        <el-table-column prop="target_name" label="备注名" width="150" />
        <el-table-column label="状态" width="80" align="center">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
              {{ row.enabled ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" align="center">
          <template #default="{ row }">
            <el-button link type="danger" @click="removeTarget(row.id)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty><el-empty description="还没有采集目标" :image-size="60" /></template>
      </el-table>
    </el-tab-pane>

    <!-- 景区档案：这个景区在同程/携程/去哪儿上各自的资料 -->
    <el-tab-pane v-if="hasPoiTarget" name="archive">
      <template #label>
        景区档案 <el-badge :value="archives.length" :max="9" type="primary" />
      </template>

      <el-alert type="info" :closable="false" style="margin-bottom: 12px">
        档案是<b>平台页面上的景区资料</b>，不是采集到的点评，不参与标注。<br />
        这里只显示<b>已经关联到本景区</b>的那几条；要新增关联，去左侧的
        <b>景区档案</b>板块里搜到之后「挂到景区」。
      </el-alert>

      <el-table v-if="archives.length" :data="archives" size="small" border>
        <el-table-column label="平台" width="90">
          <template #default="{ row }">
            <el-tag size="small" :color="CHANNEL_COLORS[row.channel]" style="color: #fff; border: none">
              {{ channelLabel(row.channel) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="poi_name" label="平台上的名称" min-width="160" />
        <el-table-column prop="poi_id" label="POI ID" width="110" class-name="mono" />
        <el-table-column prop="scenic_level" label="等级" width="70" align="center">
          <template #default="{ row }">{{ row.scenic_level || '—' }}</template>
        </el-table-column>
        <el-table-column prop="tel" label="电话" width="140">
          <template #default="{ row }">{{ row.tel || '—' }}</template>
        </el-table-column>
        <el-table-column label="详情" width="90" align="center">
          <template #default="{ row }">
            <el-tooltip v-if="row.detail_status === 'error'" :content="row.detail_error">
              <el-tag size="small" type="danger">失败</el-tag>
            </el-tooltip>
            <el-tag v-else-if="row.detail_status === 'success'" size="small" type="success">已采</el-tag>
            <el-tooltip v-else-if="row.detail_status === 'unsupported'" content="这个平台没有景区详情页，采不到更多字段">
              <el-tag size="small" type="info">无</el-tag>
            </el-tooltip>
            <el-tag v-else size="small">待采</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="110" align="center">
          <template #default="{ row }">
            <el-button
              link type="primary" :disabled="row.detail_status === 'unsupported'"
              :loading="refreshing === row.poi_id" @click="refreshArchive(row)"
            >
              重新采集
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty
        v-else :image-size="70"
        description="这个景区还没关联任何平台档案——去左侧「景区档案」搜到之后挂过来"
      />
    </el-tab-pane>
  </el-tabs>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  archiveApi, scenicApi, type ArchiveItem, type ChannelOption,
  type FilterWord, type Keyword, type Target,
} from '../api'
import { CHANNEL_COLORS, channelLabel } from '../constants'

const props = defineProps<{ scenicId: string; channels: ChannelOption[] }>()

const tab = ref('keywords')
const keywords = ref<Keyword[]>([])
const targets = ref<Target[]>([])
const keywordInput = ref('')
const savingKeyword = ref(false)
const savingTarget = ref(false)

/**
 * 每类词的上限，和后端 ScenicRepository.MAX_FILTER_WORDS 逐一对齐。
 * 两类**不一样**：附关键字是景区的各种别名，一个大景区加周边地名能堆到几百个；
 * 过滤关键字是"一概不要的东西"，几十个够用，写多了只会误伤。
 */
const MAX_AUX = 700
const MAX_EXCLUDE = 200
const filterWords = ref<FilterWord[]>([])
const auxInput = ref('')
const excludeInput = ref('')
const savingAux = ref(false)
const savingExclude = ref(false)
const auxWords = computed(() => filterWords.value.filter((w) => w.kind === 'aux'))
const excludeWords = computed(() => filterWords.value.filter((w) => w.kind === 'exclude'))

/** 一次粘贴多个：逗号、顿号、分号、换行都当分隔符。 */
function splitWords(raw: string): string[] {
  return raw.split(/[,，、;；\n\r]+/).map((x) => x.trim()).filter(Boolean)
}

async function addWords(kind: 'aux' | 'exclude', raw: string) {
  const parts = splitWords(raw)
  if (!parts.length) {
    ElMessage.warning(kind === 'aux' ? '先输入附关键字' : '先输入过滤关键字')
    return false
  }
  const r = await scenicApi.addFilterWords(props.scenicId, kind, parts)
  // 超上限被丢掉的必须说出来，否则用户会以为 250 个全存上了
  ElMessage.success(
    `新增 ${r.added} 个，跳过重复 ${r.skipped} 个`
    + (r.dropped
      ? `；超出 ${kind === 'aux' ? MAX_AUX : MAX_EXCLUDE} 个上限，丢弃 ${r.dropped} 个`
      : ''),
  )
  await loadAll()
  return true
}

async function addAux() {
  savingAux.value = true
  try {
    if (await addWords('aux', auxInput.value)) auxInput.value = ''
  } finally { savingAux.value = false }
}

async function addExclude() {
  savingExclude.value = true
  try {
    if (await addWords('exclude', excludeInput.value)) excludeInput.value = ''
  } finally { savingExclude.value = false }
}

async function removeWord(id: number) {
  await scenicApi.removeFilterWord(id)
  await loadAll()
}

async function toggleWord(item: FilterWord) {
  await scenicApi.toggleFilterWord(item.id, !item.enabled)
  await loadAll()
}

/**
 * 从景区档案里挑 POI。
 *
 * 只对**有档案的三个渠道**（同程/携程/去哪儿）生效——别的平台没有 POI，
 * 那边走的是「用户主页」，和档案无关。
 *
 * allow-create 是刻意开着的：档案是按省采的，用户要加的那个景区所在的省
 * 可能还没采过。这时候他应该能直接把 POI ID 打进去，而不是被逼着
 * 先去采一整个省。
 */
const archiveOptions = ref<ArchiveItem[]>([])
const searchingArchive = ref(false)

async function searchArchive(keyword: string) {
  if (!keyword || !keyword.trim()) {
    archiveOptions.value = []
    return
  }
  searchingArchive.value = true
  try {
    const data = await archiveApi.list({
      channel: targetForm.channel, keyword: keyword.trim(),
      page: 1, page_size: 30,
    })
    archiveOptions.value = data.items
  } finally {
    searchingArchive.value = false
  }
}

/** 从档案里挑中一条时，顺手把备注名填成景区名——省得再打一遍。 */
function onArchivePick(value: string) {
  const hit = archiveOptions.value.find((a) => a.poi_id === value)
  if (hit && !targetForm.target_name.trim()) {
    targetForm.target_name = hit.poi_name
  }
}

const targetForm = reactive({
  channel: 'ctrip', target_type: 'poi' as 'poi' | 'creator',
  target_id: '', target_name: '',
})

const targetPlaceholder = computed(() => {
  if (targetForm.target_type === 'creator') return '用户ID 或主页链接'
  return targetForm.channel === 'ctrip' ? '携程 POI_ID，如 32289'
    : targetForm.channel === 'tongcheng' ? '同程 sid，如 32289'
    : '该平台没有 POI，请选「用户主页」'
})

/**
 * 景区档案。
 *
 * 标签页跟着**采集目标**走而不是跟着档案表走：一个景区可能刚建好、
 * 还没挂上任何档案，但它确实是 POI 型平台的景区、确实该能看这一栏。
 * 只看档案表的话，这些景区永远看不到入口。
 */
const ARCHIVE_CHANNELS = ['tongcheng', 'ctrip', 'qunar']
const archives = ref<ArchiveItem[]>([])
const refreshing = ref('')
const hasPoiTarget = computed(
  () => targets.value.some(
    (t) => t.target_type === 'poi' && ARCHIVE_CHANNELS.includes(t.channel)),
)

async function loadArchives() {
  if (!hasPoiTarget.value) {
    archives.value = []
    return
  }
  const data = await archiveApi.list({ scenic_id: props.scenicId, page_size: 20 })
  archives.value = data.items
}

async function refreshArchive(row: ArchiveItem) {
  refreshing.value = row.poi_id
  try {
    const fresh = await archiveApi.refreshOne(row.channel, row.poi_id)
    if (fresh) Object.assign(row, fresh)
    ElMessage.success('已重新采集')
  } finally {
    refreshing.value = ''
  }
}

async function loadAll() {
  const [k, w, t] = await Promise.all([
    scenicApi.keywords(props.scenicId),
    scenicApi.filterWords(props.scenicId),
    scenicApi.targets(props.scenicId),
  ])
  keywords.value = k
  filterWords.value = w
  targets.value = t
  // 要先有 targets 才知道该不该拉档案，所以放在赋值之后而不是并进上面的 Promise.all
  await loadArchives()
}

async function addKeywords() {
  // 支持一次粘贴多个：逗号、顿号、分号、换行都当分隔符
  const parts = keywordInput.value
    .split(/[,，、;；\n\r]+/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (!parts.length) {
    ElMessage.warning('先输入关键字')
    return
  }
  savingKeyword.value = true
  try {
    const result = await scenicApi.addKeywords(props.scenicId, parts)
    ElMessage.success(`新增 ${result.added} 个，跳过重复 ${result.skipped} 个`)
    keywordInput.value = ''
    await loadAll()
  } finally {
    savingKeyword.value = false
  }
}

async function removeKeyword(id: number) {
  await scenicApi.removeKeyword(id)
  await loadAll()
}

async function toggleKeyword(item: Keyword) {
  await scenicApi.toggleKeyword(item.id, !item.enabled)
  await loadAll()
}

async function saveTarget() {
  if (!targetForm.target_id.trim()) {
    ElMessage.warning('目标ID不能为空')
    return
  }
  savingTarget.value = true
  try {
    await scenicApi.saveTarget(props.scenicId, {
      channel: targetForm.channel,
      target_type: targetForm.target_type,
      target_id: targetForm.target_id.trim(),
      target_name: targetForm.target_name || null,
      enabled: 1,
    })
    ElMessage.success('已添加')
    targetForm.target_id = ''
    targetForm.target_name = ''
    await loadAll()
  } finally {
    savingTarget.value = false
  }
}

async function removeTarget(id: number) {
  await scenicApi.removeTarget(id)
  await loadAll()
}

// ⚠️ 只留 watch + immediate，不要再加 onMounted(loadAll)：
// 两个都写的话，组件挂载时 scenicId 恰好也被赋值，keywords/targets
// 会各被拉两遍（一共 4 个请求），而且两次响应先后到达还可能互相覆盖。
watch(() => props.scenicId, loadAll, { immediate: true })
</script>

<style scoped>
.keyword-list { padding: 4px 0; }
/* 开放时间、优待政策、服务设施在去哪儿页面上本来就是分行的，
   不保留换行会糊成一坨长句子，看不出"成人/儿童/老人"是三条。 */
.pre { white-space: pre-wrap; word-break: break-word; }
.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.archive-empty { padding: 10px 14px; color: var(--el-text-color-secondary); font-size: 12px; }
</style>
