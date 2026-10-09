<template>
  <el-card shadow="never" class="live-card">
    <template #header>
      <div class="live-header">
        <div class="live-title">
          <el-icon><Monitor /></el-icon>
          <span>实时画面</span>
          <el-tag v-if="active" size="small" :type="hasPicture ? 'success' : 'warning'">
            {{ statusLabel }}
          </el-tag>
          <el-tag v-else size="small" type="info">没有正在运行的浏览器</el-tag>
        </div>
        <div class="live-actions">
          <el-select
            v-if="views.length > 1" v-model="currentId" size="small" style="width: 220px"
          >
            <el-option
              v-for="v in views" :key="v.view_id" :value="v.view_id"
              :label="`${channelLabel(v.channel)} · ${v.scenic_name}`"
            />
          </el-select>
          <el-button size="small" :icon="Refresh" @click="reload">刷新</el-button>
          <el-button v-if="active" size="small" @click="toggleWatch">
            {{ watching ? '停止观看' : '开始观看' }}
          </el-button>
          <el-button
            v-if="active && watching" size="small"
            :type="takeover ? 'danger' : 'warning'"
            :disabled="!connected"
            @click="toggleTakeover"
          >
            {{ takeover ? '退出接管' : '接管操作' }}
          </el-button>
        </div>
      </div>
    </template>

    <div v-if="!active" class="live-empty">
      <el-icon :size="26"><VideoCamera /></el-icon>
      <div class="live-empty-title">这条任务现在没有打开着的浏览器</div>
      <div class="muted live-empty-tip">
        实时画面只在<b>拟人模式</b>（以及快手接口模式）采集期间可用——
        只有这些情况下浏览器才是常驻的。接口模式下浏览器只在刷 Cookie 时开一下就关，
        没有画面可看。任务跑起来之后这里会自动出现。
      </div>
    </div>

    <template v-else>
      <div class="live-meta">
        <span class="live-step">{{ info?.step || '正在采集…' }}</span>
        <span class="muted live-url" :title="info?.url">{{ info?.url }}</span>
      </div>
      <div
        ref="stageRef" class="live-stage" :class="{ 'is-takeover': takeover }"
        :tabindex="takeover ? 0 : -1"
        @keydown="onKeyDown"
      >
        <img
          v-if="frameSrc" :src="frameSrc" class="live-screen"
          :class="{ 'is-live': takeover }"
          alt="采集页面实时画面" @error="onFrameError"
          @mousedown="onMouseDown" @mousemove="onMouseMove"
          @mouseup="onMouseUp" @mouseleave="onMouseLeave"
          @wheel.prevent="onWheel" @contextmenu.prevent
          @dragstart.prevent
        />
        <div v-else class="live-placeholder">
          <el-icon class="is-loading" :size="26"><Loading /></el-icon>
          <div style="margin-top: 10px">正在连接采集浏览器…</div>
        </div>
      </div>
      <div v-if="takeover" class="live-takeover-bar">
        <el-icon><Warning /></el-icon>
        <span>
          <b>已接管</b>：你的鼠标现在直接作用在采集页面上。
          拖动验证请<b>按住滑块慢慢拖过去</b>——轨迹会原样传过去，别用瞬移。
          处理完<b>务必点「退出接管」</b>，采集器还在这个页面上跑流程。
        </span>
        <span v-if="info?.injected" class="muted">本轮已注入 {{ info.injected }} 个事件</span>
      </div>
      <div v-if="takeover" class="live-keys">
        <el-input
          v-model="typeText" size="small" style="width: 260px"
          placeholder="输入文字后回车发送（中文/粘贴走这里）"
          @keyup.enter="sendText"
        />
        <el-button size="small" @click="sendText">发送</el-button>
        <el-divider direction="vertical" />
        <el-button size="small" @click="sendKey('Enter')">Enter</el-button>
        <el-button size="small" @click="sendKey('Backspace')">退格</el-button>
        <el-button size="small" @click="sendKey('Tab')">Tab</el-button>
        <el-button size="small" @click="sendKey('Escape')">Esc</el-button>
        <span class="muted" style="font-size: 12px">
          点一下画面再敲键盘也可以直接输入（中文输入法只能用左边的输入框）
        </span>
      </div>
      <div class="muted live-foot">
        <template v-if="!takeover">
          只读预览：这里的点击<b>不会</b>传给采集浏览器。
          采集器正按自己的节奏走流程，插一脚会让它的下一步落空。
          <b>只有出现拖动验证/验证码这类必须人工处理的情况</b>才点「接管操作」，
          处理完立刻退出。
        </template>
        <template v-if="info">
          当前：{{ channelLabel(info.channel) }} · 账号 {{ info.account_name }} ·
          景区 {{ info.scenic_name }} · 已跑 {{ Math.round(info.running_seconds / 60) }} 分钟。
        </template>
      </div>
    </template>
  </el-card>
