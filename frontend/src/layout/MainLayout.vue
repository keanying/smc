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
    <el-aside width="188px" class="aside-menu">
      <div class="aside-pill">
      <div class="logo">
        <!-- LOGO 原样使用（深色侧栏上垫一块白底，深蓝的「云听」才看得清），只按高度等比缩放 -->
        <span class="logo-tile"><img class="logo-img" src="/logo.png" alt="CloudSense 云听" /></span>
        <div class="logo-text">
          <div class="logo-title">CloudSense</div>
          <div class="logo-sub">云听社媒舆情分析</div>
        </div>
      </div>
      <el-menu
        router
        :default-active="route.path"
        class="side-menu"
        background-color="transparent"
        text-color="rgba(255,255,255,0.72)"
        active-text-color="#ffffff"
      >
        <el-menu-item index="/">
          <el-icon><HomeFilled /></el-icon><span>概览</span>
        </el-menu-item>
        <el-menu-item index="/scenics">
          <el-icon><LocationFilled /></el-icon><span>景区管理</span>
        </el-menu-item>
        <el-menu-item index="/archive">
          <el-icon><Collection /></el-icon><span>景区档案</span>
        </el-menu-item>
        <el-menu-item index="/tasks">
          <el-icon><List /></el-icon><span>任务管理</span>
        </el-menu-item>
        <el-menu-item index="/data">
          <el-icon><DataAnalysis /></el-icon><span>数据中心</span>
        </el-menu-item>
        <el-menu-item index="/labeling">
          <el-icon><MagicStick /></el-icon><span>标注审核</span>
        </el-menu-item>
        <!-- 指标计算是可选模块：系统设置里打开才出现 -->
        <el-menu-item v-if="metricsEnabled" index="/metrics">
          <el-icon><TrendCharts /></el-icon><span>指标计算</span>
        </el-menu-item>
        <el-menu-item index="/accounts">
          <el-icon><User /></el-icon><span>账号管理</span>
        </el-menu-item>
        <el-menu-item index="/cluster">
          <el-icon><Connection /></el-icon><span>集群</span>
        </el-menu-item>
        <!-- 景区盒子：登记、分景区、发令牌（只有管理员） -->
        <el-menu-item v-if="isAdmin" index="/boxes">
          <el-icon><Box /></el-icon><span>盒子管理</span>
        </el-menu-item>
        <!-- 系统设置（含用户管理）只有管理员能用 -->
        <el-menu-item v-if="isAdmin" index="/settings">
          <el-icon><Setting /></el-icon><span>系统设置</span>
        </el-menu-item>
      </el-menu>
      <!-- 侧栏底部：退出登录 -->
      <div v-if="currentUser" class="aside-foot">
        <button class="aside-exit" type="button" title="退出登录" @click="onUserCommand('logout')">
          <el-icon><SwitchButton /></el-icon><span>退出登录</span>
        </button>
      </div>
      </div>
    </el-aside>
    <el-container>
      <el-header height="68px" class="app-header">
        <div class="header-right">
          <span v-if="health" class="header-meta">
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
              <span class="meta-item">
                运行中任务 {{ health.scheduler.running_count }}/{{ health.scheduler.max_running }}
              </span>
            </template>
            <!-- 浏览器占用。一个 Chromium 实测 ~700MB/10 个进程，是整台机器
                 最稀缺的资源——全量采集把机器跑死那次，事后完全查不到
                 "当时开了几个浏览器"，只能对着监控猜。 -->
            <template v-if="health.browser_capacity">
              <span class="meta-sep" />
              <el-tooltip :content="browserTip" placement="bottom">
                <span class="meta-item" :class="{ 'is-warn': browserBusy }">
                  浏览器 {{ health.browser_capacity.active }}/{{ health.browser_capacity.limit }}
                  <template v-if="health.browser_capacity.waiting">
                    （{{ health.browser_capacity.waiting }} 个排队）
                  </template>
                </span>
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
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import {
  ArrowDown, Box, Collection, Connection, Setting, SwitchButton, TrendCharts,
} from '@element-plus/icons-vue'
import { authApi, settingsApi } from '../api'
import { loadFeatures, metricsEnabled } from '../composables/features'
import { currentUser, isAdmin, loadAuth } from '../composables/auth'
import ChangePasswordDialog from '../components/ChangePasswordDialog.vue'

const route = useRoute()
const router = useRouter()
const health = ref<Record<string, any> | null>(null)
const passwordVisible = ref(false)

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

const browserTip = computed(() => {
  const c = health.value?.browser_capacity
  if (!c) return ''
  const parts = [
    `同时最多开 ${c.limit} 个浏览器，现在开着 ${c.active} 个`,
  ]
  if (c.waiting) parts.push(`${c.waiting} 个任务在排队等名额`)
  if (c.memory_available_gb != null && c.memory_total_gb != null) {
    parts.push(`内存 ${c.memory_available_gb}/${c.memory_total_gb} GB 可用`)
  }
  parts.push(`一个浏览器约占 ${c.footprint_gb} GB、10 个进程`)
  return parts.join('；')
})

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
  height: 100vh;
  background: var(--smc-bg);
}
.app-shell {
  height: 100%;
  background: var(--smc-bg);
  overflow: hidden;
}

.aside-menu {
  padding: 12px 0 12px 12px;
  background: transparent;
  z-index: 10;
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

.logo {
  height: 60px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.14);
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

.side-menu {
  border-right: none;
  flex: 1;
  padding: 10px 8px;
  overflow-y: auto;
}
.side-menu :deep(.el-menu-item) {
  height: 36px;
  line-height: 36px;
  margin: 2px 0;
  padding: 0 8px !important;
  border-radius: 11px;
  font-size: 13px;
  transition: background-color 0.15s ease, color 0.15s ease;
}
/* 图标放进一个小圆角方块里（参考图里选中项的样子） */
.side-menu :deep(.el-menu-item .el-icon) {
  font-size: 14px;
  margin-right: 8px;
  width: 24px;
  height: 24px;
  border-radius: 8px;
  transition: background-color 0.15s ease;
}
.side-menu :deep(.el-menu-item:hover) {
  background: rgba(255, 255, 255, 0.10);
  color: #fff;
}
.side-menu :deep(.el-menu-item.is-active) {
  background: rgba(255, 255, 255, 0.18);
  color: #fff;
  font-weight: 600;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.12);
}
.side-menu :deep(.el-menu-item.is-active .el-icon) {
  background: #fff;
  color: var(--smc-primary);
  box-shadow: 0 4px 10px rgba(40, 30, 110, 0.25);
}
.aside-foot {
  padding: 8px;
  border-top: 1px solid rgba(255, 255, 255, 0.14);
}
.aside-exit {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border: none;
  border-radius: 11px;
  background: transparent;
  color: rgba(255, 255, 255, 0.78);
  font-size: 13px;
  cursor: pointer;
  transition: background-color 0.15s ease, color 0.15s ease;
}
.aside-exit .el-icon {
  width: 24px; height: 24px; border-radius: 8px; font-size: 14px;
  background: rgba(255, 255, 255, 0.14);
}
.aside-exit:hover { background: rgba(255, 255, 255, 0.10); color: #fff; }

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
  padding: 0 28px;
}
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
