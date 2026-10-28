<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <div class="cluster-view" v-loading="loading && !info">
    <div class="page-header">
      <h2 class="page-title">
        集群
        <InfoTip :width="380">
          多台机器连同一个 MySQL（和同一个 Redis）。<b>主机</b>负责协调：把到点的任务派给合适的机器；
          <b>从机</b>负责运行：跑采集任务。只有一台机器时它既是主也是从。<br>
          账号在哪台机器上登录，就归那台；任务派给「有这个平台能用的号、又有空位」的机器。
        </InfoTip>
      </h2>
      <div class="header-actions">
        <span class="muted">每 10 秒自动刷新</span>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <template v-if="info">
      <div class="summary">
        <div class="sum-item">
          <div class="sum-label">本机</div>
          <div class="sum-value">{{ info.self.node_name }}</div>
          <div class="sum-sub">{{ info.self.role_label }}{{ info.self.is_leader ? ' · 当前主机' : '' }}</div>
        </div>
        <div class="sum-item">
          <div class="sum-label">当前主机</div>
          <div class="sum-value">{{ leaderName }}</div>
          <div class="sum-sub">负责派任务</div>
        </div>
        <div class="sum-item">
          <div class="sum-label">在线机器</div>
          <div class="sum-value">{{ onlineCount }} / {{ info.nodes.length }}</div>
          <div class="sum-sub">{{ info.offline_seconds }} 秒没心跳算掉线</div>
        </div>
        <router-link class="sum-item is-link" :to="{ path: '/tasks', query: { status: 'running' } }">
          <div class="sum-label">运行中任务</div>
          <div class="sum-value">{{ runningTotal }} / {{ capacityTotal }}</div>
          <div class="sum-sub">所有在线机器合计 · 点击查看</div>
        </router-link>
      </div>

      <el-alert v-if="!info.leader" type="error" :closable="false" show-icon class="alert"
                title="现在没有主机：到点的任务不会派发。至少要有一台机器的角色是「主 + 从」或「主」并且在线。" />
      <el-alert v-if="!info.auto_dispatch" type="warning" :closable="false" show-icon class="alert"
                title="「系统设置 → 调度」已关闭：到点的任务不会自动派发，只能在任务页手动「立即执行」。" />
      <el-alert v-if="versionMismatch" type="warning" :closable="false" show-icon class="alert"
                :title="`各台机器的程序版本不一致（${versions.join('、')}），升级时请把每台都更新到同一版。`" />
      <el-alert v-if="redisWarn" type="warning" :closable="false" show-icon class="alert"
                title="本机没连上 Redis。多台机器时必须连同一个 Redis：代理池、账号轮换锁、去重都靠它在机器之间共享。" />
      <el-alert v-if="unassignedTotal" type="info" :closable="false" show-icon class="alert">
        <template #title>
          有 {{ unassignedTotal }} 个账号还没分配机器（升级前添加的、且 profile 不在任何一台从机上）。
          <router-link to="/accounts" class="link">去账号管理</router-link>给它们选机器。
        </template>
      </el-alert>

      <el-table :data="info.nodes" class="data-table" row-key="node_id">
        <el-table-column label="机器" min-width="170">
          <template #default="{ row }">
            <div class="node-name">
              <router-link class="link" :to="{ path: '/tasks', query: { node: row.node_id } }"
                           :title="`看派给 ${row.node_name} 的任务`">{{ row.node_name }}</router-link>
              <el-tag v-if="row.is_leader" size="small" type="success" effect="dark">主机</el-tag>
              <el-tag v-if="row.is_self" size="small" type="info">本机</el-tag>
            </div>
            <div class="node-sub">{{ row.node_id }}{{ row.host && row.host !== row.node_id ? ` · ${row.host}` : '' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="角色" width="90">
          <template #default="{ row }">{{ row.role_label }}</template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <span class="state">
              <span class="dot" :class="row.online ? 'ok' : 'bad'" />
              {{ row.online ? '在线' : '离线' }}
            </span>
            <div class="node-sub">{{ heartbeatText(row) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="任务" width="120">
          <template #default="{ row }">
            <span v-if="!row.online" class="muted">-</span>
            <template v-else-if="row.role !== 'master'">
              <router-link class="link" :to="{ path: '/tasks', query: { node: row.node_id, status: 'running' } }">
                在跑 {{ row.running_count }} / {{ row.max_running }}
              </router-link>
              <div v-if="row.load > row.running_count" class="node-sub">另有 {{ row.load - row.running_count }} 个刚派到</div>
            </template>
            <span v-else class="muted">不跑任务</span>
          </template>
        </el-table-column>
        <el-table-column label="浏览器" width="110">
          <template #default="{ row }">
            <template v-if="row.online && row.stats?.browser && row.role !== 'master'">
              {{ row.stats.browser.active }} / {{ row.stats.browser.limit }}
              <div v-if="row.stats.browser.memory_available_gb != null" class="node-sub nowrap">
                内存剩 {{ Number(row.stats.browser.memory_available_gb).toFixed(1) }}G
              </div>
            </template>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="账号（能用 / 总数）" min-width="200">
          <template #default="{ row }">
            <div v-if="Object.keys(row.accounts).length" class="acc-chips">
              <router-link v-for="(v, ch) in row.accounts" :key="ch" class="acc-chip"
                           :class="{ 'is-none': !v.usable }"
                           :to="{ path: '/accounts', query: { node: row.node_id, channel: String(ch) } }"
                           :title="`看 ${row.node_name} 上的${channelLabel(String(ch))}账号`">
                <span class="acc-dot" :style="{ background: CHANNEL_COLORS[ch] }" />
                {{ channelLabel(String(ch)) }} {{ v.usable }}/{{ v.total }}
              </router-link>
            </div>
            <span v-else class="muted">{{ row.role === 'master' ? '-' : '没有账号' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="访问地址" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <!-- 直接打开那台机器自己的页面（比如只在那台上能看的东西、或者排查它的问题） -->
            <a v-if="row.advertise_url && row.online" class="mono nowrap link" :href="row.advertise_url"
               target="_blank" rel="noopener" :title="`在新标签页打开 ${row.node_name} 的页面`">{{ row.advertise_url }}</a>
            <span v-else class="mono nowrap">{{ row.advertise_url || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="版本" width="70">
          <template #default="{ row }">{{ row.version || '-' }}</template>
        </el-table-column>
        <el-table-column width="100" align="center">
          <template #header>
            接新任务<InfoTip content="关掉后这台机器在跑的任务照常跑完，主机不再往这台派新任务。维护、重启机器前先关掉。离线的机器可以在这里删掉。" />
          </template>
          <template #default="{ row }">
            <el-popconfirm v-if="!row.online && isAdmin" title="从列表里删掉这台已下线的机器？" width="230"
                           @confirm="remove(row)">
              <template #reference>
                <span class="pop-ref"><IconAction icon="delete" tip="删除这台机器" /></span>
              </template>
            </el-popconfirm>
            <el-switch v-else-if="row.role !== 'master'" :model-value="row.accept_tasks" size="small"
                       :disabled="!isAdmin"
                       :loading="toggling === row.node_id" @change="(v: any) => setAccept(row, !!v)" />
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
      </el-table>

      <el-card shadow="never" class="block how-to">
        <template #header>怎么加一台机器</template>
        <ol class="steps">
          <li>新机器上部署同一版程序，<code>config.yaml</code> 的 <code>mysql</code>、<code>redis</code> 填和这台<b>一样</b>的库。</li>
          <li>
            加上集群配置（每台机器各写各的）：
            <pre class="code">cluster:
  role: worker              # 只跑任务；主机写 all（主 + 从）或 master（只协调）
  node_id: worker-02        # 机器编号，不能重复
  node_name: 采集机 2        # 页面上显示的名字
  advertise_url: http://10.0.0.12:8000   # 主机能访问到这台的地址</pre>
          </li>
          <li>启动后几秒内出现在上面的列表里；在「账号管理」添加账号时选这台机器，到这台上扫码登录。</li>
        </ol>
        <div class="muted">
          页面打开哪台都行：账号的登录、采集 Cookie、实时画面、任务实时日志，会自动转到账号 / 任务所在的那台机器。
        </div>
      </el-card>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { clusterApi, settingsApi, type ClusterNode, type ClusterOverview } from '../api'
import { CHANNEL_COLORS, channelLabel } from '../constants'
import { clusterInfo } from '../composables/cluster'
import { isAdmin } from '../composables/auth'

const info = ref<ClusterOverview | null>(null)
const loading = ref(false)
const toggling = ref('')
const redisWarn = ref(false)
let timer: number | undefined

const onlineNodes = computed(() => (info.value?.nodes || []).filter((n) => n.online))
const onlineCount = computed(() => onlineNodes.value.length)
const runningTotal = computed(() =>
  onlineNodes.value.reduce((s, n) => s + (n.role === 'master' ? 0 : n.running_count), 0))
const capacityTotal = computed(() =>
  onlineNodes.value.reduce((s, n) => s + (n.role === 'master' ? 0 : n.max_running), 0))
const leaderName = computed(() => {
  const id = info.value?.leader
  if (!id) return '无'
  return info.value?.nodes.find((n) => n.node_id === id)?.node_name || id
})
const versions = computed(() =>
  [...new Set(onlineNodes.value.map((n) => n.version).filter(Boolean))])
const versionMismatch = computed(() => versions.value.length > 1)
const unassignedTotal = computed(() =>
  Object.values(info.value?.unassigned_accounts || {}).reduce((s, v) => s + v.total, 0))

function heartbeatText(row: ClusterNode): string {
  if (row.idle_seconds == null) return '已停止'
  if (row.online) return `${row.idle_seconds} 秒前心跳`
  const m = Math.round(row.idle_seconds / 60)
  return m < 60 ? `${m} 分钟没心跳` : `${Math.round(m / 60)} 小时没心跳`
}

async function load() {
  loading.value = true
  try {
    info.value = await clusterApi.overview()
    clusterInfo.value = info.value
    const health = await settingsApi.health()
    redisWarn.value = info.value.nodes.length > 1 && health.redis_enabled !== false && !health.redis_is_real
      || info.value.nodes.length > 1 && health.redis_enabled === false
  } finally {
    loading.value = false
  }
}

async function setAccept(row: ClusterNode, on: boolean) {
  toggling.value = row.node_id
  try {
    await clusterApi.setAccept(row.node_id, on)
    ElMessage.success(on ? `${row.node_name} 恢复接收新任务` : `${row.node_name} 暂停接收新任务，在跑的会跑完`)
    await load()
  } finally {
    toggling.value = ''
  }
}

async function remove(row: ClusterNode) {
  await clusterApi.remove(row.node_id)
  ElMessage.success('已删除')
  await load()
}

onMounted(() => {
  load()
  timer = window.setInterval(load, 10_000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<style scoped>
.header-actions { display: flex; align-items: center; gap: 12px; }
.summary {
  display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px;
}
.sum-item {
  background: var(--el-bg-color); border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px; padding: 12px 16px;
}
.sum-label { font-size: 12px; color: var(--smc-text-secondary); }
.sum-value { font-size: 20px; font-weight: 600; margin-top: 4px; }
.sum-sub { font-size: 12px; color: var(--smc-text-secondary); margin-top: 2px; }
.alert { margin-bottom: 12px; }
.link { color: var(--el-color-primary); text-decoration: none; }
.link:hover { text-decoration: underline; }
a.sum-item { color: inherit; text-decoration: none; display: block; }
.sum-item.is-link { cursor: pointer; transition: border-color 0.15s, box-shadow 0.15s; }
.sum-item.is-link:hover { border-color: var(--el-color-primary-light-5); box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06); }
a.acc-chip { color: inherit; text-decoration: none; }
a.acc-chip:hover { color: var(--el-color-primary); }
.node-name { display: flex; align-items: center; gap: 6px; font-weight: 500; }
.node-sub { font-size: 12px; color: var(--smc-text-secondary); line-height: 18px; }
.state { display: inline-flex; align-items: center; gap: 6px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot.ok { background: #22c55e; }
.dot.bad { background: #ef4444; }
.acc-chips { display: flex; flex-wrap: wrap; gap: 4px 8px; }
.acc-chip { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; white-space: nowrap; }
.acc-chip.is-none { color: var(--el-color-danger); }
.acc-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.nowrap { white-space: nowrap; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.block { margin-top: 16px; }
.steps { margin: 0 0 8px; padding-left: 20px; line-height: 1.9; }
.code {
  margin: 6px 0; padding: 10px 12px; border-radius: 6px; font-size: 12px; line-height: 1.6;
  background: var(--el-fill-color-light); overflow-x: auto;
}
.pop-ref { display: inline-flex; }
@media (max-width: 900px) {
  .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