</template>

<script setup lang="ts">
/**
 * 采集实时画面（只读）。
 *
 * 无头模式下采集过程只能从日志推断，日志能说"点了筛选"，说不了"点完之后
 * 页面上是什么"——验证码、风控拦截、空列表，这些只有看一眼才知道。
 *
 * 两条取画面的路：
 *   WebSocket 推流   首选。CDP 只在画面变化时推帧，省带宽也更跟手
 *   轮询单帧接口     兜底。有些反向代理不转发 WebSocket，那种环境下推流
 *                    永远连不上，如果不兜底，用户看到的就是永远的"连接中…"
 * 判据是"连上之后 6 秒还没收到第一帧"，而不是"连不上"——代理常常让
 * 握手成功但不转发数据帧，只看连接状态是分辨不出来的。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Loading, Monitor, Refresh, VideoCamera, Warning } from '@element-plus/icons-vue'
import { liveApi } from '../api'
import { LiveViewSocket, type LiveViewInfo, type LiveViewMessage } from '../api/ws'
import { channelLabel } from '../constants'

const props = defineProps<{ taskId: string; running?: boolean }>()

const views = ref<LiveViewInfo[]>([])
const currentId = ref('')
const info = ref<LiveViewInfo | null>(null)
const frame = ref('')
const connected = ref(false)
const watching = ref(true)

/**
 * 人工接管。默认关闭 —— 只有出现拖动验证这类必须人工处理的情况才开。
 *
 * ⚠️ 以服务端返回的 info.takeover 为准显示，本地这个 ref 只是"我请求过什么"。
 *    两边分开是有原因的：连接断了服务端会自动退出接管（见 api/live.py），
 *    如果只看本地状态，按钮还显示"已接管"，用户以为在操作、其实每一下都被丢弃。
 */
const wantTakeover = ref(false)
const takeover = computed(() => !!info.value?.takeover)
/** 画面上按住鼠标没有？拖滑块时只有按住期间才需要连续发轨迹 */
const dragging = ref(false)
/** 上一次发 mousemove 的时间，用来限流 */
let lastMoveAt = 0
/** 帧的像素宽高，坐标换算要用（服务端再按真实视口换一次） */
const frameSize = ref({ width: 1280, height: 800 })
/** 舞台元素。接管时要给它焦点，否则 keydown 根本不会触发 */
const stageRef = ref<HTMLElement | null>(null)
/** 粘贴/中文输入框的内容 */
const typeText = ref('')

/** 这些键要整键发过去，不能当字符打 */
const SPECIAL_KEYS = new Set([
  'Enter', 'Backspace', 'Tab', 'Escape', 'Delete', 'Home', 'End',
  'PageUp', 'PageDown', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
])
const polling = ref(false)

let socket: LiveViewSocket | null = null
let discoverTimer: number | undefined
let pollTimer: number | undefined
let firstFrameTimer: number | undefined
/** 轮询兜底时直接用接口地址；推流时用 base64 内联，省一次请求 */
const pollSrc = ref('')

const active = computed(() => !!currentId.value)
const frameSrc = computed(() =>
  polling.value ? pollSrc.value : (frame.value ? `data:image/jpeg;base64,${frame.value}` : ''))
const hasPicture = computed(() => !!frameSrc.value)
/**
 * 状态标签看的是**有没有画面**，不是 WebSocket 通不通。
 * 反代不转发 WebSocket 的环境里，socket 会一直重连失败，
 * 但轮询兜底其实已经在正常出图了——这时候还显示"连接中…"是在误导人。
 */
const statusLabel = computed(() => {
  if (polling.value) return '轮询中'
  if (connected.value && frame.value) return '推流中'
  return '连接中…'
})

async function discover(): Promise<void> {
  try {
    const list = await liveApi.views(props.taskId)
    views.value = list
    if (!list.some((v) => v.view_id === currentId.value)) {
      currentId.value = list[0]?.view_id || ''
    }
  } catch {
    views.value = []
    currentId.value = ''
  }
}

function stopStream(): void {
  socket?.close()
  socket = null
  connected.value = false
  window.clearTimeout(firstFrameTimer)
  window.clearInterval(pollTimer)
  polling.value = false
  frame.value = ''
  pollSrc.value = ''
}

