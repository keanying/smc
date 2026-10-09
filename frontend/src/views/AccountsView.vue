<template>
  <div>
    <div class="page-header">
      <h2 class="page-title">
        账号管理
        <InfoTip
          content="每个账号一个独立的浏览器 profile，登录一次后采集任务静默复用登录态；同组账号自动轮换，分摊风控压力。"
          placement="right"
        />
      </h2>
      <div class="header-actions">
        <el-button :icon="Setting" @click="openQuotaDialog">配额与轮换</el-button>
        <el-button :icon="Odometer" :loading="probingAll" @click="probeAllVisible = true">
          全平台体检
        </el-button>
        <el-button :icon="Refresh" :loading="checkingAll" @click="checkAll">批量检测</el-button>
        <el-button type="primary" :icon="Plus" @click="openAdd">添加账号</el-button>
      </div>
    </div>

    <!-- 分组统计卡片：点一下按该组筛选，再点取消 -->
    <div v-if="groups.length" class="group-summary">
      <div
        v-for="g in groups" :key="g.name"
        class="group-card" :class="{ active: query.group === g.name }"
        @click="toggleGroupFilter(g.name)"
      >
        <div class="group-name">{{ g.name }}</div>
        <div class="group-nums">
          <span class="group-total">{{ g.total }} 个账号</span>
          <span class="group-active" :class="g.active > 0 ? 'is-ok' : 'is-none'">
            {{ g.active }} 个可用
          </span>
        </div>
      </div>
    </div>

    <div class="toolbar filter-bar">
      <el-select v-model="query.channel" placeholder="全部平台" clearable style="width: 140px" @change="load">
        <el-option v-for="c in loginChannels" :key="c.value" :label="c.label" :value="c.value" />
      </el-select>
      <el-select v-model="query.group" placeholder="全部分组" clearable style="width: 140px" @change="load">
        <el-option v-for="g in groups" :key="g.name" :label="g.name" :value="g.name" />
      </el-select>
      <el-select v-model="query.status" placeholder="全部状态" clearable style="width: 140px" @change="load">
        <el-option v-for="(label, value) in ACCOUNT_STATUS_LABELS" :key="value" :label="label" :value="value" />
      </el-select>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
      <span class="filter-count muted">共 {{ accounts.length }} 个账号</span>
    </div>

    <el-table :data="accounts" v-loading="loading">
      <el-table-column label="平台" width="96">
        <template #default="{ row }">
          <span class="channel-badge" :style="{ background: CHANNEL_COLORS[row.channel] }">
            {{ channelLabel(row.channel) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="account_name" label="账号标识" min-width="200">
        <template #default="{ row }">
          <div class="account-name" :title="row.account_name">{{ row.account_name }}</div>
          <div v-if="row.nickname" class="account-sub">{{ row.nickname }}</div>
        </template>
      </el-table-column>
      <el-table-column label="分组" width="110">
        <template #default="{ row }">
          <span class="group-chip">{{ row.account_group || 'default' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="登录态" width="100">
        <template #default="{ row }">
          <span class="status-cell">
            <el-tooltip v-if="row.status === 'expired'" content="登录失效，需重新登录" placement="top">
              <AppIcon name="unlink" :size="14" />
            </el-tooltip>
            <el-tag :type="ACCOUNT_STATUS_TYPES[row.status]" size="small" disable-transitions>
              {{ ACCOUNT_STATUS_LABELS[row.status] || row.status }}
            </el-tag>
          </span>
        </template>
      </el-table-column>
      <el-table-column label="Cookie 更新于" width="170">
        <template #default="{ row }"><span class="time-text">{{ formatTime(row.cookie_updated_at) }}</span></template>
      </el-table-column>
      <el-table-column label="最近检测" width="170">
        <template #default="{ row }">
          <div class="time-text">{{ formatTime(row.last_check_time) }}</div>
          <el-tooltip v-if="row.last_error" :content="row.last_error" placement="top">
            <div class="error-text">{{ row.last_error.slice(0, 20) }}…</div>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="今日用量" width="160">
        <template #header>
          <span class="th-with-action">
            今日用量
            <el-tooltip content="配额与轮换">
              <el-button link :icon="Setting" class="th-setting" @click="openQuotaDialog" />
            </el-tooltip>
          </span>
        </template>
        <template #default="{ row }">
          <div v-if="cooling(row)" class="usage-line">
            <el-tooltip :content="row.cooldown_reason || '配额用满，正在冷却'">
              <el-tag size="small" type="warning" disable-transitions>
                <span class="tag-inner"><AppIcon name="lock" :size="12" />冷却至 {{ shortTime(row.cooldown_until) }}</span>
              </el-tag>
            </el-tooltip>
            <IconAction icon="unlock" tip="解除冷却" @click="releaseCooldown(row)" />
          </div>
          <template v-else>
            <span v-if="quotaOf(row)" class="usage-num" :class="{ 'is-warn': nearLimit(row) }">
              {{ usedOf(row) }}<span class="usage-cap">/{{ quotaOf(row) }}</span>
            </span>
            <span v-else class="muted">—</span>
            <!-- 轮换锁跟冷却不一样：号没毛病，只是刚干过活，有别人就让别人上 -->
            <div v-if="lockedFor(row)" class="usage-line" style="margin-top: 4px">
              <el-tooltip
                :content="`采过了，${lockedFor(row)}内先用同组的其他号；只有这一个号时照用不误`"
              >
                <el-tag size="small" type="info" disable-transitions>
                  <span class="tag-inner"><AppIcon name="pending" :size="12" />轮换 {{ lockedFor(row) }}</span>
                </el-tag>
              </el-tooltip>
              <IconAction icon="unlock" tip="解除轮换锁" @click="unlockRotation(row)" />
            </div>
          </template>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="72" align="center">
        <template #default="{ row }">
          <el-switch :model-value="!!row.enabled" size="small" @change="(v: boolean) => toggle(row, v)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="250" align="center" fixed="right">
        <template #default="{ row }">
          <div class="row-actions">
            <IconAction icon="online" tip="打开浏览器登录" @click="openLogin(row)" />
            <IconAction
              icon="publish" tip="采集 Cookie"
              :loading="checkingKey === rowKey(row)" @click="check(row)"
            />
            <IconAction icon="edit" tip="编辑（分组、昵称、轮换锁）" @click="openEdit(row)" />
            <IconAction icon="view" tip="查看 / 导入 Cookie" @click="openCookies(row)" />
            <IconAction icon="query" tip="试搜" @click="openProbe(row)" />
            <el-popconfirm title="退出登录会删除该账号的浏览器 profile，下次要重新扫码。确定吗？"
                           width="260" @confirm="logout(row)">
              <template #reference>
                <span class="pop-ref"><IconAction icon="offline" tip="退出登录" /></span>
              </template>
            </el-popconfirm>
            <el-popconfirm title="删除账号及其 profile？" width="220" @confirm="remove(row)">
              <template #reference>
                <span class="pop-ref"><IconAction icon="delete" tip="删除" /></span>
              </template>
            </el-popconfirm>
          </div>
        </template>
      </el-table-column>
      <template #empty><el-empty description="还没有账号" :image-size="72" /></template>
    </el-table>

    <!-- 添加账号 -->
    <el-dialog v-model="addDialog" :title="editingRow ? '编辑账号' : '添加账号'" width="480px">
      <el-form :model="form" label-width="100px" class="add-form">
        <el-form-item label="平台" required>
          <el-select v-model="form.channel" style="width: 100%" :disabled="!!editingRow">
            <el-option
              v-for="c in channels" :key="c.value"
              :label="c.need_login ? c.label : `${c.label}（无需登录）`"
              :value="c.value" :disabled="!c.need_login"
            />
          </el-select>
        </el-form-item>
        <el-form-item required>
          <template #label>
            账号标识<InfoTip content="只是本地标识，用来区分多个账号；也是浏览器 profile 的目录名。" />
          </template>
          <el-input v-model="form.account_name" placeholder="如 抖音-运营01" :disabled="!!editingRow" />
        </el-form-item>
        <el-form-item>
          <template #label>
            账号分组<InfoTip content="同平台的多个账号放同一组，采集时自动轮换；任务也可以绑定只用某一组。" />
          </template>
          <el-select
            v-model="form.account_group" filterable allow-create default-first-option
            style="width: 100%" placeholder="选已有分组，或输入新分组名回车"
          >
            <el-option v-for="g in groups" :key="g.name" :label="g.name" :value="g.name" />
            <el-option label="default（默认组）" value="default" />
          </el-select>
        </el-form-item>
        <el-form-item label="备注昵称">
          <el-input v-model="form.nickname" />
        </el-form-item>
        <el-form-item>
          <template #label>
            轮换锁
            <InfoTip>
              这个号采过之后，多久之内先让同组的其他号上；只有这一个号时照用不误。<br>
              <b>0 = 用全局值</b>（当前 {{ rotationHours }} 小时）
            </InfoTip>
          </template>
          <el-input-number v-model="form.rotate_lock_hours" :min="0" :max="336" :step="6" />
          <span class="unit">小时</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">
          {{ editingRow ? '保存' : '保存并登录' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 全平台体检 -->
    <el-dialog v-model="probeAllVisible" width="860px" top="5vh">
      <template #header>
        <span class="el-dialog__title">全平台采集体检</span>
        <InfoTip
          content="用真实采集链路把各平台挨个试搜一遍（只取一条、不写库）。抖音/快手要开浏览器页面，整个过程可能要一两分钟。"
        />
      </template>
      <div class="toolbar">
        <el-input v-model="probeAllKeyword" placeholder="试搜关键字，如 天山天池" style="width: 220px" />
        <el-input v-model="probeAllCtrip" placeholder="携程 POI_ID（选填）" style="width: 180px" />
        <el-input v-model="probeAllTongcheng" placeholder="同程 sid（选填）" style="width: 160px" />
        <el-button type="primary" :loading="probingAll" @click="runProbeAll">开始体检</el-button>
      </div>
      <div v-if="probeAllReports.length" class="probe-all-results">
        <div
          v-for="r in probeAllReports" :key="r.channel"
          class="probe-all-row" :class="r.ok ? 'is-ok' : 'is-bad'"
        >
          <div class="probe-all-head">
            <span class="channel-badge" :style="{ background: CHANNEL_COLORS[r.channel] }">
              {{ r.channel_label }}
            </span>
            <el-tag :type="r.ok ? 'success' : 'danger'" size="small" disable-transitions>
              {{ r.ok ? `通过 · ${r.found} 条` : '未通过' }}
            </el-tag>
            <span class="muted probe-all-time">{{ r.elapsed_seconds }}s</span>
            <el-button
              v-if="!r.ok || r.logs?.length" link type="primary" size="small" class="probe-all-toggle"
              @click="toggleProbeDetail(r.channel)"
            >
              {{ expandedProbes.has(r.channel) ? '收起' : '详情' }}
            </el-button>
          </div>
          <div v-if="r.sample" class="probe-all-sample">
            {{ r.sample.title?.slice(0, 60) || r.sample.work_id }}
            <span class="muted">— {{ r.sample.author }}</span>
          </div>
          <div v-if="!r.ok && r.error" class="probe-all-error">{{ r.error }}</div>
          <div v-if="expandedProbes.has(r.channel)" class="probe-all-detail">
            <div v-for="(l, i) in r.logs" :key="i" class="probe-log-line" :class="`lv-${l.level}`">
              {{ l.message }}
            </div>
            <div v-if="r.http?.snippet" class="probe-http">
              HTTP {{ r.http.status }} · {{ r.http.length }} 字符<br>
              <span class="mono">{{ r.http.snippet?.slice(0, 300) }}</span>
            </div>
          </div>
        </div>
      </div>
      <el-empty v-else-if="!probingAll" description="填好关键字，点「开始体检」" :image-size="60" />
    </el-dialog>

    <LoginBrowser
      :visible="loginVisible" :channel="loginTarget.channel" :account-name="loginTarget.accountName"
      @close="onLoginClosed"
    />
    <!-- 配额与轮换：原来只能去「系统设置」里改全局值，单平台的上限根本没地方改 -->
    <el-dialog v-model="quotaDialog" title="配额与轮换" width="720px">
      <el-form label-width="104px" label-position="left" class="quota-form">
        <el-form-item>
          <template #label>
            启用配额<InfoTip content="超过任一上限，账号自动冷却，到期自动恢复。" />
          </template>
          <el-switch v-model="quotaForm.enabled" />
        </el-form-item>
      </el-form>
      <div class="section-title">
        各平台上限
        <InfoTip
          content="留空 = 用默认值（灰字）。默认值来自「系统设置 → 采集参数」里的全局配额，没填全局就是平台出厂值。"
        />
      </div>
      <el-table :data="quotaRows" size="small" class="quota-table">
        <el-table-column label="平台" width="96">
          <template #default="{ row }">
            <span class="channel-badge" :style="{ background: CHANNEL_COLORS[row.channel] }">
              {{ channelLabel(row.channel) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="每天最多采（条）">
          <template #default="{ row }">
            <el-input-number
              v-model="row.daily_works" :min="1" :step="50" :value-on-clear="null"
              :placeholder="`默认 ${row.defaults.daily_works}`" controls-position="right"
              :disabled="!quotaForm.enabled" style="width: 140px"
            />
          </template>
        </el-table-column>
        <el-table-column label="单次连续工作（分钟）">
          <template #default="{ row }">
            <el-input-number
              v-model="row.session_minutes" :min="1" :step="15" :value-on-clear="null"
              :placeholder="`默认 ${row.defaults.session_minutes || '不限'}`" controls-position="right"
              :disabled="!quotaForm.enabled" style="width: 140px"
            />
          </template>
        </el-table-column>
        <el-table-column label="触发后冷却（分钟）">
          <template #default="{ row }">
            <el-input-number
              v-model="row.cooldown_minutes" :min="1" :step="30" :value-on-clear="null"
              :placeholder="`默认 ${row.defaults.cooldown_minutes}`" controls-position="right"
              :disabled="!quotaForm.enabled" style="width: 140px"
            />
          </template>
        </el-table-column>
      </el-table>
      <el-form label-width="104px" label-position="left" class="quota-form" style="margin-top: 20px">
        <el-form-item>
          <template #label>
            账号轮换锁
            <InfoTip>
              一个号采过之后，这段时间内先让同组的其他号上；只有一个号时照用不误。<br>
              单个账号想用别的时长，在那一行「编辑」里单独填（以账号自己的为准）。
            </InfoTip>
          </template>
          <el-switch v-model="quotaForm.rotate_lock_enabled" />
          <el-input-number
            v-model="quotaForm.rotate_lock_hours" :min="1" :max="336" :step="6"
            :disabled="!quotaForm.rotate_lock_enabled" style="width: 130px; margin-left: 12px"
          />
          <span class="unit">小时</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="quotaDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingQuota" @click="saveQuota">保存</el-button>
      </template>
    </el-dialog>

    <CookieDialog
      v-model="cookieVisible" :channel="cookieTarget.channel"
      :account-name="cookieTarget.accountName" @changed="load"
    />
    <ProbeDialog
      v-model="probeVisible" :channel="probeTarget.channel"
      :account-name="probeTarget.accountName"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Odometer, Plus, Refresh, Setting } from '@element-plus/icons-vue'
import {
  accountApi, settingsApi, type Account, type AccountQuotaInfo, type AccountQuotaLimits,
  type AccountGroup, type ChannelOption, type ProbeReport,
} from '../api'
import {
  ACCOUNT_STATUS_LABELS, ACCOUNT_STATUS_TYPES, CHANNEL_COLORS,
  channelLabel, formatTime,
} from '../constants'
import LoginBrowser from '../components/LoginBrowser.vue'
import CookieDialog from '../components/CookieDialog.vue'
import ProbeDialog from '../components/ProbeDialog.vue'

const accounts = ref<Account[]>([])
const channels = ref<ChannelOption[]>([])
const groups = ref<AccountGroup[]>([])
const loading = ref(false)
const saving = ref(false)
const checkingAll = ref(false)
const checkingKey = ref('')
const addDialog = ref(false)
const loginVisible = ref(false)
const loginTarget = reactive({ channel: '', accountName: '' })
const cookieVisible = ref(false)
const cookieTarget = reactive({ channel: '', accountName: '' })
const probeVisible = ref(false)
const probeTarget = reactive({ channel: '', accountName: '' })
const query = reactive({ channel: '', status: '', group: '' })
const form = reactive({
  channel: 'douyin', account_name: '', nickname: '', account_group: 'default',
  rotate_lock_hours: 0,
})
/** 正在编辑的账号；null = 添加。平台和账号标识是主键，编辑时不能改 */
const editingRow = ref<Account | null>(null)

function openAdd() {
  editingRow.value = null
  Object.assign(form, { account_name: '', nickname: '', account_group: 'default', rotate_lock_hours: 0 })
  addDialog.value = true
}

function openEdit(row: Account) {
  editingRow.value = row
  Object.assign(form, {
    channel: row.channel, account_name: row.account_name, nickname: row.nickname || '',
    account_group: row.account_group || 'default', rotate_lock_hours: row.rotate_lock_hours || 0,
  })
  addDialog.value = true
}

// 全平台体检
const probeAllVisible = ref(false)
const probingAll = ref(false)
const probeAllKeyword = ref('天山天池')
const probeAllCtrip = ref('')
const probeAllTongcheng = ref('')
const probeAllReports = ref<ProbeReport[]>([])
const expandedProbes = ref<Set<string>>(new Set())

const loginChannels = computed(() => channels.value.filter((c) => c.need_login))

function rowKey(row: Account): string {
  return `${row.channel}:${row.account_name}`
}

async function load() {
  loading.value = true
  try {
    accounts.value = (await accountApi.list(query)).items
  } finally {
    loading.value = false
  }
}

/**
 * 平台清单。
 *
 * ⚠️ 这个函数原来不存在——`channels` 声明了却从没被赋值，于是「添加账号」
 * 的平台下拉**一个选项都没有**。而 el-select 在没有匹配选项时会把
 * v-model 的原始值直接显示出来，`form.channel` 默认是 'douyin'，
 * 所以看起来像"平台只剩 douyin"，其实是整个列表是空的。
 * 顶部那个平台筛选下拉也一样是空的，只是它有 placeholder，不容易发现。
 */
async function loadChannels() {
  try {
    channels.value = await accountApi.channels()
  } catch {
    channels.value = []
  }
}

/**
 * 配额与冷却。
 *
 * 加这一列是因为：账号被平台踢下线之后，人得先知道"它今天采了多少"
 * 才能判断是不是采太多了。以前这个数只存在日志里。
 */
const quotaInfo = ref<AccountQuotaInfo | null>(null)

async function loadQuota() {
  try {
    quotaInfo.value = await accountApi.quota({ days: 1 })
  } catch {
    quotaInfo.value = null
  }
}

function cooling(row: Account): boolean {
  if (!row.cooldown_until) return false
  return new Date(row.cooldown_until).getTime() > Date.now()
}

function shortTime(value?: string | null): string {
  if (!value) return ''
  const d = new Date(value)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function quotaOf(row: Account): number {
  return quotaInfo.value?.limits?.[row.channel]?.daily_works || 0
}

function usedOf(row: Account): number {
  const hit = quotaInfo.value?.usage?.find(
    (u) => u.channel === row.channel && u.account_name === row.account_name)
  return hit?.works || 0
}

function nearLimit(row: Account): boolean {
  const cap = quotaOf(row)
  return cap > 0 && usedOf(row) >= cap * 0.8
}

const rotationHours = computed(() => quotaInfo.value?.rotation?.default_hours ?? 12)

/** 这个号还剩多久解锁；没锁返回空串 */
function lockedFor(row: Account): string {
  const rot = quotaInfo.value?.rotation
  if (!rot?.enabled) return ''
  const left = rot.locked?.[`${row.channel}/${row.account_name}`] || 0
  if (left <= 0) return ''
  // ⚠️ 向上取整。锁 12 小时刚上完只剩 11 小时 59 分，floor 会写成「11 小时」——
  //    用户刚在设置里填了 12，页面上立刻显示 11，只会让人以为哪儿没生效。
  if (left >= 3600) return `${Math.ceil(left / 3600)} 小时`
  return `${Math.max(1, Math.ceil(left / 60))} 分钟`
}

/**
 * 「配额与轮换」对话框。
 *
 * 存的地方和「系统设置 → 采集参数」是同一份（crawl.account_quota），
 * 单平台的值写进 per_channel——后端一直支持，只是以前页面上没入口。
 * 输入框留空（null）= 用默认值，存成 0：后端 _apply 把 0 当"没填"，不是"不限"。
 */
type QuotaKey = keyof AccountQuotaLimits
const QUOTA_KEYS: QuotaKey[] = ['daily_works', 'session_minutes', 'cooldown_minutes']
interface QuotaRow {
  channel: string
  daily_works: number | null
  session_minutes: number | null
  cooldown_minutes: number | null
  defaults: AccountQuotaLimits
}
const quotaDialog = ref(false)
const savingQuota = ref(false)
const quotaRows = ref<QuotaRow[]>([])
const quotaForm = reactive({ enabled: true, rotate_lock_enabled: true, rotate_lock_hours: 12 })

async function openQuotaDialog() {
  const [settings, info] = await Promise.all([settingsApi.get(), accountApi.quota({ days: 1 })])
  quotaInfo.value = info
  const cfg = settings.config?.crawl?.account_quota || {}
  const perChannel: Record<string, Partial<AccountQuotaLimits>> = cfg.per_channel || {}
  quotaForm.enabled = cfg.enabled !== false
  quotaForm.rotate_lock_enabled = cfg.rotate_lock_enabled !== false
  quotaForm.rotate_lock_hours = info.rotation?.default_hours || 12
  quotaRows.value = Object.keys(info.limits || {}).map((channel) => {
    const own = perChannel[channel] || {}
    const row: QuotaRow = {
      channel,
      daily_works: null, session_minutes: null, cooldown_minutes: null,
      defaults: info.defaults?.[channel] || info.limits[channel],
    }
    for (const key of QUOTA_KEYS) {
      const v = Number(own[key] || 0)
      row[key] = v > 0 ? v : null
    }
    return row
  })
  quotaDialog.value = true
}

async function saveQuota() {
  savingQuota.value = true
  try {
    const perChannel: Record<string, AccountQuotaLimits> = {}
    for (const row of quotaRows.value) {
      perChannel[row.channel] = {
        daily_works: row.daily_works || 0,
        session_minutes: row.session_minutes || 0,
        cooldown_minutes: row.cooldown_minutes || 0,
      }
    }
    await settingsApi.save('crawl', {
      account_quota: {
        enabled: quotaForm.enabled,
        rotate_lock_enabled: quotaForm.rotate_lock_enabled,
        rotate_lock_hours: quotaForm.rotate_lock_hours,
        per_channel: perChannel,
      },
    })
    ElMessage.success('已保存，下次挑号/采集时生效')
    quotaDialog.value = false
    await loadQuota()
  } finally {
    savingQuota.value = false
  }
}

async function unlockRotation(row: Account) {
  // ⚠️ 这个**不**弹确认框。跟解除冷却不一样：轮换锁只是"刚干过活，让别人
  //    先上"的软偏好，解掉它顶多是这个号被连着用两轮，配额和冷却那两道
  //    闸还在。为一个没风险的操作加一次点击，只会让人烦。
  await accountApi.unlockRotation(row.channel, row.account_name)
  ElMessage.success('已解除轮换锁')
  await loadQuota()
}

async function releaseCooldown(row: Account) {
  await ElMessageBox.confirm(
    `解除冷却之后这个账号会立刻被重新使用。配额闸门本来就是为了别把账号用到被封——确定吗？`,
    '解除冷却', { type: 'warning' })
  await accountApi.clearCooldown(row.channel, row.account_name)
  ElMessage.success('已解除冷却')
  await Promise.all([load(), loadQuota()])
}

async function loadGroups() {
  try {
    groups.value = await accountApi.groups()
  } catch {
    groups.value = []
  }
}

function toggleGroupFilter(name: string) {
  query.group = query.group === name ? '' : name
  load()
}

async function save() {
  if (!form.account_name.trim()) {
    ElMessage.warning('给账号起个标识名')
    return
  }
  saving.value = true
  try {
    // 编辑也走这个接口：后端按 平台+账号 upsert，登录态/Cookie 不动。
    // ⚠️ enabled、login_type 必须原样带回去，否则停用的号一编辑就被悄悄启用了
    const editing = editingRow.value
    await accountApi.create({
      channel: form.channel,
      account_name: form.account_name.trim(),
      nickname: form.nickname || null,
      account_group: form.account_group || 'default',
      rotate_lock_hours: form.rotate_lock_hours || 0,
      ...(editing ? { enabled: editing.enabled, login_type: editing.login_type } : {}),
    })
    addDialog.value = false
    await Promise.all([load(), loadGroups()])
    if (editing) {
      ElMessage.success('已保存')
      editingRow.value = null
      return
    }
    openLogin({ channel: form.channel, account_name: form.account_name.trim() } as Account)
    form.account_name = ''
    form.nickname = ''
  } finally {
    saving.value = false
  }
}

function openCookies(row: Account) {
  cookieTarget.channel = row.channel
  cookieTarget.accountName = row.account_name
  cookieVisible.value = true
}

function openProbe(row: Account) {
  probeTarget.channel = row.channel
  probeTarget.accountName = row.account_name
  probeVisible.value = true
}

function openLogin(row: Account) {
  loginTarget.channel = row.channel
  loginTarget.accountName = row.account_name
  loginVisible.value = true
}

async function onLoginClosed(loggedIn: boolean) {
  loginVisible.value = false
  if (loggedIn) ElMessage.success('登录态已保存')
  await Promise.all([load(), loadGroups()])
}

async function check(row: Account) {
  checkingKey.value = rowKey(row)
  try {
    const result = await accountApi.harvest(row.channel, row.account_name)
    if (result.logged_in) {
      ElMessage.success(`${result.message}（${result.cookie_count} 个 Cookie）`)
    } else {
      ElMessage.warning(result.message)
    }
    await Promise.all([load(), loadGroups()])
  } finally {
    checkingKey.value = ''
  }
}

async function checkAll() {
  checkingAll.value = true
  try {
    const results = await accountApi.list({})
    const total = results.items.filter((a) => a.enabled).length
    if (!total) {
      ElMessage.info('没有启用的账号')
      return
    }
    ElMessage.success(`批量检测完成：${total} 个账号`)
    await Promise.all([load(), loadGroups()])
  } finally {
    checkingAll.value = false
  }
}

async function logout(row: Account) {
  try {
    await ElMessageBox.confirm(`确定退出 ${row.account_name} 的登录吗？`, '提示', { type: 'warning' })
    await accountApi.logout(row.channel, row.account_name)
    ElMessage.success('已退出登录')
    await Promise.all([load(), loadGroups()])
  } catch {
    // 用户取消
  }
}

/**
 * 启用 / 停用。
 *
 * ⚠️ 表格里 `@change="(v) => toggle(row, v)"` 调的是这个函数，但它原来
 * **根本没定义**（文件里只有 toggleProbeDetail）——点开关直接 ReferenceError，
 * 开关拨不动，而且控制台之外没有任何提示。
 */
async function toggle(row: Account, enabled: boolean) {
  try {
    await accountApi.toggle(row.channel, row.account_name, enabled)
    row.enabled = enabled ? 1 : 0
    ElMessage.success(enabled ? '已启用' : '已停用')
  } catch (e: any) {
    ElMessage.error(e?.message || '操作失败')
    await load()          // 失败就把界面拨回真实状态，别让开关停在假的位置
  }
}

async function remove(row: Account) {
  try {
    await ElMessageBox.confirm(`确定删除账号 ${row.account_name} 吗？`, '警告', { type: 'error' })
    await accountApi.remove(row.channel, row.account_name)
    ElMessage.success('删除成功')
    await Promise.all([load(), loadGroups()])
  } catch {
    // 用户取消
  }
}

async function runProbeAll() {
  probingAll.value = true
  try {
    // ⚠️ probeAll 收的是**三个位置参数**，原来传了一个对象进去，
    //    keyword 会变成 "[object Object]"——体检等于拿一个乱码去搜。
    //    返回的也直接就是报告数组（后端 ok(reports)），原来读 result.reports
    //    永远是 undefined，所以列表永远空、提示永远是"所有平台正常"。
    const reports = await accountApi.probeAll(
      probeAllKeyword.value,
      probeAllCtrip.value,
      probeAllTongcheng.value,
    )
    probeAllReports.value = reports || []
    const passed = (reports || []).filter((r) => r.ok).length
    ElMessage.success(`体检完成：${passed}/${(reports || []).length} 个平台能采到数据`)
  } catch (e: any) {
    ElMessage.error(e.message || '体检失败')
  } finally {
    probingAll.value = false
  }
}

function toggleProbeDetail(channel: string) {
  if (expandedProbes.value.has(channel)) {
    expandedProbes.value.delete(channel)
  } else {
    expandedProbes.value.add(channel)
  }
}

onMounted(() => {
  load()
  loadGroups()
  loadChannels()
  loadQuota()
})
</script>

<style scoped>
.header-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.header-actions .el-button + .el-button,
.filter-bar .el-button + .el-button { margin-left: 0; }

/* ---------- 分组统计卡片 ---------- */
.group-summary {
  display: grid;
  /* ⚠️ 最小宽度要容得下「NN 个账号 · NN 个可用」一整行（约 130px + 内边距 32）。
     以前卡片里还有图标和箭头，宽度给 200 时正好差一点点，于是「1 个账号」
     从字中间断成「1 个账 / 号」——看着像文案写错了，其实是宽度不够。 */
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}
.group-card {
  padding: 14px 16px;
  background: var(--smc-card-bg);
  border: 1px solid var(--smc-border);
  border-radius: var(--smc-radius);
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}
.group-card:hover { border-color: var(--smc-border-strong); }
.group-card.active {
  border-color: var(--smc-primary);
  background: var(--smc-primary-light);
}
.group-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--smc-text);
  margin-bottom: 6px;
  /* 分组名是用户自己起的，可能很长；省略号比换行好——
     换行会把下面那行数字顶下去，几张卡片高度参差不齐 */
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.group-nums {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  /* 万一还是放不下，让它整体横向省略，而不是把每个词拆开换行 */
  overflow: hidden;
}
/* ⚠️ 这两条是修「1 个账 / 号」的关键。
   flex 子项默认 min-width:auto 但允许收缩，父容器一窄就把文字压到
   一个字一行。nowrap 保证不从字中间断，flex-shrink:0 保证不被压。 */
.group-total,
.group-active {
  white-space: nowrap;
  flex-shrink: 0;
}
.group-total { color: var(--smc-text-secondary); }
/* 两个数字之间加个分隔点，比单纯留空更容易一眼分开 */
.group-active::before {
  content: '·';
  margin-right: 8px;
  color: var(--smc-text-tertiary);
}
.group-active.is-ok { color: #16a34a; }
.group-active.is-none { color: #d97706; }

/* ---------- 筛选栏 ---------- */
.filter-count {
  margin-left: auto;
  font-size: 13px;
}

/* ---------- 表格 ---------- */
.account-name {
  font-weight: 500;
  color: var(--smc-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.account-sub {
  font-size: 12px;
  color: var(--smc-text-secondary);
  margin-top: 2px;
}
.status-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.time-text {
  font-size: 13px;
  white-space: nowrap;
  color: #5b6375;
  font-variant-numeric: tabular-nums;
}
.error-text {
  font-size: 12px;
  color: #dc2626;
  margin-top: 2px;
  cursor: default;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.th-with-action {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}
.th-setting {
  color: var(--smc-text-secondary);
  padding: 0 2px;
  height: auto;
}
.th-setting:hover { color: var(--smc-primary); }
.usage-line {
  display: flex;
  align-items: center;
  gap: 2px;
}
.usage-num { font-variant-numeric: tabular-nums; }
.usage-cap { color: var(--smc-text-secondary); }
/* 今日用量接近上限时标黄：再采下去就要进冷却了 */
.is-warn,
.is-warn .usage-cap { color: #d97706; font-weight: 600; }
.tag-inner {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.row-actions {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}
.pop-ref { display: inline-flex; }

/* ---------- 弹窗 ---------- */
.unit {
  margin-left: 8px;
  color: var(--smc-text-secondary);
  font-size: 13px;
}
.add-form :deep(.el-form-item__label),
.quota-form :deep(.el-form-item__label) {
  align-items: center;
}
.quota-form :deep(.el-form-item) { margin-bottom: 12px; }
.quota-table { margin-bottom: 4px; }

/* 全平台体检结果 */
.probe-all-results {
  border: 1px solid var(--smc-border);
  border-radius: var(--smc-radius-sm);
}
.probe-all-row {
  padding: 12px 14px;
  border-bottom: 1px solid var(--smc-border);
}
.probe-all-row:last-child { border-bottom: none; }
.probe-all-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.probe-all-time { font-size: 12px; }
.probe-all-toggle { margin-left: auto; }
.probe-all-sample {
  margin-top: 8px;
  font-size: 13px;
  color: #4a5263;
}
.probe-all-error {
  margin-top: 8px;
  font-size: 12px;
  color: #dc2626;
  word-break: break-all;
}
.probe-all-detail {
  margin-top: 8px;
  padding: 10px 12px;
  background: #fafbfc;
  border-radius: var(--smc-radius-sm);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.7;
  max-height: 240px;
  overflow-y: auto;
}
.probe-log-line.lv-warn { color: #d97706; }
.probe-log-line.lv-error { color: #dc2626; }
.probe-http {
  margin-top: 6px;
  color: var(--smc-text-secondary);
  word-break: break-all;
}
</style>
