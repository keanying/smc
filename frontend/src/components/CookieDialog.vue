<template>
  <el-dialog
    v-model="visible" width="760px" destroy-on-close
    :close-on-click-modal="false" @open="onOpen"
  >
    <template #header>
      <span>Cookie — {{ channelLabel(channel) }} / {{ accountName }}</span>
    </template>

    <!-- 当前状态 -->
    <el-card shadow="never" style="margin-bottom: 14px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>当前登录态</span>
          <el-button link :icon="Refresh" :loading="loadingState" @click="loadState">刷新</el-button>
        </div>
      </template>

      <div v-if="state" class="cookie-state">
        <el-tag :type="state.logged_in ? 'success' : 'danger'" effect="dark">
          {{ state.logged_in ? '已登录' : '未登录' }}
        </el-tag>
        <span>共 <b>{{ state.count }}</b> 个 Cookie</span>
        <span class="muted">更新于 {{ formatTime(state.cookie_updated_at) }}</span>
      </div>

      <el-alert
        v-if="state && !state.logged_in" type="error" :closable="false" show-icon
        style="margin-top: 10px"
        title="这些 Cookie 里没有登录凭据"
      >
        <div>
          需要下面任意一个：
          <el-tag
            v-for="name in state.expected_any_of" :key="name"
            size="small" class="mono" style="margin: 2px 4px 2px 0"
          >{{ name }}</el-tag>
        </div>
        <div style="margin-top: 6px">
          只有游客 Cookie 的话，抖音会返回「请先登录，再继续搜索吧」。
          用下面的方式补一份真正的登录 Cookie 即可。
        </div>
      </el-alert>

      <el-collapse v-if="state?.names.length" style="margin-top: 8px">
        <el-collapse-item :title="`查看全部 ${state.names.length} 个 Cookie 名`">
          <el-tag
            v-for="name in state.names" :key="name" size="small"
            :type="state.expected_any_of.includes(name) ? 'success' : 'info'"
            effect="plain" class="mono" style="margin: 2px 4px 2px 0"
          >{{ name }}</el-tag>
          <div class="muted" style="font-size: 12px; margin-top: 6px">
            绿色的是登录凭据。这里只显示名字，不显示值。
          </div>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <!-- 导入 -->
    <el-card shadow="never">
      <template #header><span>粘贴导入</span></template>

      <ol class="cookie-howto">
        <li>
          在你自己的浏览器里登录
          <el-link v-if="state?.home_url" type="primary" :href="state.home_url" target="_blank">
            {{ state.home_url }}
          </el-link>
        </li>
        <li>用 Cookie-Editor 插件点 <b>Export</b>（JSON 格式，推荐——带域名和过期时间）</li>
        <li>粘到下面，点导入</li>
      </ol>

      <el-input
        v-model="raw" type="textarea" :rows="7"
        placeholder='两种格式都认：&#10;[{"name":"sessionid","value":"...","domain":".douyin.com",...}]&#10;或&#10;sessionid=xxx; sid_tt=yyy; uid_tt=zzz'
        class="mono"
      />
      <div class="muted" style="font-size: 12px; margin-top: 6px">
        导入的 Cookie 会同时写进这个账号的浏览器 profile ——
        抖音和快手采集时要开真实页面算签名，只存数据库那边还是未登录状态。
      </div>

      <el-alert
        v-if="importResult" :closable="false" show-icon style="margin-top: 10px"
        :type="importResult.profile_warning ? 'warning' : 'success'"
        :title="importResult.profile_warning
          || `导入成功：${importResult.count} 个 Cookie（${importResult.format === 'json' ? 'JSON' : '请求头串'} 格式），已写入浏览器 profile`"
      />
    </el-card>

    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button :loading="harvesting" @click="harvest">从浏览器采集</el-button>
      <el-button type="primary" :loading="importing" :disabled="!raw.trim()" @click="doImport">
        导入 Cookie
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { accountApi, type CookieImportResult, type CookieInspect } from '../api'
import { channelLabel, formatTime } from '../constants'

const props = defineProps<{
  modelValue: boolean
  channel: string
  accountName: string
}>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  changed: []
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const state = ref<CookieInspect | null>(null)
const raw = ref('')
const importResult = ref<CookieImportResult | null>(null)
const loadingState = ref(false)
const importing = ref(false)
const harvesting = ref(false)

async function loadState() {
  loadingState.value = true
  try {
    state.value = await accountApi.inspectCookies(props.channel, props.accountName)
  } finally {
    loadingState.value = false
  }
}

function onOpen() {
  raw.value = ''
  importResult.value = null
  state.value = null
  loadState()
}

async function doImport() {
  importing.value = true
  try {
    importResult.value = await accountApi.importCookies(
      props.channel, props.accountName, raw.value,
    )
    raw.value = ''
    await loadState()
    emit('changed')
    if (!importResult.value.profile_warning) {
      ElMessage.success(`已导入 ${importResult.value.count} 个 Cookie`)
    }
  } finally {
    importing.value = false
  }
}

async function harvest() {
  harvesting.value = true
  try {
    const result = await accountApi.harvest(props.channel, props.accountName)
    if (result.logged_in) ElMessage.success(result.message)
    else ElMessage.warning(result.message)
    await loadState()
    emit('changed')
  } finally {
    harvesting.value = false
  }
}
</script>

<style scoped>
.cookie-state {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.cookie-howto {
  margin: 0 0 10px;
  padding-left: 20px;
  font-size: 13px;
  line-height: 1.9;
  color: #606266;
}
</style>