function startPolling(): void {
  if (polling.value || !currentId.value) return
  polling.value = true
  const tick = () => { pollSrc.value = liveApi.frameUrl(currentId.value) }
  tick()
  pollTimer = window.setInterval(tick, 1200)
}

function startStream(): void {
  stopStream()
  if (!currentId.value || !watching.value) return
  socket = new LiveViewSocket(
    currentId.value,
    (message: LiveViewMessage) => {
      if (message.type === 'frame') {
        window.clearTimeout(firstFrameTimer)
        if (polling.value) { window.clearInterval(pollTimer); polling.value = false }
        frame.value = message.data
        if (message.width && message.height) {
          frameSize.value = { width: message.width, height: message.height }
        }
      } else if (message.type === 'meta') {
        info.value = message.data
      } else if (message.type === 'status' && message.state === 'gone') {
        stopStream()
        currentId.value = ''
        void discover()
      }
    },
    (isConnected) => { connected.value = isConnected },
  )
  socket.open()
  // 握手成功但收不到帧的环境（不转发 WebSocket 数据的反代）在这里被兜住
  firstFrameTimer = window.setTimeout(() => { if (!frame.value) startPolling() }, 6000)
}

/**
 * 轮询兜底时图片取不到 = 这个采集会话没了（接口返回 404）。
 * 推流那条路有服务端发的 gone 状态帧，轮询这条路没有，只能靠这个信号——
 * 不然定时器会继续每 1.2 秒打一次 404，直到下一轮 discover 才停。
 */
function onFrameError(): void {
  if (!polling.value) return
  window.clearInterval(pollTimer)
  polling.value = false
  pollSrc.value = ''
  void discover()
}

function toggleWatch(): void {
  watching.value = !watching.value
  if (watching.value) startStream()
  else {
    // 停止观看时一并退出接管：连接都断了，接管留着只会让下一个连上来的人
    // 在不知情的情况下"一点就生效"
    if (wantTakeover.value) setTakeover(false)
    stopStream()
  }
}

// ---------------- 人工接管 ----------------
function setTakeover(on: boolean): void {
  wantTakeover.value = on
  socket?.send({ type: 'takeover', on })
  if (!on) { dragging.value = false; return }
  // 焦点必须给到舞台，否则点完「接管」直接敲键盘一点反应都没有，
  // 而用户完全看不出是"没聚焦"——只会觉得键盘功能坏了
  void nextTick(() => stageRef.value?.focus())
}

function toggleTakeover(): void {
  setTakeover(!takeover.value)
}

/**
 * 画面按 CSS 尺寸缩放显示，回传的是**帧的像素坐标**。
 * 服务端还会再按真实视口换算一次（见 live_view._handle_input）——
 * 两段换算各管一段，前端不需要知道采集浏览器的视口有多大。
 */
function toFrameCoords(event: MouseEvent) {
  const rect = (event.target as HTMLImageElement).getBoundingClientRect()
  if (!rect.width || !rect.height) return { x: 0, y: 0 }
  return {
    x: (event.clientX - rect.left) * (frameSize.value.width / rect.width),
    y: (event.clientY - rect.top) * (frameSize.value.height / rect.height),
  }
}

function onMouseDown(event: MouseEvent): void {
  if (!takeover.value) return
  event.preventDefault()
  dragging.value = true
  socket?.send({ type: 'mousedown', ...toFrameCoords(event) })
}

/**
 * ⚠️ 拖动验证成败就在这个函数上。
 *
 * 滑块类验证会看**轨迹**：只有起点和终点两个事件，等于瞬移，必然判定为机器。
 * 所以按住期间要连续发中间点。但也不能一个不漏地发——浏览器 mousemove
 * 一秒能有几百个，全塞进 WebSocket 会排队，轨迹反而变形。
 * 限到 ~60/s（16ms）：既保留了人手的抖动和加速度，又不至于堵住通道。
 */
function onMouseMove(event: MouseEvent): void {
  if (!takeover.value || !dragging.value) return
  const now = Date.now()
  if (now - lastMoveAt < 16) return
  lastMoveAt = now
  socket?.send({ type: 'mousemove', ...toFrameCoords(event) })
}

function onMouseUp(event: MouseEvent): void {
  if (!takeover.value) return
  event.preventDefault()
  dragging.value = false
  // 松手前把**最终位置**补一个 mousemove：上面的限流可能刚好把最后一个点吃掉，
  // 那样滑块会停在差几像素的地方，判定不过而且看不出为什么
  const at = toFrameCoords(event)
  socket?.send({ type: 'mousemove', ...at })
  socket?.send({ type: 'mouseup', ...at })
}

