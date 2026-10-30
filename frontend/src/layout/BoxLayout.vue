<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!-- 景区版：深色外框，左边一列只有图标的窄侧栏，右边一块白色大圆角面板 -->
  <div class="box-frame">
    <aside class="box-rail">
      <!-- LOGO 原样使用，垫一块奶油色圆角底 -->
      <div class="rail-logo" title="CloudSense 云听"><img src="/logo.png" alt="CloudSense 云听" /></div>
      <nav class="rail-nav">
        <el-tooltip v-for="item in menu" :key="item.path" :content="item.label" placement="right"
                    :show-after="200">
          <button type="button" class="rail-btn" :class="{ 'is-active': isActive(item.path) }"
                  :aria-label="item.label" @click="router.push(item.path)">
            <el-icon><component :is="item.icon" /></el-icon>
          </button>
        </el-tooltip>
      </nav>
      <div v-if="currentUser" class="rail-foot">
        <el-tooltip content="修改密码" placement="right" :show-after="200">
          <button type="button" class="rail-btn" aria-label="修改密码" @click="passwordVisible = true">
            <el-icon><Key /></el-icon>
          </button>
        </el-tooltip>
        <el-tooltip content="退出登录" placement="right" :show-after="200">
          <button type="button" class="rail-btn" aria-label="退出登录" @click="logout">
            <el-icon><SwitchButton /></el-icon>
          </button>
        </el-tooltip>
      </div>
    </aside>

    <section class="box-panel">
      <header class="box-top">
        <div class="box-top-right">
          <span v-if="health" class="box-status">
            <el-tooltip :content="healthText" placement="bottom">
              <span class="status-item">
                <span class="status-dot" :class="healthOk ? 'ok' : 'bad'" />{{ healthOk ? '盒子运行正常' : '盒子异常' }}
              </span>
            </el-tooltip>
            <template v-if="health.scheduler">
              <span class="status-sep" />
              <span class="status-item">
                采集中 {{ health.scheduler.running_count }}/{{ health.scheduler.max_running }}
              </span>
            </template>
            <template v-if="health.browser_capacity">
              <span class="status-sep" />
              <span class="status-item">
                浏览器 {{ health.browser_capacity.active }}/{{ health.browser_capacity.limit }}
              </span>
            </template>
          </span>
          <span v-if="currentUser" class="box-user">
            <span class="box-avatar">{{ (currentUser.display_name || currentUser.username).slice(0, 1) }}</span>
            <span class="box-user-text">
              <span class="box-user-name">{{ currentUser.display_name || currentUser.username }}</span>
              <span class="box-user-role">{{ currentUser.role_label }}</span>
            </span>
          </span>
        </div>
      </header>
      <main class="box-main">
        <router-view />
      </main>
    </section>
    <ChangePasswordDialog v-model="passwordVisible" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import {
  DataAnalysis, HomeFilled, Key, List, MagicStick, SwitchButton, User,
} from '@element-plus/icons-vue'
import { authApi, settingsApi } from '../api'
import { currentUser, loadAuth } from '../composables/auth'
import ChangePasswordDialog from '../components/ChangePasswordDialog.vue'

const route = useRoute()
const router = useRouter()
const passwordVisible = ref(false)
const health = ref<Record<string, any> | null>(null)

/** 景区版只有这几项：景区管理 / 景区档案 / 指标计算 / 集群 / 系统设置都在总部 */
const menu = [
  { path: '/', label: '概览', icon: HomeFilled },
  { path: '/tasks', label: '任务管理', icon: List },
  { path: '/data', label: '数据中心', icon: DataAnalysis },
  { path: '/labeling', label: '标注审核', icon: MagicStick },
  { path: '/accounts', label: '账号管理', icon: User },
]


function isActive(path: string): boolean {
  return path === '/' ? route.path === '/' : route.path.startsWith(path)
}

async function logout() {
  await ElMessageBox.confirm('确定退出登录？', '退出', { type: 'info' })
  await authApi.logout()
  await loadAuth(true)
  router.replace('/login')
}

