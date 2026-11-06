<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!-- 铺满窗口；左边是紫色渐变的圆角侧栏 -->
  <div class="app-frame">
  <el-container class="app-shell">
    <!-- 窄屏自动收成只显示图标（也可以点侧栏边上的小圆钮手动收起 / 展开）。
         收起的宽度刚好让图标停在原来那一列：展开 / 收起时图标不跳 -->
    <el-aside :width="collapsed ? '78px' : '200px'" class="aside-menu" :class="{ 'is-collapsed': collapsed }">
      <div class="aside-pill">
        <div class="logo">
          <!-- LOGO 原样使用（深色侧栏上垫一块白底，深蓝的「云听」才看得清），只按高度等比缩放 -->
          <span class="logo-tile"><img class="logo-img" src="/logo.png" alt="CloudSense 云听" /></span>
          <div class="logo-text">
            <div class="logo-title">CloudSense</div>
            <div class="logo-sub">云听社媒舆情分析</div>
          </div>
        </div>
        <SideMenu :items="menuItems" :actions="menuActions" :active="activeMenu" :collapsed="collapsed" />
      </div>
      <!-- 收起 / 展开：贴在侧栏右边缘的小圆钮 -->
      <el-tooltip :content="collapsed ? '展开菜单' : '收起菜单'" placement="right" :show-after="200">
        <button class="aside-toggle" type="button" :aria-label="collapsed ? '展开菜单' : '收起菜单'" @click="toggleCollapse">
          <el-icon><component :is="collapsed ? ChevronForward : ChevronBack" /></el-icon>
        </button>
      </el-tooltip>
    </el-aside>
    <el-container>
      <el-header height="68px" class="app-header">
        <div class="header-right">
          <!-- 窗口窄的时候状态胶囊放不下，先藏起来（齿轮还在，健康状态照样看得到） -->
          <span v-if="health && !narrow" class="header-meta">
            <span class="meta-item">
              <span class="health-dot sm" :class="redisOk ? 'ok' : 'bad'" />
              {{ redisText }}
            </span>
            <!-- 多台机器时显示整个集群的合计，点一下去集群页看每台的情况 -->
            <template v-if="health.cluster && health.cluster.nodes_online > 1">
              <span class="meta-sep" />
              <el-tooltip :content="clusterTip" placement="bottom">
                <router-link to="/cluster" class="meta-item meta-link">
                  集群 {{ health.cluster.nodes_online }} 台 · 运行中任务
                  {{ health.cluster.running }}/{{ health.cluster.max_running }}
                </router-link>
              </el-tooltip>
            </template>
            <template v-else-if="health.scheduler">
              <span class="meta-sep" />
              <!-- 悬停：在跑哪些任务；管理员点一下直接去改上限（系统设置 → 调度与浏览器） -->
              <el-tooltip placement="bottom-end" popper-class="meta-tip" :show-after="150" @show="loadHealth">
                <template #content>
                  <div class="tip-title">
                    运行中任务 {{ health.scheduler.running_count }}/{{ health.scheduler.max_running }}
                  </div>
                  <ul v-if="runningTasks.length" class="tip-list">
                    <li v-for="t in runningTasks" :key="t.task_id">
                      <b>{{ t.task_name }}</b>
                      <div class="tip-sub">
                        {{ [t.scenic_name, (t.channels || []).map(channelLabel).join('、')].filter(Boolean).join(' · ') }}
                        · 已运行 {{ durationText(t.running_seconds) }}
                      </div>
                    </li>
                  </ul>
                  <div v-else class="tip-sub">现在没有任务在跑</div>
                </template>
                <component :is="isAdmin ? 'router-link' : 'span'" :to="RUNTIME_SETTINGS"
                           class="meta-item" :class="{ 'meta-link': isAdmin }">
                  运行中任务 {{ health.scheduler.running_count }}/{{ health.scheduler.max_running }}
                </component>
              </el-tooltip>
            </template>
            <!-- 浏览器占用。一个 Chromium 实测 ~700MB/10 个进程，是整台机器
                 最稀缺的资源——全量采集把机器跑死那次，事后完全查不到
                 "当时开了几个浏览器"，只能对着监控猜。 -->
            <template v-if="health.browser_capacity">
              <span class="meta-sep" />
              <el-tooltip placement="bottom-end" popper-class="meta-tip" :show-after="150" @show="loadHealth">
                <template #content>
                  <div class="tip-title">
                    浏览器 {{ health.browser_capacity.active }}/{{ health.browser_capacity.limit }}
                    <span class="tip-sub">（{{ health.browser_capacity.configured ? '手工设置的上限' : '上限按内存自动推算' }}）</span>
                  </div>
                  <ul v-if="browserHolders.length" class="tip-list">
                    <li v-for="(h, i) in browserHolders" :key="i">
                      <b>{{ h.label }}</b><span class="tip-sub"> · 已占 {{ durationText(h.seconds) }}</span>
                    </li>
                  </ul>
                  <div v-else class="tip-sub">现在没有开着的浏览器</div>
                  <div v-if="health.browser_capacity.waiting" class="tip-warn">
                    {{ health.browser_capacity.waiting }} 个在排队等名额
                  </div>
                  <dl class="tip-kv">
                    <template v-if="health.browser_capacity.memory_available_gb != null">
                      <dt>可用内存</dt>
                      <dd>{{ health.browser_capacity.memory_available_gb }} / {{ health.browser_capacity.memory_total_gb }} GB</dd>
                    </template>
                    <dt>暂停阈值</dt>
                    <dd>低于 {{ health.browser_capacity.min_free_gb }} GB 不开新浏览器</dd>
                    <dt>单个浏览器</dt>
                    <dd>约 {{ health.browser_capacity.footprint_gb }} GB · 10 个进程</dd>
                  </dl>
                </template>
                <component :is="isAdmin ? 'router-link' : 'span'" :to="RUNTIME_SETTINGS"
                           class="meta-item" :class="{ 'is-warn': browserBusy, 'meta-link': isAdmin }">
                  浏览器 {{ health.browser_capacity.active }}/{{ health.browser_capacity.limit }}
                  <template v-if="health.browser_capacity.waiting">
                    （{{ health.browser_capacity.waiting }} 个排队）
                  </template>
                </component>
              </el-tooltip>
            </template>
          </span>
          <!-- 系统健康：一直转的齿轮。绿色=正常，红色=有问题。
               为什么是"一直转"而不是"有问题才转"：不转的图标和
               页面卡死、JS 挂掉长得一模一样，正好在最需要它的时候
               失去意义。转着就是"前端还活着，而且刚问过后端"。 -->
          <el-tooltip :content="healthText" placement="bottom">
            <el-icon
              class="health-gear" :class="healthOk ? 'is-ok' : 'is-bad'"
              @click="loadHealth"
            >
              <Setting />
            </el-icon>
          </el-tooltip>
          <!-- 显示比例：默认按屏幕宽度自动缩放（composables/zoom.ts） -->
          <el-dropdown trigger="click" @command="setZoomMode">
            <span class="zoom-chip" title="显示比例">
              <el-icon><Monitor /></el-icon>{{ Math.round(zoom * 100) }}%
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-for="o in ZOOM_OPTIONS" :key="o.value" :command="o.value"
                                  :class="{ 'is-current': o.value === zoomMode }">
                  {{ o.label }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <!-- 当前登录的人：改密码、退出 -->
          <el-dropdown v-if="currentUser" trigger="click" @command="onUserCommand">
            <span class="user-chip">
              <span class="user-avatar">{{ (currentUser.display_name || currentUser.username).slice(0, 1) }}</span>
              <span class="user-name">{{ currentUser.display_name || currentUser.username }}</span>
              <span class="user-role">{{ currentUser.role_label }}</span>
              <el-icon class="user-caret"><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>登录名：{{ currentUser.username }}</el-dropdown-item>
                <el-dropdown-item command="password" divided>修改密码</el-dropdown-item>
                <el-dropdown-item command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="app-main">
        <router-view />
      </el-main>
      <ChangePasswordDialog v-model="passwordVisible" />
    </el-container>
  </el-container>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { ArrowDown, Monitor, Setting } from '@element-plus/icons-vue'
import { ChevronBack, ChevronForward } from '@vicons/ionicons5'
import { authApi, settingsApi } from '../api'
import { loadFeatures, metricsEnabled } from '../composables/features'
import { currentUser, isAdmin, loadAuth } from '../composables/auth'
import ChangePasswordDialog from '../components/ChangePasswordDialog.vue'
import { channelLabel } from '../constants'
import { layoutWidth, setZoomMode, zoom, zoomMode, ZOOM_OPTIONS } from '../composables/zoom'
import SideMenu, { type NavAction, type NavItem } from './SideMenu.vue'
import { NAV_ICONS } from './navIcons'

const route = useRoute()
const router = useRouter()
const health = ref<Record<string, any> | null>(null)
const passwordVisible = ref(false)
/** 侧边栏高亮哪一项：子页面（新建任务、任务详情、运行详情）高亮它所属的那一项 */
const activeMenu = computed(() => {
  const menu = route.meta.menu as string | undefined
  if (menu) return menu
  if (route.path.startsWith('/tasks')) return '/tasks'
  return route.path
})
/** 侧边栏菜单。指标计算是可选模块：系统设置里打开才出现；系统设置里也有档案、盒子管理（非管理员只看得到档案） */
const menuItems = computed<NavItem[]>(() => [
  { key: 'home', path: '/', label: '概览', icon: NAV_ICONS.home },
  { key: 'scenics', path: '/scenics', label: '目标管理', icon: NAV_ICONS.target },
  // 任务管理：任务列表 + 运行记录（每执行一次一条）
  {
    key: 'task-group', label: '任务管理', icon: NAV_ICONS.task,
    children: [{ path: '/tasks', label: '任务列表' }, { path: '/runs', label: '运行记录' }],
  },
  { key: 'data', path: '/data', label: '数据中心', icon: NAV_ICONS.data },
  { key: 'labeling', path: '/labeling', label: '标注审核', icon: NAV_ICONS.label },
  ...(metricsEnabled.value ? [{ key: 'metrics', path: '/metrics', label: '指标计算', icon: NAV_ICONS.metrics }] : []),
  { key: 'accounts', path: '/accounts', label: '账号管理', icon: NAV_ICONS.account },
  { key: 'cluster', path: '/cluster', label: '集群', icon: NAV_ICONS.cluster },
  { key: 'settings', path: '/settings', label: '系统设置', icon: NAV_ICONS.settings },
])
const menuActions = computed<NavAction[]>(() => (currentUser.value
  ? [{ key: 'logout', label: '退出登录', icon: NAV_ICONS.logout, onClick: () => onUserCommand('logout') }]
  : []))
/** 顶栏「运行中任务」「浏览器」点过去改上限的地方 */
const RUNTIME_SETTINGS = { path: '/settings', query: { tab: 'runtime' } }

/**
 * 侧边栏收起：内容区摆不开（缩放后的布局宽度不到 1200）时自动收成图标。
 * 手动点过就按手动的来，记在本机；窗口宽窄跨过阈值时清掉手动状态，重新跟着屏幕走。
 */
const COLLAPSE_BELOW = 1200
const COLLAPSE_KEY = 'smc.ui.asideCollapsed'
const autoCollapsed = computed(() => layoutWidth.value < COLLAPSE_BELOW)
const manualCollapsed = ref<boolean | null>(
  localStorage.getItem(COLLAPSE_KEY) === null ? null : localStorage.getItem(COLLAPSE_KEY) === '1')
const collapsed = computed(() => manualCollapsed.value ?? autoCollapsed.value)
watch(autoCollapsed, () => {
  manualCollapsed.value = null
  localStorage.removeItem(COLLAPSE_KEY)
})
function toggleCollapse() {
  manualCollapsed.value = !collapsed.value
  localStorage.setItem(COLLAPSE_KEY, manualCollapsed.value ? '1' : '0')
}
/** 顶栏放不下状态胶囊的宽度 */
const narrow = computed(() => layoutWidth.value < 1100)

async function onUserCommand(command: string) {
  if (command === 'password') {
    passwordVisible.value = true
    return
  }
  if (command === 'logout') {
    await ElMessageBox.confirm('确定退出登录？', '退出', { type: 'info' })
    await authApi.logout()
    await loadAuth(true)
    router.replace('/login')
  }
}
let timer: number | undefined

/**
 * 系统健康。
 *
 * ⚠️ 判定要**同时**看 mysql 和 redis：只看 mysql 的话，Redis 断了
 * 齿轮还是绿的，而右边那行明晃晃写着「Redis 未连接」——
 * 两个指示互相打架，用户就再也不会信这个齿轮了。
 * Redis 没启用（配置里关掉）不算异常。
 */
const healthOk = computed(() => !!health.value?.mysql && redisOk.value)

/**
 * ⚠️ 不能看 `redis` 这个字段：Redis 连不上时后端会退化成进程内缓存，
 * 而内存实现的 ping 恒为 true——于是"连不上"和"连上了"返回的是同一个值。
 * 要看 `redis_is_real`。没启用 Redis（配置里主动关掉）不算异常。
 */
const redisOk = computed(() => {
  const h = health.value
  if (!h) return false
  return h.redis_enabled === false ? true : !!h.redis_is_real
})

const redisText = computed(() => {
  const h = health.value
  if (!h) return 'Redis 检查中'
  if (h.redis_enabled === false) return 'Redis 未启用'
  return h.redis_is_real ? 'Redis 已连接' : 'Redis 连不上（已退化为进程内缓存）'
})

const clusterTip = computed(() => {
  const c = health.value?.cluster
  if (!c) return ''
  return `本机：${c.node_name}${c.is_leader ? '（当前主机）' : ''}；在线 ${c.nodes_online} 台机器`
})

const browserBusy = computed(() => {
  const c = health.value?.browser_capacity
  return !!c && (c.waiting > 0 || c.active >= c.limit)
})

/** 在跑的任务（本机）。悬停「运行中任务」时列出来 */
const runningTasks = computed<Array<Record<string, any>>>(() => health.value?.scheduler?.running_tasks || [])

/** 浏览器名额被谁占着：把闸门里的标识（login/平台/账号、cookie/平台、平台/账号）翻成人话 */
const browserHolders = computed(() => (health.value?.browser_capacity?.holders || []).map(
  (h: { who: string; seconds: number }) => ({ label: holderLabel(h.who), seconds: h.seconds })))

function holderLabel(who: string): string {
  const parts = String(who || '').split('/')
  if (parts[0] === 'login') return `登录窗口 · ${channelLabel(parts[1] || '')} · ${parts.slice(2).join('/') || '—'}`
  if (parts[0] === 'cookie') return `采集 Cookie · ${channelLabel(parts[1] || '')}`
  if (who === 'cookie-inject') return '导入 Cookie'
  if (parts.length >= 2) return `采集 · ${channelLabel(parts[0])} · 账号 ${parts.slice(1).join('/') || '—'}`
  return who || '任务'
}

function durationText(seconds: number): string {
  const n = Math.max(0, Number(seconds) || 0)
  if (n < 60) return `${n} 秒`
  if (n < 3600) return `${Math.floor(n / 60)} 分钟`
  return `${Math.floor(n / 3600)} 小时 ${Math.floor((n % 3600) / 60)} 分`
}

const healthText = computed(() => {
  const h = health.value
  if (!h) return '正在检查服务状态……'
  if (!h.mysql) return '数据库连不上'
  if (!redisOk.value) return 'Redis 连不上，代理与去重状态只在本进程内有效'
  return '服务正常（点一下立即重新检查）'
})

async function loadHealth() {
  try {
    health.value = await settingsApi.health()
  } catch {
    health.value = { mysql: false, redis: false }
  }
}

onMounted(() => {
  loadHealth()
  loadFeatures()
  timer = window.setInterval(loadHealth, 30_000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<style scoped>
/* 铺满整个浏览器窗口，四周不留边 */
.app-frame {
  height: var(--vh100);
  background: var(--smc-bg);
}
.app-shell {
  height: 100%;
  background: var(--smc-bg);
  overflow: hidden;
}

.aside-menu {
  position: relative;
  padding: 12px 0 12px 12px;
  background: transparent;
  overflow: visible;
  z-index: 10;
  transition: width 0.2s ease;
}
/* 紫色渐变的圆角侧栏 */
.aside-pill {
  height: 100%;
  display: flex;
  flex-direction: column;
  border-radius: 20px;
  background: var(--smc-gradient);
  box-shadow: 0 16px 36px rgba(93, 78, 201, 0.32);
  overflow: hidden;
}

/* LOGO 白块的中心和菜单图标同一列（收起时正好居中） */
.logo {
  flex: none;
  height: 60px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 0 0 8px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.14);
  white-space: nowrap;
  overflow: hidden;
}
.logo-tile {
  display: inline-flex;
  align-items: center;
  padding: 3px 5px;
  border-radius: 8px;
  background: #fff;
  flex-shrink: 0;
  box-shadow: 0 4px 10px rgba(40, 30, 110, 0.18);
}
.logo-img {
  display: block;
  height: 24px;
  width: auto;
}
.logo-text { transition: opacity 0.15s ease; }
.logo-title {
  color: #fff;
  font-weight: 700;
  font-size: 13px;
  letter-spacing: 0.4px;
  line-height: 1.3;
}
.logo-sub {
  color: rgba(255, 255, 255, 0.65);
  font-size: 9.5px;
  white-space: nowrap;
  letter-spacing: 0.3px;
}
.aside-menu.is-collapsed .logo-text { opacity: 0; }

/* 收起 / 展开的小圆钮：骑在侧栏右边缘、LOGO 分隔线的位置 */
.aside-toggle {
  position: absolute;
  top: 61px;
  right: -11px;
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: #fff;
  color: var(--smc-primary);
  font-size: 12px;
  box-shadow: 0 2px 8px rgba(40, 30, 110, 0.28);
  cursor: pointer;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}
.aside-toggle:hover { transform: scale(1.12); box-shadow: 0 4px 12px rgba(40, 30, 110, 0.36); }
.aside-toggle:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }

.health-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  display: inline-block;
}
.health-dot.sm { width: 6px; height: 6px; }
.meta-link { color: inherit; text-decoration: none; }
.user-chip {
  display: inline-flex; align-items: center; gap: 8px; cursor: pointer; margin-left: 4px;
  padding: 4px 12px 4px 4px; border-radius: 22px; transition: box-shadow 0.15s;
  background: #fff; box-shadow: var(--smc-shadow);
}
.user-chip:hover { box-shadow: var(--smc-shadow-hover); }
.user-avatar {
  width: 32px; height: 32px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
  background: var(--smc-gradient); color: #fff; font-size: 14px; font-weight: 600;
}
.user-name { font-size: 13px; color: var(--el-text-color-primary); max-width: 120px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.user-role { font-size: 12px; color: var(--smc-text-secondary); }
.user-caret { font-size: 12px; color: var(--smc-text-secondary); }
.meta-link:hover { color: var(--el-color-primary); }
.health-dot.ok { background: #22c55e; }
.health-dot.bad { background: #ef4444; }

/* 系统健康齿轮：绿色转=正常，红色转=有问题 */
.health-gear {
  font-size: 16px;
  cursor: pointer;
  animation: gear-spin 3.2s linear infinite;
  transform-origin: center;
}
.health-gear.is-ok { color: #22c55e; }
.health-gear.is-bad { color: #ef4444; animation-duration: 1.1s; }
@keyframes gear-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
/* 关掉动画的用户（系统设置里的"减少动态效果"）看到的是静止的齿轮，
   颜色照样区分正常和异常——别把唯一的状态信息藏在动画里。 */
@media (prefers-reduced-motion: reduce) {
  .health-gear { animation: none; }
}

.app-header {
  background: transparent;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 28px;
}
.zoom-chip {
  display: inline-flex; align-items: center; justify-content: center; gap: 4px;
  height: 32px; border: none; border-radius: 16px; background: #fff; box-shadow: var(--smc-shadow);
  color: var(--smc-text-secondary); cursor: pointer; transition: box-shadow 0.15s, color 0.15s;
}
.zoom-chip { padding: 0 12px; font-size: 12px; }
.zoom-chip:hover { box-shadow: var(--smc-shadow-hover); color: var(--el-color-primary); }
:global(.el-dropdown-menu__item.is-current) { color: var(--el-color-primary); font-weight: 600; }

/* 顶栏悬停卡片：分行列出在跑的任务、浏览器被谁占着（弹层在 body 下，要用 :global） */
:global(.meta-tip.el-popper) { max-width: 380px; line-height: 1.6; font-size: 12px; padding: 10px 12px; }
:global(.meta-tip .tip-title) { font-weight: 600; font-size: 13px; margin-bottom: 4px; }
:global(.meta-tip .tip-list) { margin: 4px 0 6px; padding-left: 16px; }
:global(.meta-tip .tip-list li) { margin: 2px 0; }
:global(.meta-tip .tip-sub) { opacity: 0.72; font-weight: normal; }
:global(.meta-tip .tip-warn) { color: #fbbf24; margin: 2px 0; }
:global(.meta-tip .tip-line) { opacity: 0.85; }
/* 内存这几项：左边项名、右边数值，两列对齐；和上面的占用列表用一道细线隔开 */
:global(.meta-tip .tip-kv) {
  display: grid; grid-template-columns: auto 1fr; column-gap: 12px; row-gap: 2px;
  margin: 8px 0 0; padding-top: 8px; border-top: 1px solid rgba(255, 255, 255, 0.18);
}
:global(.meta-tip .tip-kv dt) { opacity: 0.65; }
:global(.meta-tip .tip-kv dd) { margin: 0; }

/* 顶栏只放右边的状态和当前用户（页面标题各页自己有，不再重复显示面包屑） */
.header-right { display: flex; align-items: center; gap: 12px; margin-left: auto; }
/* 右上角的状态做成一条白色胶囊 */
.header-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  color: var(--smc-text-secondary);
  background: #fff;
  padding: 8px 16px;
  border-radius: 20px;
  box-shadow: var(--smc-shadow);
}
.meta-item { display: inline-flex; align-items: center; gap: 6px; }
.meta-sep { width: 1px; height: 12px; background: var(--smc-border); }
.meta-item.is-warn { color: #d97706; font-weight: 500; }

.app-main {
  background-color: transparent;
  padding: 8px 28px 28px;
  overflow-y: auto;
  /* 不显示滚动条（滚轮、触控板照常能滚）：贴着圆角窗口右边一长条，很扎眼 */
  scrollbar-width: none;
}
.app-main::-webkit-scrollbar { display: none; }
</style>
