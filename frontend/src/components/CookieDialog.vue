<template>
  <el-dialog
    v-model="visible" width="720px" destroy-on-close
    :close-on-click-modal="false" @open="onOpen"
  >
    <template #header>
      <span class="el-dialog__title">Cookie</span>
      <span class="dialog-sub">{{ channelLabel(channel) }} / {{ accountName }}</span>
    </template>

    <!-- 当前状态 -->
    <div class="section-title">
      当前登录态
      <el-button link :icon="Refresh" :loading="loadingState" class="section-action" @click="loadState">
        刷新
      </el-button>
    </div>
    <div class="cookie-block">
      <div v-if="state" class="cookie-state">
        <el-tag :type="state.logged_in ? 'success' : 'danger'" disable-transitions>
          {{ state.logged_in ? '已登录' : '未登录' }}
        </el-tag>
        <span>共 <b>{{ state.count }}</b> 个 Cookie</span>
        <span class="muted">更新于 {{ formatTime(state.cookie_updated_at) }}</span>
      </div>

      <el-alert
        v-if="state && !state.logged_in" type="error" :closable="false" show-icon
        style="margin-top: 12px"
      >
        <template #title>
          缺少登录凭据
          <InfoTip
            content="只有游客 Cookie 的话，抖音会返回「请先登录，再继续搜索吧」。用下面的粘贴导入补一份真正的登录 Cookie 即可。"
          />
        </template>
        <div class="expected-names">
          需要以下任意一个：
          <el-tag
            v-for="name in state.expected_any_of" :key="name"
            size="small" class="mono" disable-transitions
          >{{ name }}</el-tag>
        </div>
      </el-alert>

      <el-collapse v-if="state?.names.length" class="cookie-names">
        <el-collapse-item>
          <template #title>
            全部 {{ state.names.length }} 个 Cookie 名
            <InfoTip content="绿色的是登录凭据。这里只显示名字，不显示值。" />
          </template>
          <div class="name-tags">
            <el-tag
              v-for="name in state.names" :key="name" size="small"
              :type="state.expected_any_of.includes(name) ? 'success' : 'info'"
              effect="plain" class="mono" disable-transitions
            >{{ name }}</el-tag>
          </div>
        </el-collapse-item>
      </el-collapse>
    </div>

    <!-- 导入 -->
    <div class="section-title" style="margin-top: 20px">
      粘贴导入
      <InfoTip :width="360">
        1. 在你自己的浏览器里登录该平台<br>
        2. 用 Cookie-Editor 插件点 <b>Export</b>（JSON 格式，推荐——带域名和过期时间）<br>
        3. 粘到下面，点「导入 Cookie」<br>
        导入的 Cookie 会同时写进这个账号的浏览器 profile——抖音和快手采集时要开真实页面算签名，只存数据库那边还是未登录状态。
      </InfoTip>
      <el-link
        v-if="state?.home_url" type="primary" :href="state.home_url" target="_blank"
        :underline="false" class="section-action"
      >
        打开登录页
      </el-link>
    </div>

    <el-input
      v-model="raw" type="textarea" :rows="7"
      placeholder='两种格式都认：&#10;[{"name":"sessionid","value":"...","domain":".douyin.com",...}]&#10;或&#10;sessionid=xxx; sid_tt=yyy; uid_tt=zzz'
      class="mono"
    />

    <el-alert
      v-if="importResult" :closable="false" show-icon style="margin-top: 12px"
      :type="importResult.profile_warning ? 'warning' : 'success'"
      :title="importResult.profile_warning
        || `已导入 ${importResult.count} 个 Cookie（${importResult.format === 'json' ? 'JSON' : '请求头串'}），已写入浏览器 profile`"
    />

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
.dialog-sub {
  margin-left: 10px;
  font-size: 13px;
  color: var(--smc-text-secondary);
}
.section-action { margin-left: auto; font-weight: 400; }
.cookie-block {
  padding: 14px 16px;
  border: 1px solid var(--smc-border);
  border-radius: var(--smc-radius-sm);
}
.cookie-state {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 13px;
}
.expected-names {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 2px;
}
.cookie-names {
  margin-top: 10px;
  border-top: none;
  border-bottom: none;
}
.cookie-names :deep(.el-collapse-item__header) {
  height: 32px;
  border-bottom: none;
  font-size: 13px;
  color: #5b6375;
}
.cookie-names :deep(.el-collapse-item__wrap) { border-bottom: none; }
.cookie-names :deep(.el-collapse-item__content) { padding-bottom: 4px; }
.name-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
</style>
