<template>
  <div>
    <div class="page-header">
      <div>
        <h2 class="page-title">账号管理</h2>
        <p class="page-subtitle">
          每个账号一个独立的浏览器 profile，登录一次之后采集任务静默复用登录态；
          同组账号自动轮换，分摊风控压力
        </p>
      </div>
      <div style="display: flex; gap: 10px">
        <el-button :icon="Odometer" :loading="probingAll" @click="probeAllVisible = true">
          全平台体检
        </el-button>
        <el-button :icon="Refresh" :loading="checkingAll" @click="checkAll">批量检测</el-button>
        <el-button type="primary" :icon="Plus" @click="addDialog = true">添加账号</el-button>
      </div>
    </div>

    <!-- 分组统计卡片 -->
    <div v-if="groups.length" class="group-summary">
      <div
        v-for="g in groups" :key="g.name"
        class="group-card" :class="{ active: query.group === g.name, 'is-default': g.name === 'default' }"
        @click="toggleGroupFilter(g.name)"
      >
        <div class="group-icon">
          <el-icon><FolderOpened /></el-icon>
        </div>
        <div class="group-info">
          <div class="group-name">{{ g.name }}</div>
          <div class="group-nums">
            <span class="group-total">{{ g.total }} 个账号</span>
            <span class="group-active" :class="{ 'text-green': g.active > 0, 'text-orange': g.active === 0 }">
              {{ g.active }} 个可用
            </span>
          </div>
        </div>
        <div class="group-arrow">
          <el-icon><ArrowRight /></el-icon>
        </div>
      </div>
    </div>

    <div class="toolbar">
      <el-select v-model="query.channel" placeholder="平台" clearable style="width: 130px" @change="load">
        <el-option v-for="c in loginChannels" :key="c.value" :label="c.label" :value="c.value" />
      </el-select>
      <el-select v-model="query.group" placeholder="分组" clearable style="width: 150px" @change="load">
        <el-option v-for="g in groups" :key="g.name" :label="g.name" :value="g.name" />
      </el-select>
      <el-select v-model="query.status" placeholder="状态" clearable style="width: 130px" @change="load">
        <el-option v-for="(label, value) in ACCOUNT_STATUS_LABELS" :key="value" :label="label" :value="value" />
      </el-select>
      <el-button @click="load">刷新</el-button>
    </div>

    <el-table :data="accounts" v-loading="loading" border stripe>
      <el-table-column label="平台" width="100">
        <template #default="{ row }">
          <span class="channel-badge" :style="{ background: CHANNEL_COLORS[row.channel] }">
            {{ channelLabel(row.channel) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="account_name" label="账号标识" min-width="140">
        <template #default="{ row }">
          <div style="font-weight: 500">{{ row.account_name }}</div>
          <div v-if="row.nickname" class="muted" style="font-size: 12px">{{ row.nickname }}</div>
        </template>
      </el-table-column>
      <el-table-column label="分组" width="120" align="center">
        <template #default="{ row }">
          <span class="group-chip">{{ row.account_group || 'default' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="登录态" width="100" align="center">
        <template #default="{ row }">
          <el-tag :type="ACCOUNT_STATUS_TYPES[row.status]" size="small">
            {{ ACCOUNT_STATUS_LABELS[row.status] || row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="Cookie 更新于" width="150">
        <template #default="{ row }">{{ formatTime(row.cookie_updated_at) }}</template>
      </el-table-column>
      <el-table-column label="最近检测" width="150">
        <template #default="{ row }">
          <div>{{ formatTime(row.last_check_time) }}</div>
          <el-tooltip v-if="row.last_error" :content="row.last_error" placement="top">
            <span class="muted" style="font-size: 12px; color: #f56c6c">
              {{ row.last_error.slice(0, 20) }}…
            </span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="今日用量" width="130" align="center">
        <template #default="{ row }">
          <template v-if="cooling(row)">
            <el-tooltip :content="row.cooldown_reason || '配额用满，正在冷却'">
              <el-tag size="small" type="warning">冷却至 {{ shortTime(row.cooldown_until) }}</el-tag>
            </el-tooltip>
            <el-button link type="primary" size="small" @click="releaseCooldown(row)">
              解除
            </el-button>
          </template>
          <template v-else>
            <span v-if="quotaOf(row)" :class="{ 'is-warn': nearLimit(row) }">
              {{ usedOf(row) }}/{{ quotaOf(row) }}
            </span>
            <span v-else class="muted">—</span>
            <!-- 轮换锁跟冷却不一样：号没毛病，只是刚干过活，有别人就让别人上 -->
            <div v-if="lockedFor(row)" style="margin-top: 2px">
              <el-tooltip
                :content="`采过了，${lockedFor(row)}内先用同组的其他号；只有这一个号时照用不误`"
              >
                <el-tag size="small" type="info">轮换 {{ lockedFor(row) }}</el-tag>
              </el-tooltip>
              <el-button link type="primary" size="small" @click="unlockRotation(row)">
                解锁
              </el-button>
            </div>
          </template>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="70" align="center">
        <template #default="{ row }">
          <el-switch :model-value="!!row.enabled" @change="(v: boolean) => toggle(row, v)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="380" align="center" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" :icon="Monitor" @click="openLogin(row)">打开浏览器</el-button>
          <el-button link type="primary" :loading="checkingKey === rowKey(row)" @click="check(row)">
            采集 Cookie
          </el-button>
          <el-button link type="primary" @click="openCookies(row)">Cookie</el-button>
          <el-button link type="success" :icon="Search" @click="openProbe(row)">试搜</el-button>
          <el-popconfirm title="退出登录会删除该账号的浏览器 profile，下次要重新扫码。确定吗？"
                         width="260" @confirm="logout(row)">
            <template #reference><el-button link type="warning">退出</el-button></template>
          </el-popconfirm>
          <el-popconfirm title="删除账号及其 profile？" @confirm="remove(row)">
            <template #reference><el-button link type="danger">删除</el-button></template>
          </el-popconfirm>
        </template>
      </el-table-column>
      <template #empty><el-empty description="还没有账号，点右上角「添加账号」开始" /></template>
    </el-table>

    <!-- 添加账号 -->
    <el-dialog v-model="addDialog" title="添加账号" width="460px">
      <el-form :model="form" label-width="90px">
        <el-form-item label="平台" required>
          <el-select v-model="form.channel" style="width: 100%">
            <el-option
              v-for="c in channels" :key="c.value"
              :label="c.need_login ? c.label : `${c.label}（无需登录）`"
              :value="c.value" :disabled="!c.need_login"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="账号标识" required>
          <el-input v-model="form.account_name" placeholder="自己起个名字，如 抖音-运营01" />
          <div class="muted" style="font-size: 12px; margin-top: 4px">
            只是本地标识，用来区分多个账号；会作为浏览器 profile 的目录名
          </div>
        </el-form-item>
        <el-form-item label="账号分组">
          <el-select
            v-model="form.account_group" filterable allow-create default-first-option
            style="width: 100%" placeholder="选已有分组，或输入新分组名回车"
          >
            <el-option v-for="g in groups" :key="g.name" :label="g.name" :value="g.name" />
            <el-option label="default（默认组）" value="default" />
          </el-select>
          <div class="muted" style="font-size: 12px; margin-top: 4px">
            同平台的多个账号放同一组，采集时自动轮换；任务也可以绑定只用某一组
          </div>
        </el-form-item>
        <el-form-item label="备注昵称">
          <el-input v-model="form.nickname" />
        </el-form-item>
        <el-form-item label="轮换锁">
          <el-input-number v-model="form.rotate_lock_hours" :min="0" :max="336" :step="6" />
          <span class="muted" style="margin-left: 8px">小时</span>
          <div class="muted" style="font-size: 12px; margin-top: 4px">
            这个号采过之后，多久之内先让同组的其他号上。
            <b>0 = 用系统设置里的全局值</b>（当前 {{ rotationHours }} 小时）。
            只有这一个号时照用不误，不会把采集卡住。
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存并登录</el-button>
      </template>
    </el-dialog>

    <!-- 全平台体检 -->
    <el-dialog v-model="probeAllVisible" title="全平台采集体检" width="860px" top="5vh">
      <el-alert type="info" :closable="false" style="margin-bottom: 14px">
        用真实采集链路把六个平台挨个试搜一遍（只取一条、不写库），
        抖音/快手要开浏览器页面，整个过程可能要一两分钟。
      </el-alert>
      <div class="toolbar" style="margin-bottom: 14px">
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
            <el-tag :type="r.ok ? 'success' : 'danger'" size="small">
              {{ r.ok ? `通过 · 拿到 ${r.found} 条` : '未通过' }}
            </el-tag>
            <span class="muted" style="font-size: 12px">{{ r.elapsed_seconds }}s</span>
            <el-button
              v-if="!r.ok || r.logs?.length" link type="primary" size="small"
              @click="toggleProbeDetail(r.channel)"
            >
              {{ expandedProbes.has(r.channel) ? '收起' : '详情' }}
            </el-button>
          </div>
          <div v-if="r.sample" class="probe-all-sample">
            样例：{{ r.sample.title?.slice(0, 60) || r.sample.work_id }}
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
      <el-empty v-else-if="!probingAll" description="填好关键字后点「开始体检」" :image-size="60" />
    </el-dialog>

    <LoginBrowser
      :visible="loginVisible" :channel="loginTarget.channel" :account-name="loginTarget.accountName"
      @close="onLoginClosed"
    />
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
import { ArrowRight, FolderOpened, Monitor, Odometer, Plus, Refresh, Search } from '@element-plus/icons-vue'
import { accountApi, type Account, type AccountQuotaInfo, type AccountGroup, type ChannelOption, type ProbeReport } from '../api'
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
    await accountApi.create({
      channel: form.channel,
      account_name: form.account_name.trim(),
      nickname: form.nickname || null,
      account_group: form.account_group || 'default',
      rotate_lock_hours: form.rotate_lock_hours || 0,
    })
    addDialog.value = false
    await Promise.all([load(), loadGroups()])
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
/* 今日用量接近上限时标黄：再采下去就要进冷却了 */
.is-warn { color: #d97706; font-weight: 600; }
.group-summary {
  display: grid;
  /* ⚠️ 240 不是拍脑袋：图标 40 + 间距 12 + 「1 个账号 1 个可用」约 120
     + 箭头 14 + 左右内边距 40 ≈ 226，再留一点余量。
     之前是 200，正好差一点点，于是「1 个账号」从字中间断成
     「1 个账 / 号」——看着像文案写错了，其实是宽度不够。 */
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}
.group-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 20px;
  background: #fff;
  border: 1px solid #e8ecf3;
  border-radius: 12px;
  cursor: pointer;
  transition: all 0.2s ease;
}
.group-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(16, 24, 40, 0.08);
  border-color: #4f6ef7;
}
.group-card.active {
  background: #eef2ff;
  border-color: #4f6ef7;
  box-shadow: 0 4px 12px rgba(79, 110, 247, 0.15);
}
.group-card.is-default {
  background: linear-gradient(135deg, #f0f5ff 0%, #e6f0ff 100%);
  border-color: #91caff;
}
.group-card.is-default.active {
  background: linear-gradient(135deg, #d6e4ff 0%, #cce0ff 100%);
  border-color: #4f6ef7;
}
.group-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  background: #f0f2f5;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  color: #4f6ef7;
  flex-shrink: 0;
}
.group-card.active .group-icon {
  background: #4f6ef7;
  color: #fff;
}
.group-info {
  flex: 1;
  min-width: 0;
}
.group-name {
  font-size: 14px;
  font-weight: 600;
  color: #1f2733;
  margin-bottom: 4px;
  /* 分组名是用户自己起的，可能很长；省略号比换行好——
     换行会把下面那行数字顶下去，四张卡片高度参差不齐 */
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
.group-total {
  color: #7a8699;
}
.group-active {
  font-weight: 500;
}
/* 两个数字之间加个分隔点，比单纯留空更容易一眼分开 */
.group-active::before {
  content: '·';
  margin-right: 8px;
  color: #d5dbe6;
  font-weight: 400;
}
.group-active.text-green {
  color: #10b981;
}
.group-active.text-orange {
  color: #f59e0b;
}
.group-arrow {
  color: #c9d2e0;
  font-size: 14px;
  flex-shrink: 0;
  transition: transform 0.2s ease;
}
.group-card:hover .group-arrow {
  transform: translateX(4px);
  color: #4f6ef7;
}
</style>
