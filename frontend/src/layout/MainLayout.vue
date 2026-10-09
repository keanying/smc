<template>
  <el-container class="app-shell">
    <el-aside width="216px" class="aside-menu">
      <div class="logo">
        <div class="logo-mark">S</div>
        <div class="logo-text">
          <div class="logo-title">景区社媒采集</div>
          <div class="logo-sub">Scenic Media Collector</div>
        </div>
      </div>
      <el-menu
        router
        :default-active="route.path"
        class="side-menu"
        background-color="transparent"
        text-color="#9aa5bd"
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
        <el-menu-item index="/accounts">
          <el-icon><User /></el-icon><span>账号管理</span>
        </el-menu-item>
        <el-menu-item index="/settings">
          <el-icon><Setting /></el-icon><span>系统设置</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header height="56px" class="app-header">
        <div class="header-title">{{ route.meta.title || '' }}</div>
        <div class="header-right">
          <span v-if="health" class="header-meta">
            <span class="meta-item">
              <span class="health-dot sm" :class="redisOk ? 'ok' : 'bad'" />
              {{ redisText }}
            </span>
            <template v-if="health.scheduler">
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
        </div>
      </el-header>
      <el-main class="app-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  Collection, Setting,
} from '@element-plus/icons-vue'
import { settingsApi } from '../api'

const route = useRoute()
const health = ref<Record<string, any> | null>(null)
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
  timer = window.setInterval(loadHealth, 30_000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<style scoped>
.app-shell { height: 100vh; }

.aside-menu {
  background: linear-gradient(180deg, #1c2333 0%, #151a26 100%);
  display: flex;
  flex-direction: column;
  box-shadow: 4px 0 20px rgba(0, 0, 0, 0.15);
  z-index: 10;
}

.logo {
  height: 64px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}
.logo-mark {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: linear-gradient(135deg, #4f6ef7, #7c9bff);
  color: #fff;
  font-weight: 700;
  font-size: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  box-shadow: 0 4px 12px rgba(79, 110, 247, 0.35);
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}
.logo-mark:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 16px rgba(79, 110, 247, 0.45);
}
.logo-title {
  color: #fff;
  font-weight: 600;
  font-size: 14px;
  letter-spacing: 0.5px;
  line-height: 1.3;
}
.logo-sub {
  color: #6b7690;
  font-size: 10px;
  letter-spacing: 0.4px;
}

.side-menu {
  border-right: none;
  flex: 1;
  padding: 10px 8px;
}
.side-menu :deep(.el-menu-item) {
  height: 44px;
  line-height: 44px;
  margin: 4px 0;
  border-radius: 10px;
  font-size: 13.5px;
  transition: all 0.2s ease;
}
.side-menu :deep(.el-menu-item:hover) {
  background: rgba(255, 255, 255, 0.08);
  color: #d5dcee;
  transform: translateX(4px);
}
.side-menu :deep(.el-menu-item.is-active) {
  background: linear-gradient(90deg, rgba(79, 110, 247, 0.9), rgba(79, 110, 247, 0.55));
  color: #fff;
  font-weight: 500;
  box-shadow: 0 4px 12px rgba(79, 110, 247, 0.35);
}

.aside-footer { padding: 14px; text-align: center; }
.health-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 6px 16px;
  border-radius: 20px;
  font-size: 12px;
  background: rgba(255, 255, 255, 0.06);
  color: #9aa5bd;
  transition: all 0.2s ease;
}
.health-pill:hover {
  background: rgba(255, 255, 255, 0.1);
}
.health-pill.is-ok .health-dot { background: #34d399; box-shadow: 0 0 8px #34d399; }
.health-pill.is-bad { color: #fca5a5; }
.health-pill.is-bad .health-dot { background: #f87171; box-shadow: 0 0 8px #f87171; }
.health-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.health-dot.sm { width: 7px; height: 7px; }
.health-dot.ok { background: #34d399; }
.health-dot.bad { background: #f87171; }

/* 系统健康齿轮：绿色转=正常，红色转=有问题 */
.health-gear {
  font-size: 18px;
  cursor: pointer;
  animation: gear-spin 3.2s linear infinite;
  transform-origin: center;
}
.health-gear.is-ok { color: #16a34a; }
.health-gear.is-bad { color: #dc2626; animation-duration: 1.1s; }
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
  background: #fff;
  border-bottom: 1px solid #e8ecf3;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.04);
}
.header-title { font-size: 16px; font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 10px; }
.header-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  color: #7a8699;
}
.meta-item { display: inline-flex; align-items: center; gap: 5px; }
.meta-sep { width: 1px; height: 12px; background: #e3e8f0; }
.meta-item.is-warn { color: #d97706; font-weight: 600; }

.app-main {
  background-color: #f4f6fb;
  padding: 24px;
  overflow-y: auto;
}
</style>