// 和总部版顶栏一样：同时看数据库和 Redis（没启用 Redis 不算异常）
const healthOk = computed(() => {
  const h = health.value
  if (!h?.mysql) return false
  return h.redis_enabled === false ? true : !!h.redis_is_real
})
const healthText = computed(() => {
  const h = health.value
  if (!h) return '正在检查……'
  if (!h.mysql) return '数据库连不上'
  if (!healthOk.value) return 'Redis 连不上，去重状态只在本进程内有效'
  return '数据库、缓存都正常'
})

let timer: number | undefined
async function loadHealth() {
  try {
    health.value = await settingsApi.health()
  } catch {
    health.value = { mysql: false }
  }
}
onMounted(() => {
  loadHealth()
  timer = window.setInterval(loadHealth, 30_000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<style scoped>
.box-frame {
  height: 100vh;
  display: flex;
  gap: 0;
  /* 白色面板贴着窗口上、右、下三边，只有左边挨着深色图标栏 */
  padding: 0;
  background: var(--smc-page-bg);
}

/* ---------------- 左侧图标栏 ---------------- */
.box-rail {
  width: 76px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 18px 0 20px;          /* 外框不再留边，图标栏自己留上下空 */
}
.rail-logo {
  width: 48px;
  height: 48px;
  border-radius: 16px;
  background: var(--box-cream);
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 26px;
}
/* LOGO 不变形：限定高度、宽度自适应，太宽就按宽度缩 */
.rail-logo img { max-width: 40px; max-height: 30px; width: auto; height: auto; display: block; }
.rail-nav { display: flex; flex-direction: column; gap: 10px; flex: 1; }
.rail-foot { display: flex; flex-direction: column; gap: 10px; }
.rail-btn {
  width: 44px;
  height: 44px;
  border-radius: 50%;
  border: none;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  color: rgba(255, 255, 255, 0.55);
  font-size: 19px;
  cursor: pointer;
  transition: background-color 0.15s ease, color 0.15s ease;
}
.rail-btn:hover { background: rgba(255, 255, 255, 0.08); color: #fff; }
.rail-btn.is-active { background: #fff; color: var(--box-ink); }
.rail-btn:focus-visible { outline: 2px solid var(--box-cream); outline-offset: 2px; }

/* ---------------- 右侧白色面板 ---------------- */
.box-panel {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: #fff;
  border-radius: 30px 0 0 30px;
  overflow: hidden;
}
.box-top {
  height: 68px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 16px;
  padding: 0 32px;
}
.box-top-right { display: flex; align-items: center; gap: 14px; }
.box-status {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  color: var(--smc-text-secondary);
  padding: 8px 14px;
  border-radius: 999px;
  background: #f4f5f8;
}
.status-item { display: inline-flex; align-items: center; gap: 6px; }
.status-sep { width: 1px; height: 12px; background: var(--smc-border-strong); }
.status-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.status-dot.ok { background: #22c55e; }
.status-dot.bad { background: #ef4444; }
.box-user { display: inline-flex; align-items: center; gap: 10px; }
.box-avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--box-lavender);
  color: var(--box-ink);
  font-weight: 600;
}
.box-user-text { display: flex; flex-direction: column; line-height: 1.25; }
.box-user-name { font-size: 13px; font-weight: 600; color: var(--smc-text); }
.box-user-role { font-size: 12px; color: var(--smc-text-secondary); }

.box-main {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 4px 32px 32px;
  scrollbar-width: none;
}
.box-main::-webkit-scrollbar { display: none; }

@media (max-width: 760px) {
  .box-panel { border-radius: 22px 0 0 22px; }
  .box-rail { width: 60px; }
  .rail-btn { width: 40px; height: 40px; }
  .box-top { padding: 0 16px; }
  .box-status, .box-user-text { display: none; }
  .box-main { padding: 4px 16px 24px; }
}
</style>
