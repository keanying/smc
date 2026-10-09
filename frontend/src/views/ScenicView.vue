<template>
  <div>
    <div class="page-header">
      <h2 class="page-title">
        景区管理
        <InfoTip content="先建景区，再挂关键字和各平台采集目标；任务按景区维度组织数据。点行首箭头展开配置。" />
      </h2>
      <div>
        <el-button :icon="Upload" @click="csvVisible = true">CSV 导入</el-button>
        <el-button type="primary" :icon="Plus" @click="openScenicDialog()">新建景区</el-button>
      </div>
    </div>

    <div class="toolbar">
      <el-input
        v-model="query.keyword" placeholder="搜索景区ID或名称" clearable
        style="width: 260px" :prefix-icon="Search" @keyup.enter="loadScenics" @clear="loadScenics"
      />
      <el-button @click="loadScenics">查询</el-button>
      <el-checkbox v-model="query.enabled_only" @change="loadScenics">只看启用</el-checkbox>
    </div>

    <el-table :data="scenics" v-loading="loading" class="data-table" max-height="calc(100vh - 290px)">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="expand-wrap">
            <ScenicDetail :scenic-id="row.scenic_id" :channels="channels" />
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="scenic_id" label="景区ID" width="170" />
      <el-table-column prop="scenic_name" label="景区名称" min-width="180" />
      <el-table-column label="地区" width="140">
        <template #default="{ row }">
          <span class="muted">{{ [row.province, row.city].filter(Boolean).join(' / ') || '-' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="关键字" width="90" align="center">
        <template #default="{ row }">
          <el-tag :type="row.keyword_count ? 'success' : 'info'" size="small">
            {{ row.keyword_count || 0 }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="采集目标" width="100" align="center">
        <template #default="{ row }">
          <el-tag :type="row.target_count ? 'success' : 'info'" size="small">
            {{ row.target_count || 0 }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90" align="center">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
            {{ row.enabled ? '启用' : '停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120" align="center">
        <template #default="{ row }">
          <div class="row-actions">
            <IconAction icon="edit" tip="编辑" @click="openScenicDialog(row)" />
            <IconAction icon="task" tip="建任务" @click="goCreateTask(row)" />
            <el-popconfirm
              title="删除景区会一并删除其关键字和采集目标，已采集的数据保留。确定吗？"
              width="280" @confirm="removeScenic(row)"
            >
              <template #reference><span><IconAction icon="delete" tip="删除" /></span></template>
            </el-popconfirm>
          </div>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="还没有景区" :image-size="80" />
      </template>
    </el-table>

    <el-pagination
      v-if="total > query.page_size"
      style="margin-top: 16px; justify-content: flex-end"
      layout="total, prev, pager, next"
      :total="total" :page-size="query.page_size" :current-page="query.page"
      @current-change="(p: number) => { query.page = p; loadScenics() }"
    />

    <!-- 新建/编辑景区 -->
    <el-dialog v-model="scenicDialog" :title="form.id ? '编辑景区' : '新建景区'" width="480px">
      <el-form :model="form" label-width="90px">
        <el-form-item label="景区ID" required>
          <el-input v-model="form.scenic_id" :disabled="!!form.id" placeholder="业务侧唯一标识，如 SC001" />
        </el-form-item>
        <el-form-item label="景区名称" required>
          <el-input v-model="form.scenic_name" placeholder="如 西湖风景区" />
        </el-form-item>
        <el-form-item label="省份"><el-input v-model="form.province" /></el-form-item>
        <el-form-item label="城市"><el-input v-model="form.city" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="form.remark" type="textarea" :rows="2" /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="form.enabledBool" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="scenicDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveScenic">保存</el-button>
      </template>
    </el-dialog>

    <!-- CSV 导入 -->
    <el-dialog v-model="csvVisible" title="从 CSV 批量导入景区" width="520px">
      <div class="csv-head">
        <span class="muted">CSV 格式</span>
        <InfoTip :width="340">
          必填列：<b>scenic_id</b>、<b>scenic_name</b>（中文表头 景区ID、景区名称 也认）<br />
          可选列：省份、城市、备注<br />
          <span class="mono">景区ID,景区名称,省份,城市<br />SC001,西湖风景区,浙江,杭州</span>
        </InfoTip>
      </div>
      <el-upload
        drag :auto-upload="false" :limit="1" accept=".csv"
        :on-change="onCsvChange" :file-list="csvFiles"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">拖到此处或<em>点击选择</em> CSV 文件</div>
      </el-upload>
      <template #footer>
        <el-button @click="csvVisible = false">取消</el-button>
        <el-button type="primary" :loading="importing" :disabled="!csvFile" @click="importCsv">
          开始导入
        </el-button>
      </template>
    </el-dialog>

  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Plus, Search, Upload, UploadFilled } from '@element-plus/icons-vue'
import { scenicApi, type ChannelOption, type Scenic } from '../api'
import ScenicDetail from '../components/ScenicDetail.vue'

const router = useRouter()
const loading = ref(false)
const saving = ref(false)
const importing = ref(false)
const scenics = ref<Scenic[]>([])
const total = ref(0)
const channels = ref<ChannelOption[]>([])

const query = reactive({ keyword: '', enabled_only: false, page: 1, page_size: 20 })

const scenicDialog = ref(false)
const form = reactive({
  id: 0, scenic_id: '', scenic_name: '',
  province: '', city: '', remark: '', enabledBool: true,
})

const csvVisible = ref(false)
const csvFiles = ref<any[]>([])
const csvFile = ref<File | null>(null)

async function loadScenics() {
  loading.value = true
  try {
    const result = await scenicApi.list(query)
    scenics.value = result.items
    total.value = result.total
  } finally {
    loading.value = false
  }
}

function openScenicDialog(row?: Scenic) {
  if (row) {
    Object.assign(form, {
      id: row.id, scenic_id: row.scenic_id, scenic_name: row.scenic_name,
      province: row.province || '', city: row.city || '',
      remark: row.remark || '', enabledBool: !!row.enabled,
    })
  } else {
    Object.assign(form, {
      id: 0, scenic_id: '', scenic_name: '',
      province: '', city: '', remark: '', enabledBool: true,
    })
  }
  scenicDialog.value = true
}

async function saveScenic() {
  if (!form.scenic_id.trim() || !form.scenic_name.trim()) {
    ElMessage.warning('景区ID和景区名称都要填')
    return
  }
  saving.value = true
  try {
    await scenicApi.save({
      scenic_id: form.scenic_id.trim(),
      scenic_name: form.scenic_name.trim(),
      province: form.province || null,
      city: form.city || null,
      remark: form.remark || null,
      enabled: form.enabledBool ? 1 : 0,
    })
    ElMessage.success('已保存')
    scenicDialog.value = false
    await loadScenics()
  } finally {
    saving.value = false
  }
}

async function removeScenic(row: Scenic) {
  await scenicApi.remove(row.scenic_id)
  ElMessage.success('已删除')
  await loadScenics()
}

function goCreateTask(row: Scenic) {
  router.push({ name: 'task-new', query: { scenic_id: row.scenic_id } })
}

function onCsvChange(file: any) {
  csvFile.value = file.raw
  csvFiles.value = [file]
}

async function importCsv() {
  if (!csvFile.value) return
  importing.value = true
  try {
    const result = await scenicApi.importCsv(csvFile.value)
    ElMessage.success(`导入完成：新增 ${result.created} 个，更新 ${result.updated} 个`)
    if (result.errors?.length) {
      ElMessage.warning(`有 ${result.errors.length} 行被跳过：${result.errors[0]}`)
    }
    csvVisible.value = false
    csvFiles.value = []
    csvFile.value = null
    await loadScenics()
  } finally {
    importing.value = false
  }
}

onMounted(async () => {
  channels.value = await scenicApi.channels()
  await loadScenics()
})
</script>

<style scoped>
.expand-wrap { padding: 8px 24px 16px; }
.row-actions { display: inline-flex; align-items: center; gap: 2px; }
.row-actions > span { display: inline-flex; }
.csv-head { display: flex; align-items: center; margin-bottom: 12px; font-size: 13px; }
</style>
