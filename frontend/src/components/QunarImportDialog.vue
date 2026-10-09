<template>
  <el-dialog
    :model-value="modelValue" title="从去哪儿导入景区" width="880px" top="6vh"
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <!-- 景区 ID 和采集目标都从去哪儿页面上抓，不用手工填 POI ID：手填最常见的两个问题
         是名字打错和 ID 少复制一位，而这两个都要等到任务跑出 0 条才会发现。 -->
    <div class="toolbar">
      <el-select
        v-model="province" clearable filterable placeholder="省份（可不选）"
        style="width: 150px" @change="onProvinceChange"
      >
        <el-option
          v-for="p in provinces" :key="p.province"
          :label="`${p.province}（${p.cities.length}）`" :value="p.province"
        />
        <template #empty>
          <div class="sync-hint">
            还没有行政区划数据
            <el-button link type="primary" :loading="syncing" @click="syncRegions">
              同步一次
            </el-button>
          </div>
        </template>
      </el-select>
      <el-select
        v-model="city" filterable placeholder="选城市（可输入拼音搜索）"
        style="width: 240px" :loading="loadingCities" @change="loadPois(1)"
      >
        <el-option
          v-for="c in visibleCities" :key="c.city_id"
          :label="`${c.city_name}（${c.city_id}）`" :value="c.city_id"
        />
      </el-select>
      <el-button :disabled="!city" :loading="loadingPois" @click="loadPois(1)">
        查询
      </el-button>
      <InfoTip content="选城市 → 勾景区 → 导入。景区 ID 和采集目标直接从去哪儿页面抓取，不用手填 POI ID。" />
      <div class="spacer" />
      <el-checkbox v-model="withDetail">同时抓详细档案</el-checkbox>
      <InfoTip content="逐个进景区详情页取开放时间 / 电话 / 介绍 / 优待政策 / 服务设施，每个景区多一次请求，慢但档案全。不勾也能建景区和跑采集，档案之后随时可刷。" />
    </div>

    <el-table
      ref="tableRef" :data="pois" v-loading="loadingPois" height="380"
      size="small" @selection-change="onSelect"
    >
      <el-table-column type="selection" width="44" />
      <el-table-column prop="poi_name" label="景区名称" min-width="190">
        <template #default="{ row }">
          {{ row.poi_name }}
          <el-tag v-if="imported.has(row.poi_id)" size="small" type="info">已导入</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="scenic_level" label="评级" width="72" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.scenic_level" size="small" type="warning">
            {{ row.scenic_level }}
          </el-tag>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="address" label="地址" min-width="220" show-overflow-tooltip />
      <el-table-column prop="poi_id" label="去哪儿 POI" width="130" />
      <template #empty>
        <el-empty :description="city ? '这一页没有景区' : '先选一个城市'" :image-size="60" />
      </template>
    </el-table>

    <div class="foot-bar">
      <el-pagination
        v-if="totalPages > 1" small background layout="prev, pager, next"
        :current-page="page" :page-count="totalPages" @current-change="loadPois"
      />
      <div class="spacer" />
      <span class="muted">
        已勾选 {{ selected.length }} 个<template v-if="selectedNew < selected.length">
          （其中 {{ selected.length - selectedNew }} 个已导入过，会覆盖更新）</template>
      </span>
    </div>

    <template #footer>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button
        type="primary" :disabled="!selected.length" :loading="importing"
        @click="doImport"
      >
        导入 {{ selected.length ? `${selected.length} 个景区` : '' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
/**
 * 从去哪儿导入景区。
 *
 * 只做"浏览 + 勾选 + 导入"三件事，不在这里跑采集——导入完成后，
 * 景区和采集目标都已经建好，去任务管理建任务就能直接跑。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  qunarApi, type QunarCity, type QunarPoi, type QunarProvince,
} from '../api'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'imported'): void
}>()

const cities = ref<QunarCity[]>([])
const pois = ref<QunarPoi[]>([])
const selected = ref<QunarPoi[]>([])
const city = ref('')
const cityName = ref('')
const page = ref(1)
const totalPages = ref(1)
const withDetail = ref(false)
const loadingCities = ref(false)
const loadingPois = ref(false)
const importing = ref(false)

/**
 * 省份筛选。去哪儿的城市索引是**一张扁平的名单**（三百多个城市，只有城市名），
 * 省份是另外一份行政区划数据按名字接上去的，所以它可能是空的——
 * 空的时候下拉里给一个「同步一次」的入口，而不是让用户对着空列表发呆。
 */
const provinces = ref<QunarProvince[]>([])
const province = ref('')
const syncing = ref(false)

/** 选了省就只列这个省的城市；没选省（或压根没同步过）就列全部。 */
const visibleCities = computed(() => {
  if (!province.value) return cities.value
  const hit = provinces.value.find((p) => p.province === province.value)
  if (!hit) return cities.value
  const allow = new Set(hit.cities.map((c) => c.city_id))
  return cities.value.filter((c) => allow.has(c.city_id))
})

/**
 * 这一页里哪些景区已经导过。
 *
 * 不是为了禁止重复导入——重复导入是安全的（景区ID固定成 QN+POI ID，
 * 命中同一条直接更新）。标出来只是让用户知道勾它会发生什么，
 * 否则"新建 0 个，更新 12 个"这个提示会让人以为哪里出错了。
 */
const imported = ref<Set<string>>(new Set())
const selectedNew = computed(
  () => selected.value.filter((p) => !imported.value.has(p.poi_id)).length,
)

async function loadProvinces() {
  provinces.value = await qunarApi.provinces()
}

async function syncRegions() {
  syncing.value = true
  try {
    const r = await qunarApi.syncRegions()
    ElMessage.success(`行政区划 ${r.saved} 行，其中 ${r.matched} 行接上了去哪儿城市`)
    await loadProvinces()
  } finally {
    syncing.value = false
  }
}

function onProvinceChange() {
  // 换省之后原来选的城市多半不在这个省里了，留着它会让列表和下拉对不上
  if (city.value && !visibleCities.value.some((c) => c.city_id === city.value)) {
    city.value = ''
    pois.value = []
  }
}

async function loadCities() {
  if (cities.value.length) return
  loadingCities.value = true
  try {
    cities.value = await qunarApi.cities()
  } finally {
    loadingCities.value = false
  }
}

async function loadPois(target = 1) {
  if (!city.value) return
  loadingPois.value = true
  try {
    const data = await qunarApi.pois(city.value, target)
    pois.value = data.items
    cityName.value = data.city_name
    page.value = data.page
    totalPages.value = data.total_pages
    // 只查这一页出现的这些景区导没导过，不是把整张档案表拉回来
    const saved = await qunarApi.saved({ city: city.value, page_size: 200 })
    imported.value = new Set(saved.items.map((x) => x.poi_id))
  } finally {
    loadingPois.value = false
  }
}

function onSelect(rows: QunarPoi[]) {
  selected.value = rows
}

async function doImport() {
  importing.value = true
  try {
    const result = await qunarApi.importPois({
      items: selected.value,
      city_id: city.value,
      city_name: cityName.value,
      with_detail: withDetail.value,
    })
    ElMessage.success(
      `新建 ${result.created} 个，更新 ${result.updated} 个`
      + (result.failed?.length ? `，失败 ${result.failed.length} 个` : ''),
    )
    emit('imported')
    emit('update:modelValue', false)
  } finally {
    importing.value = false
  }
}

// 打开时才拉城市列表：这一下要真的去请求去哪儿，
// 页面一加载就拉的话，用户没打算导入也白跑一次请求
watch(() => props.modelValue, (open) => {
  if (!open) return
  void loadCities()
  void loadProvinces()
})
</script>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
.foot-bar { display: flex; align-items: center; gap: 10px; margin-top: 12px; }
.spacer { flex: 1; }
.foot-bar .muted { font-size: 12px; }
.sync-hint { padding: 10px 14px; color: var(--smc-text-secondary); font-size: 12px; }
</style>
