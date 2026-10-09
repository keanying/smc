<template>
  <div class="archive-page">
    <div class="page-head">
      <div>
        <h2>景区档案</h2>
        <p class="muted">
          三个平台的景区资料库：<b>采一次落库</b>，之后建景区、加采集目标都在库里挑，
          不用每次去打平台的接口。这里存的是平台自己的景区资料（名称/等级/地址/开放时间…），
          <b>不是点评</b>，不参与标注。
        </p>
      </div>
      <div class="stat-row">
        <div v-for="s in stats" :key="s.channel" class="stat-card">
          <div class="stat-name">{{ channelLabel(s.channel) }}</div>
          <div class="stat-num">{{ s.total }}</div>
          <div class="muted">已建景区 {{ s.linked }}</div>
        </div>
      </div>
    </div>

    <el-tabs v-model="channel" class="ch-tabs" @tab-change="onChannelChange">
      <el-tab-pane v-for="c in channels" :key="c.channel" :name="c.channel">
        <template #label>
          {{ c.label }}
          <el-tag v-if="!c.supports_detail" size="small" type="info">无详情页</el-tag>
        </template>
      </el-tab-pane>
    </el-tabs>

    <el-alert
      v-if="currentChannel && !currentChannel.supports_detail"
      type="warning" :closable="false" style="margin-bottom: 12px"
    >
      <b>{{ currentChannel.label }}没有景区详情页</b>——列表页只给名称、等级、地址、省市这几样。
      所以开放时间 / 电话 / 优待政策 / 服务设施 / 介绍这几列对它<b>永远是空的</b>，
      这不是采失败，重采多少次也不会有。
    </el-alert>

    <!-- 采集作业进度 -->
    <el-card v-if="job" shadow="never" class="job-card" :class="`is-${job.status}`">
      <div class="job-head">
        <b>{{ jobStatusText }}</b>
        <span class="muted">
          {{ job.current ? `正在采：${job.current}` : '' }}
          {{ job.message }}
        </span>
        <div class="spacer" />
        <el-button
          v-if="job.status === 'running'" size="small" type="danger" plain
          @click="cancelJob"
        >
          停止
        </el-button>
      </div>
      <el-progress :percentage="job.percent" :status="progressStatus" />
      <div class="muted job-nums">
        区域 {{ job.regions_done }}/{{ job.regions_total }} ·
        采到 {{ job.collected }} 条（新增 {{ job.created }}，更新 {{ job.updated }}）
        <template v-if="job.with_detail">
          · 详情 {{ job.detail_done }} 成功 / {{ job.detail_failed }} 失败
        </template>
        · 用时 {{ job.elapsed_seconds }}s
      </div>
      <div v-if="job.error" class="job-error">{{ job.error }}</div>
    </el-card>

    <!-- 采集区域 -->
    <el-card shadow="never" class="region-card">
      <div class="toolbar">
        <!-- 标题整块可点：区域多的时候（同程 387 个）这一片能占掉大半屏，
             收起来之后下面的档案列表才看得见。 -->
        <b class="region-title" @click="toggleRegions">
          <el-icon class="fold-icon" :class="{ 'is-open': regionsOpen }"><ArrowRight /></el-icon>
          采集区域
          <span class="muted">（{{ visibleRegions.length }}/{{ regions.length }}）</span>
        </b>
        <el-button size="small" :loading="loadingRegions" :icon="Refresh" @click="refreshRegions">
          重新探测
        </el-button>
        <el-button size="small" @click="manualVisible = true">手工添加</el-button>
        <!-- 去哪儿的城市索引是一张扁平名单，没有省份归属。省份靠另一份
             行政区划数据按名字接上去——不同步这一次，去哪儿采下来的档案
             省份列就是空的，档案页也没法按省筛。 -->
        <el-button
          v-if="channel === 'qunar'" size="small" :loading="syncingRegions"
          @click="syncQunarRegions"
        >
          同步行政区划
        </el-button>
        <el-radio-group v-if="hasCityRegions" v-model="regionLevel" size="small">
          <el-radio-button value="province">按省（{{ levelCount('province') }}）</el-radio-button>
          <el-radio-button value="city">按市（{{ levelCount('city') }}）</el-radio-button>
        </el-radio-group>
        <el-input
          v-if="regions.length > 30" v-model="regionFilter" placeholder="筛区域"
          clearable size="small" style="width: 130px"
        />
        <!-- 全选作用在**当前看得见的**区域上（按省/按市 + 筛选之后的），
             不是整份清单。筛出「贵州」再点全选，选中的就该是这一个，
             而不是悄悄把另外 386 个也勾上。 -->
        <el-checkbox
          v-if="visibleRegions.length" v-model="allVisibleSelected"
          :indeterminate="someVisibleSelected"
        >
          全选{{ regionFilter || (hasCityRegions && regionLevel === 'city') ? '当前' : '' }}
        </el-checkbox>
        <el-button
          v-if="selectedRegions.length" link type="primary" size="small"
          @click="selectedRegions = []"
        >
          清空（已选 {{ selectedRegions.length }}）
        </el-button>
        <div class="spacer" />
        <el-checkbox v-model="withDetail" :disabled="!currentChannel?.supports_detail">
          同时采详情
        </el-checkbox>
        <el-tooltip
          content="逐个进景区详情页取开放时间/电话/优待政策/服务设施/介绍。
                   一个省几百上千条，每条一次请求，会慢很多。不勾也能先把名录采下来，
                   详情之后随时补。"
          placement="top"
        >
          <el-icon class="muted"><QuestionFilled /></el-icon>
        </el-tooltip>
        <el-button
          type="primary" size="small" :disabled="!selectedRegions.length || job?.status === 'running'"
          @click="startCollect"
        >
          采集选中的 {{ selectedRegions.length || '' }} 个区域
        </el-button>
      </div>

      <el-empty
        v-if="!regions.length" :image-size="60"
        description="还没有区域清单——点「重新探测」从平台上抓，抓不到就手工添加一条"
      />
      <!-- 收起时把已选的摘要留在外面：勾了十几个省再一收，
           什么都不显示的话就不知道自己到底选了什么，只能再展开确认一遍。 -->
      <div v-else-if="!regionsOpen" class="region-folded muted">
        <template v-if="selectedRegions.length">
          已选 {{ selectedRegions.length }} 个：{{ selectedNames }}
        </template>
        <template v-else>区域清单已收起，点上面的标题展开</template>
      </div>
      <el-checkbox-group v-else v-model="selectedRegions" class="region-grid">
        <el-checkbox v-for="r in visibleRegions" :key="r.region_id" :value="r.region_id">
          {{ r.region_name }}
          <span class="muted">
            <template v-if="r.last_sync_time">（{{ r.poi_count }} 条）</template>
            <template v-else>（没采过）</template>
          </span>
        </el-checkbox>
      </el-checkbox-group>
    </el-card>

    <!-- 档案列表 -->
    <el-card shadow="never">
      <div class="toolbar">
        <el-input
          v-model="query.keyword" placeholder="搜名称 / 地址 / POI ID" clearable
          style="width: 260px" @keyup.enter="load(1)" @clear="load(1)"
        />
        <el-select v-model="query.province" clearable placeholder="省份" style="width: 140px" @change="load(1)">
          <el-option v-for="p in provinces" :key="p.province" :label="`${p.province}（${p.total}）`" :value="p.province" />
        </el-select>
        <el-input v-model="query.city" placeholder="城市" clearable style="width: 120px" @keyup.enter="load(1)" />
        <el-select v-model="query.linked" clearable placeholder="建过景区吗" style="width: 140px" @change="load(1)">
          <el-option label="已建景区" value="yes" />
          <el-option label="还没建" value="no" />
        </el-select>
        <el-button :icon="Search" @click="load(1)">查询</el-button>
        <div class="spacer" />
        <el-button
          type="primary" :disabled="!selected.length" :loading="importing" @click="doImport"
        >
          建为景区{{ selected.length ? `（${selected.length}）` : '' }}
        </el-button>
      </div>

      <el-table :data="items" v-loading="loading" border size="small" @selection-change="onSelect">
        <el-table-column type="selection" width="44" />
        <el-table-column prop="poi_name" label="景区名称" min-width="180">
          <template #default="{ row }">
            {{ row.poi_name }}
            <el-tag v-if="row.scenic_id" size="small" type="success">{{ row.scenic_id }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="scenic_level" label="等级" width="70" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.scenic_level" size="small" type="warning">{{ row.scenic_level }}</el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="地区" width="150">
          <template #default="{ row }">{{ [row.province, row.city_name].filter(Boolean).join(' · ') || '—' }}</template>
        </el-table-column>
        <el-table-column prop="address" label="地址" min-width="200" show-overflow-tooltip />
        <el-table-column prop="poi_id" label="POI ID" width="110" class-name="mono" />
        <el-table-column label="详情" width="90" align="center">
          <template #default="{ row }">
            <el-tooltip v-if="row.detail_status === 'error'" :content="row.detail_error">
              <el-tag size="small" type="danger">失败</el-tag>
            </el-tooltip>
            <el-tag v-else-if="row.detail_status === 'success'" size="small" type="success">已采</el-tag>
            <el-tag v-else-if="row.detail_status === 'unsupported'" size="small" type="info">无</el-tag>
            <el-tag v-else size="small">待采</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="210" align="center">
          <template #default="{ row }">
            <el-button link type="primary" @click="openDetail(row)">详情</el-button>
            <el-button link type="primary" @click="openAttach(row)">挂到景区</el-button>
            <el-button
              link type="primary" :disabled="row.detail_status === 'unsupported'"
              @click="refreshOne(row)"
            >
              重采
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="这个渠道还没有档案——先在上面选区域采一次" :image-size="60" />
        </template>
      </el-table>

      <el-pagination
        v-if="total > query.page_size" small background class="pager"
        layout="total, prev, pager, next" :total="total"
        :current-page="query.page" :page-size="query.page_size" @current-change="load"
      />
    </el-card>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" :title="detailRow?.poi_name || '景区档案'" size="620px">
      <el-descriptions v-if="detailRow" :column="1" border size="small">
        <el-descriptions-item label="渠道">{{ channelLabel(detailRow.channel) }}</el-descriptions-item>
        <el-descriptions-item label="POI ID">
          <span class="mono">{{ detailRow.poi_id }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="等级">{{ detailRow.scenic_level || '—' }}</el-descriptions-item>
        <el-descriptions-item label="地区">
          {{ [detailRow.province, detailRow.city_name].filter(Boolean).join(' · ') || '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="地址">{{ detailRow.address || '—' }}</el-descriptions-item>
        <el-descriptions-item label="电话">{{ detailRow.tel || '—' }}</el-descriptions-item>
        <el-descriptions-item label="开放时间">
          <span class="pre">{{ detailRow.open_time || '—' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="优待政策">
          <span class="pre">{{ detailRow.discount_policy || '—' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="服务设施">
          <span class="pre">{{ detailRow.amenity || '—' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="景区介绍">
          <span class="pre">{{ detailRow.scenic_intro || '—' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="来源">
          <a v-if="detailRow.source_url" :href="detailRow.source_url" target="_blank" rel="noreferrer">
            打开平台原页
          </a>
          <span v-else class="muted">—</span>
        </el-descriptions-item>
      </el-descriptions>
    </el-drawer>

    <!-- 挂到已有景区 -->
    <el-dialog v-model="attachVisible" title="挂到已有景区" width="480px">
      <el-alert type="info" :closable="false" style="margin-bottom: 12px">
        同一个景区在三个平台上各有一个 POI。用这里把它们挂到<b>同一个景区</b>下，
        数据才归在一起；走「建为景区」会各建一个，同一个景区的数据被劈成三份。
      </el-alert>
      <el-select
        v-model="attachScenic" filterable remote :remote-method="searchScenics"
        :loading="searchingScenic" placeholder="输入景区名称搜索" style="width: 100%"
      >
        <el-option
          v-for="s in scenicOptions" :key="s.scenic_id"
          :label="`${s.scenic_name}（${s.scenic_id}）`" :value="s.scenic_id"
        />
      </el-select>
      <template #footer>
        <el-button @click="attachVisible = false">取消</el-button>
        <el-button type="primary" :disabled="!attachScenic" @click="doAttach">挂上去</el-button>
      </template>
    </el-dialog>

    <!-- 手工加区域 -->
    <el-dialog v-model="manualVisible" title="手工添加采集区域" width="480px">
      <el-alert type="info" :closable="false" style="margin-bottom: 12px">
        区域清单是从平台页面上扒的，平台一改版就可能探测不到。
        这里能手填一条，功能就不会被一次改版彻底堵死。<br />
        编号填平台自己的：携程是 districtId，同程是 pid，去哪儿是城市 slug。
      </el-alert>
      <el-form label-width="90px">
        <el-form-item label="区域编号">
          <el-input v-model="manualForm.region_id" placeholder="如 6 / 110000 / guiyang" />
        </el-form-item>
        <el-form-item label="区域名称">
          <el-input v-model="manualForm.region_name" placeholder="如 贵州省" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="manualVisible = false">取消</el-button>
        <el-button type="primary" @click="addManualRegion">添加</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
/**
 * 景区档案。
 *
 * 和「景区管理」的分工：那边管的是**本系统的景区**（业务主数据、关键字、
 * 采集目标）；这边是**平台侧的景区名录**，一个只读的资料库，
 * 唯一的写操作就是把某条建成景区 / 挂到景区上。
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowRight, QuestionFilled, Refresh, Search } from '@element-plus/icons-vue'
import {
  archiveApi, qunarApi, scenicApi,
  type ArchiveChannel, type ArchiveItem, type ArchiveJob, type ArchiveRegion,
  type ArchiveStat,
} from '../api'

const channels = ref<ArchiveChannel[]>([])
const channel = ref('')
const currentChannel = computed(
  () => channels.value.find((c) => c.channel === channel.value) || null,
)
const stats = ref<ArchiveStat[]>([])

function channelLabel(value: string): string {
  return channels.value.find((c) => c.channel === value)?.label || value
}

// ---------------- 区域 ----------------
const regions = ref<ArchiveRegion[]>([])
const selectedRegions = ref<string[]>([])
const loadingRegions = ref(false)
const withDetail = ref(false)
/**
 * 区域按省/市分层显示。
 *
 * 同程带了 34 个省 + 353 个市，一次铺 387 个勾选框没法看，也没法选。
 * 默认按省；广东一个省九百多条，想拆细就切到按市——同程的列表接口
 * 除了 pid 还吃 cid，所以按市采是平台本来就支持的。
 */
const regionLevel = ref<'province' | 'city'>('province')
const regionFilter = ref('')
const hasCityRegions = computed(() => regions.value.some((r) => r.level === 'city'))
const levelCount = (level: string) =>
  regions.value.filter((r) => r.level === level).length
const visibleRegions = computed(() => {
  const wanted = hasCityRegions.value ? regionLevel.value : ''
  const word = regionFilter.value.trim()
  return regions.value.filter(
    (r) => (!wanted || r.level === wanted) && (!word || r.region_name.includes(word)),
  )
})

/**
 * 区域清单展开/收起。
 *
 * 区域多的时候默认收起——同程一来就是 34 个省的勾选框，
 * 下面的档案列表会被整个挤到屏幕外。但只要用户自己点过一次，
 * 后面就听他的：拉一次清单自动合上，会显得像是点了没反应。
 */
const regionsOpen = ref(true)
const foldTouched = ref(false)

function toggleRegions() {
  regionsOpen.value = !regionsOpen.value
  foldTouched.value = true
}

function autoFold() {
  if (foldTouched.value) return
  regionsOpen.value = visibleRegions.value.length <= 20
}

/**
 * 切「按省/按市」之后要重新判断折不折叠。
 *
 * 只在拉清单时算一次是不够的：去哪儿拉回来的时候按省是 0 个
 * （0 ≤ 20 → 展开），一点「按市」变成 393 个，却还是展开的，
 * 整屏全是勾选框。用户自己折过就不再干预。
 */
watch(regionLevel, () => { autoFold() })

/**
 * 全选 / 取消全选，作用范围是**当前可见的**区域。
 *
 * ⚠️ 取消时只摘掉可见的那些，不能直接清空 selectedRegions——
 * 用户可能先按省勾了几个、再切到按市勾几个城市，一次"取消全选"
 * 把另一个视图里的选择也抹掉，他不会知道发生了什么。
 */
const allVisibleSelected = computed({
  get: () => visibleRegions.value.length > 0
    && visibleRegions.value.every((r) => selectedRegions.value.includes(r.region_id)),
  set: (on: boolean) => {
    const ids = visibleRegions.value.map((r) => r.region_id)
    if (on) {
      selectedRegions.value = Array.from(new Set([...selectedRegions.value, ...ids]))
    } else {
      const drop = new Set(ids)
      selectedRegions.value = selectedRegions.value.filter((id) => !drop.has(id))
    }
  },
})

/** 半选状态：可见区域选了一部分。没有它的话，勾了 3/34 个复选框看起来还是空的。 */
const someVisibleSelected = computed(() => {
  const picked = visibleRegions.value.filter(
    (r) => selectedRegions.value.includes(r.region_id)).length
  return picked > 0 && picked < visibleRegions.value.length
})

/** 收起时显示的已选摘要，太多就省略掉尾巴。 */
const selectedNames = computed(() => {
  const byId = new Map(regions.value.map((r) => [r.region_id, r.region_name]))
  const names = selectedRegions.value.map((id) => byId.get(id) || id)
  return names.length > 8
    ? `${names.slice(0, 8).join('、')} 等 ${names.length} 个`
    : names.join('、')
})
const manualVisible = ref(false)
const syncingRegions = ref(false)

async function syncQunarRegions() {
  syncingRegions.value = true
  try {
    const r = await qunarApi.syncRegions()
    ElMessage.success(`行政区划 ${r.saved} 行，其中 ${r.matched} 行接上了去哪儿城市`)
  } finally {
    syncingRegions.value = false
  }
}
const manualForm = reactive({ region_id: '', region_name: '' })

async function loadRegions() {
  regions.value = await archiveApi.regions(channel.value)
  pickDefaultLevel()
  autoFold()
}

/**
 * 默认选**有内容的**那一层。
 *
 * 去哪儿的区域全是市级（它的城市索引本来就是一张扁平名单，没有省），
 * 死守"默认按省"的话，切过去看到的是**一片空白**——用户得自己猜到
 * 要去点「按市（393）」才有东西。省级一个都没有时就直接落到市级。
 */
function pickDefaultLevel() {
  if (!hasCityRegions.value) return
  const provinces = regions.value.filter((r) => r.level === 'province').length
  if (provinces === 0) regionLevel.value = 'city'
}

async function refreshRegions() {
  loadingRegions.value = true
  try {
    const r = await archiveApi.refreshRegions(channel.value)
    ElMessage.success(`区域清单 ${r.saved} 条`)
    await loadRegions()
  } finally {
    loadingRegions.value = false
  }
}

async function addManualRegion() {
  if (!manualForm.region_id.trim() || !manualForm.region_name.trim()) {
    ElMessage.warning('编号和名称都要填')
    return
  }
  await archiveApi.addRegion(channel.value, manualForm.region_id.trim(),
                             manualForm.region_name.trim())
  manualVisible.value = false
  manualForm.region_id = ''
  manualForm.region_name = ''
  await loadRegions()
}

// ---------------- 采集作业 ----------------
const job = ref<ArchiveJob | null>(null)
let timer: ReturnType<typeof setInterval> | null = null

const jobStatusText = computed(() => ({
  running: '采集中', finished: '采集完成', failed: '采集失败', canceled: '已停止',
}[job.value?.status || 'running']))

const progressStatus = computed(() => {
  if (job.value?.status === 'failed') return 'exception'
  if (job.value?.status === 'finished') return 'success'
  return undefined
})

async function startCollect() {
  job.value = await archiveApi.collect({
    channel: channel.value,
    region_ids: selectedRegions.value,
    with_detail: withDetail.value && !!currentChannel.value?.supports_detail,
  })
  ElMessage.success('已开始采集，可以离开这个页面，进度不会丢')
}

async function cancelJob() {
  if (!job.value) return
  await archiveApi.cancelJob(job.value.job_id)
  ElMessage.info('已请求停止，正在收尾')
}

/**
 * 轮询作业进度。
 *
 * 进度存在**服务端**，所以刷新页面、切走再回来都能接着看到——
 * 只存前端的话，用户切个页面回来会以为任务没跑，于是再点一次，
 * 两个作业同时打同一个站点，最可能的结果是两个都被风控。
 */
async function pollJob() {
  const running = (await archiveApi.jobs()).find(
    (j) => j.channel === channel.value,
  )
  if (!running) return
  const before = job.value?.status
  job.value = running
  if (before === 'running' && running.status !== 'running') {
    await Promise.all([load(query.page), loadRegions(), loadStats(), loadProvinces()])
  }
}

// ---------------- 档案列表 ----------------
const items = ref<ArchiveItem[]>([])
const selected = ref<ArchiveItem[]>([])
const total = ref(0)
const loading = ref(false)
const importing = ref(false)
const provinces = ref<{ province: string; total: number }[]>([])
const query = reactive({
  keyword: '', province: '', city: '', linked: '', page: 1, page_size: 20,
})

async function load(page = 1) {
  query.page = page
  loading.value = true
  try {
    const data = await archiveApi.list({ ...query, channel: channel.value })
    items.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

async function loadProvinces() {
  provinces.value = await archiveApi.provinces(channel.value)
}

async function loadStats() {
  stats.value = await archiveApi.stats()
}

function onSelect(rows: ArchiveItem[]) {
  selected.value = rows
}

async function doImport() {
  importing.value = true
  try {
    const r = await archiveApi.importPois(
      channel.value, selected.value.map((x) => x.poi_id))
    ElMessage.success(
      `新建景区 ${r.created} 个，更新 ${r.updated} 个`
      + (r.failed?.length ? `，失败 ${r.failed.length} 个` : ''))
    await Promise.all([load(query.page), loadStats()])
  } finally {
    importing.value = false
  }
}

async function refreshOne(row: ArchiveItem) {
  const fresh = await archiveApi.refreshOne(row.channel, row.poi_id)
  if (fresh) Object.assign(row, fresh)
  ElMessage.success('已重新采集')
}

// ---------------- 详情 / 挂景区 ----------------
const detailVisible = ref(false)
const detailRow = ref<ArchiveItem | null>(null)

async function openDetail(row: ArchiveItem) {
  // 列表接口**不返回**介绍/政策/设施三个大字段（LONGTEXT，一页能有几百 KB），
  // 所以打开抽屉时单独取一次完整的这一条
  detailRow.value = await archiveApi.detail(row.channel, row.poi_id)
  detailVisible.value = true
}

const attachVisible = ref(false)
const attachRow = ref<ArchiveItem | null>(null)
const attachScenic = ref('')
const scenicOptions = ref<{ scenic_id: string; scenic_name: string }[]>([])
const searchingScenic = ref(false)

function openAttach(row: ArchiveItem) {
  attachRow.value = row
  attachScenic.value = ''
  scenicOptions.value = []
  attachVisible.value = true
  void searchScenics('')
}

async function searchScenics(keyword: string) {
  searchingScenic.value = true
  try {
    const data = await scenicApi.list({ keyword, page: 1, page_size: 30 })
    scenicOptions.value = data.items.map(
      (s) => ({ scenic_id: s.scenic_id, scenic_name: s.scenic_name }))
  } finally {
    searchingScenic.value = false
  }
}

async function doAttach() {
  if (!attachRow.value || !attachScenic.value) return
  await archiveApi.attach(
    attachRow.value.channel, attachRow.value.poi_id, attachScenic.value)
  attachVisible.value = false
  ElMessage.success('已挂上去')
  await Promise.all([load(query.page), loadStats()])
}

// ---------------- 生命周期 ----------------
async function onChannelChange() {
  selected.value = []
  selectedRegions.value = []
  regionLevel.value = 'province'
  regionFilter.value = ''
  foldTouched.value = false
  job.value = null
  query.province = ''
  await Promise.all([loadRegions(), loadProvinces(), load(1), pollJob()])
}

onMounted(async () => {
  channels.value = await archiveApi.channels()
  channel.value = channels.value[0]?.channel || ''
  await Promise.all([loadStats(), onChannelChange()])
  timer = setInterval(() => { void pollJob() }, 3000)
})

onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.archive-page { display: flex; flex-direction: column; gap: 12px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; flex-wrap: wrap; }
.page-head h2 { margin: 0 0 4px; }
.page-head p { margin: 0; max-width: 720px; line-height: 1.6; }
.stat-row { display: flex; gap: 10px; }
.stat-card { min-width: 110px; padding: 10px 14px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; }
.stat-name { font-size: 12px; color: var(--el-text-color-secondary); }
.stat-num { font-size: 22px; font-weight: 600; line-height: 1.2; }
.ch-tabs { margin-bottom: -8px; }
.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.spacer { flex: 1; }
.region-grid { display: flex; flex-wrap: wrap; gap: 6px 18px; }
.region-title { cursor: pointer; user-select: none; display: inline-flex; align-items: center; gap: 4px; }
.region-title:hover { color: var(--el-color-primary); }
.fold-icon { transition: transform 0.2s; }
.fold-icon.is-open { transform: rotate(90deg); }
.region-folded { font-size: 12px; line-height: 1.7; }
.job-card.is-failed { border-color: var(--el-color-danger-light-5); }
.job-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.job-nums { margin-top: 6px; font-size: 12px; }
.job-error { margin-top: 6px; color: var(--el-color-danger); font-size: 12px; }
.pager { margin-top: 10px; justify-content: flex-end; }
/* 开放时间、优待政策、服务设施在平台页面上本来就是分行的，
   不保留换行会糊成一坨长句子 */
.pre { white-space: pre-wrap; word-break: break-word; }
</style>
