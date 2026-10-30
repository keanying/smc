<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!-- 主：登记景区盒子、分景区、发令牌。盒子采到的数据报到这里存储和标注 -->
  <div>
    <div class="page-header">
      <h2 class="page-title">
        盒子管理
        <InfoTip :width="360">
          景区盒子在景区现场采集，采到的数据直接报给这里存储和标注；盒子上的数据中心、标注审核也是查这里，只看得到分给它的景区。<br>
          在这里登记盒子、勾选景区，把令牌配到盒子的 config.yaml 里，盒子每分钟来同步一次。
        </InfoTip>
      </h2>
      <div>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openEdit()">登记盒子</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="boxes" class="data-table">
      <el-table-column label="盒子" min-width="200">
        <template #default="{ row }">
          <div class="box-cell">
            <span class="dot" :class="!row.enabled ? 'off' : row.online ? 'ok' : 'bad'" />
            <div>
              <div class="box-name">{{ row.box_name }}</div>
              <div class="muted mono small">{{ row.box_id }}</div>
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="分到的景区" min-width="240">
        <template #default="{ row }">
          <span v-if="!row.scenics.length" class="muted">还没分</span>
          <el-tag v-for="s in row.scenics" :key="s.scenic_id" size="small" class="scenic-tag">
            {{ s.scenic_name }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="150">
        <template #default="{ row }">
          <span v-if="!row.enabled" class="muted">已停用</span>
          <span v-else-if="row.online" class="ok-text">在线</span>
          <span v-else class="muted">{{ row.last_seen_at ? `离线 · ${relTime(row.last_seen_at)}` : '还没连上过' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="累计上报" width="170">
        <template #default="{ row }">
          作品 {{ formatCount(row.uploaded_works) }} · 评论 {{ formatCount(row.uploaded_comments) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="230" align="center">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="primary" @click="toggle(row)">{{ row.enabled ? '停用' : '启用' }}</el-button>
          <el-button link type="primary" @click="resetToken(row)">重置令牌</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <div class="empty">还没有登记盒子。点右上角「登记盒子」，把发的令牌配到盒子上。</div>
      </template>
    </el-table>

    <!-- 登记 / 编辑 -->
    <el-dialog v-model="editVisible" :title="editing ? '编辑盒子' : '登记盒子'" width="560px">
      <el-form label-width="84px">
        <el-form-item label="名字">
          <el-input v-model="form.box_name" placeholder="如：A景区 1号盒子" maxlength="100" />
        </el-form-item>
        <el-form-item label="景区">
          <el-select v-model="form.scenic_ids" multiple filterable collapse-tags collapse-tags-tooltip
                     :max-collapse-tags="4" placeholder="这台盒子负责采哪些景区" style="width: 100%">
            <el-option v-for="s in scenicOptions" :key="s.scenic_id"
                       :label="`${s.scenic_name}（${s.scenic_id}）`" :value="s.scenic_id" />
          </el-select>
        </el-form-item>
      </el-form>
      <p class="muted small">盒子每分钟同步一次：景区连同关键字、附加 / 过滤词、采集目标都跟着这里走；去掉的景区在盒子上停用。</p>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ editing ? '保存' : '登记并生成令牌' }}</el-button>
      </template>
    </el-dialog>

    <!-- 令牌只显示一次 -->
    <el-dialog v-model="tokenVisible" title="盒子令牌" width="620px" :close-on-click-modal="false">
      <el-alert type="warning" :closable="false" show-icon
                title="令牌只显示这一次，关掉就看不到了（丢了可以重置）。" />
      <p class="token-intro">把下面这段写进盒子的 <span class="mono">config/config.yaml</span>，重启盒子：</p>
      <pre class="token-block">{{ configSnippet }}</pre>
      <template #footer>
        <el-button @click="copySnippet">复制</el-button>
        <el-button type="primary" @click="tokenVisible = false">我已配好</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { boxesApi, scenicApi, type ScenicBox, type ScenicOption } from '../api'
import { formatCount } from '../constants'

const loading = ref(false)
const saving = ref(false)
const boxes = ref<ScenicBox[]>([])
const scenicOptions = ref<ScenicOption[]>([])
const editVisible = ref(false)
const editing = ref<ScenicBox | null>(null)
const form = reactive({ box_name: '', scenic_ids: [] as string[] })
const tokenVisible = ref(false)
const token = ref('')

/** 盒子要连的主地址：就是现在浏览器里打开的这个 */
const configSnippet = computed(() => [
  'edition:',
  '  name: scenic',
  `  master_url: ${window.location.origin}`,
  `  box_token: ${token.value}`,
].join('\n'))

async function load() {
  loading.value = true
  try {
    boxes.value = await boxesApi.list()
  } finally {
    loading.value = false
  }
}

async function openEdit(row?: ScenicBox) {
  scenicOptions.value = await scenicApi.options(false)
  editing.value = row || null
  form.box_name = row?.box_name || ''
  form.scenic_ids = [...(row?.scenic_ids || [])]
  editVisible.value = true
}

async function save() {
  if (!form.box_name.trim()) {
    ElMessage.warning('给盒子起个名字')
    return
  }
  saving.value = true
  try {
    const payload = { box_name: form.box_name.trim(), scenic_ids: form.scenic_ids }
    if (editing.value) {
      await boxesApi.update(editing.value.box_id, payload)
      ElMessage.success('已保存，盒子一分钟内同步')
    } else {
      showToken((await boxesApi.create(payload)).token)
    }
    editVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

function showToken(value: string) {
  token.value = value
  tokenVisible.value = true
}

async function toggle(row: ScenicBox) {
  if (row.enabled) {
    await ElMessageBox.confirm(`停用「${row.box_name}」？它的令牌立刻失效，采到的数据报不上来。`, '停用', { type: 'warning' })
  }
  await boxesApi.update(row.box_id, { enabled: !row.enabled })
  await load()
}

async function resetToken(row: ScenicBox) {
  await ElMessageBox.confirm(`重置「${row.box_name}」的令牌？旧令牌立刻失效，要把新的配到盒子上。`, '重置令牌', { type: 'warning' })
  showToken((await boxesApi.resetToken(row.box_id)).token)
}

async function remove(row: ScenicBox) {
  await ElMessageBox.confirm(`删除「${row.box_name}」？它上报过的数据还在。`, '删除', { type: 'warning' })
  await boxesApi.remove(row.box_id)
  await load()
}

async function copySnippet() {
  try {
    await navigator.clipboard.writeText(configSnippet.value)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('浏览器不让复制，请手动选中复制')
  }
}

function relTime(value: string): string {
  const t = new Date(String(value).replace(' ', 'T')).getTime()
  if (Number.isNaN(t)) return ''
  const diff = Math.max(0, (Date.now() - t) / 1000)
  if (diff < 3600) return `${Math.max(1, Math.floor(diff / 60))} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

onMounted(load)
</script>

<style scoped>
.box-cell { display: flex; align-items: center; gap: 10px; }
.box-name { font-weight: 600; }
.small { font-size: 12px; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.dot.ok { background: #22c55e; }
.dot.bad { background: #f59e0b; }
.dot.off { background: #c0c4cc; }
.ok-text { color: #16a34a; }
.scenic-tag { margin: 2px 6px 2px 0; }
.empty { padding: 24px 0; color: var(--smc-text-secondary); }
.token-intro { margin: 14px 0 8px; font-size: 13px; }
.token-block {
  margin: 0; padding: 14px 16px; border-radius: 12px; background: #1d1f26; color: #e8eaf0;
  font-size: 13px; line-height: 1.7; white-space: pre-wrap; word-break: break-all;
}
</style>