function onMouseLeave(event: MouseEvent): void {
  // 拖着拖着滑出画面：必须补一个 mouseup，否则采集页面会一直以为按键还按着，
  // 之后所有点击都变成拖拽
  if (!takeover.value || !dragging.value) return
  onMouseUp(event)
}

function onWheel(event: WheelEvent): void {
  if (!takeover.value) return
  socket?.send({ type: 'wheel', deltaX: event.deltaX, deltaY: event.deltaY })
}

/**
 * 键盘。接管期间敲的键直接打到采集页面上。
 *
 * ⚠️ 两个坑：
 * 1. **必须 preventDefault**。不拦的话退格会让浏览器后退、方向键和空格会滚动
 *    本地页面、Tab 会把焦点跳到别的按钮上——人在操作采集页面，本地页面却在动。
 * 2. **中文输入法打不出来**。IME 组字期间 keydown 的 key 是 'Process'，
 *    拿不到真正的字。所以下面还留了一个输入框走 type 事件——
 *    中文验证码、粘贴长串都得用它。这不是偷懒，是浏览器就取不到。
 */
function onKeyDown(event: KeyboardEvent): void {
  if (!takeover.value) return
  const key = event.key
  // IME 组字中：交给输入框那条路，这里什么都不做
  if (event.isComposing || key === 'Process' || key === 'Unidentified') return

  const combo: string[] = []
  if (event.ctrlKey) combo.push('Control')
  if (event.altKey) combo.push('Alt')
  if (event.metaKey) combo.push('Meta')
  // Shift 只在组合键里显式带上：单独按 Shift+a 时 event.key 已经是 'A' 了，
  // 再拼一个 Shift+A 会变成两次大写处理
  if (event.shiftKey && (combo.length || SPECIAL_KEYS.has(key))) combo.push('Shift')

  if (SPECIAL_KEYS.has(key) || combo.length) {
    event.preventDefault()
    socket?.send({ type: 'key', key: [...combo, key].join('+') })
    return
  }
  if (key.length === 1) {           // 普通可打印字符
    event.preventDefault()
    socket?.send({ type: 'type', text: key })
  }
}

function sendText(): void {
  if (!takeover.value || !typeText.value) return
  socket?.send({ type: 'type', text: typeText.value })
  typeText.value = ''
}

function sendKey(key: string): void {
  if (!takeover.value) return
  socket?.send({ type: 'key', key })
}

async function reload(): Promise<void> {
  await discover()
  if (watching.value) startStream()
}

watch(currentId, (id) => {
  info.value = views.value.find((v) => v.view_id === id) || null
  if (id && watching.value) startStream()
  else stopStream()
})

watch(() => props.taskId, () => { void reload() }, { immediate: true })

// 任务可能在页面开着的时候才跑起来，所以要一直找。
// 找到之后放慢：没在跑的时候 8 秒问一次，正在看的时候 30 秒对一次就够了。
discoverTimer = window.setInterval(() => {
  if (!currentId.value || !connected.value) void discover()
}, 8000)

onBeforeUnmount(() => {
  window.clearInterval(discoverTimer)
  stopStream()
})
</script>

<style scoped>
.live-card { margin-bottom: 16px; }
.live-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.live-title { display: flex; align-items: center; gap: 8px; font-weight: 600; }
.live-actions { display: flex; align-items: center; gap: 8px; }
.live-meta { display: flex; align-items: baseline; gap: 12px; margin-bottom: 8px; flex-wrap: wrap; }
.live-step { font-weight: 600; }
.live-url { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
.live-stage {
  background: #1f1f1f; border-radius: 6px; overflow: hidden;
  display: flex; align-items: center; justify-content: center; min-height: 240px;
}
.live-screen { width: 100%; display: block; }
.live-placeholder { color: #bbb; text-align: center; padding: 40px 0; }
.live-empty { text-align: center; padding: 28px 12px; }
.live-empty-title { margin-top: 8px; font-weight: 600; }
.live-empty-tip { font-size: 12px; margin-top: 6px; line-height: 1.7; }
.live-foot { font-size: 12px; margin-top: 8px; line-height: 1.7; }
.live-stage.is-takeover {
  outline: 2px solid #f56c6c;
  outline-offset: 2px;
  border-radius: 6px;
}
.live-screen.is-live { cursor: crosshair; }
.live-takeover-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  padding: 8px 12px;
  border-radius: 8px;
  background: #fef0f0;
  border: 1px solid #fbc4c4;
  color: #c45656;
  font-size: 12.5px;
  line-height: 1.6;
}
.live-stage:focus { outline: 2px solid #f56c6c; outline-offset: 2px; }
.live-keys {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 8px;
}
</style>