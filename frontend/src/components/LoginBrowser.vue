<template>
  <el-dialog
    :model-value="visible" :title="`登录 ${channelLabel(channel)} · ${accountName}`"
    width="1000px" top="4vh" :close-on-click-modal="false" :close-on-press-escape="false"
    @update:model-value="close"
  >
    <el-alert :type="statusType" :closable="false" style="margin-bottom: 12px">
      <div style="display: flex; align-items: center; gap: 10px">
        <el-icon v-if="loading" class="is-loading"><Loading /></el-icon>
        <span>{{ status.message || '正在连接…' }}</span>
      </div>
    </el-alert>

    <div class="browser-stage" :style="{ width: `${displayWidth}px` }">
      <img
        v-if="frame" ref="screen" :src="`data:image/jpeg;base64,${frame}`"
        class="browser-screen" :style="{ width: `${displayWidth}px` }"
        @click="onClick" @mousedown="onMouseDown" @mouseup="onMouseUp"
        @mousemove="onMouseMove" @wheel.prevent="onWheel" @contextmenu.prevent
      />
      <div v-else class="browser-placeholder">
        <el-icon class="is-loading" :size="30"><Loading /></el-icon>
        <div style="margin-top: 10px">正在启动服务端浏览器…</div>
        <div class="muted" style="font-size: 12px; margin-top: 4px">
          首次启动需要十几秒
        </div>
      </div>
    </div>

    <div class="keyboard-bar">
      <el-input
        v-model="typeText" placeholder="需要输入文字时，在这里打字后点「发送到页面」"
        style="flex: 1" clearable @keyup.enter="sendText"
      />
      <el-button @click="sendText">发送到页面</el-button>
      <el-button @click="sendKey('Enter')">回车</el-button>
      <el-button @click="sendKey('Backspace')">退格</el-button>
      <el-button :icon="RefreshRight" @click="reload">刷新页面</el-button>
    </div>

    <div class="muted" style="font-size: 12px; margin-top: 8px">
      画面是服务端浏览器的实时推流，鼠标点击和滚轮会同步回放到服务端。
      扫码登录直接用手机扫画面里的二维码即可。
      <b>登录成功后点「我已登录，保存」</b>——系统也会自动识别，
      但以你看到的画面为准；保存之后采集任务会静默复用，不再弹这个窗口。
      <template v-if="channel === 'weibo'">
        微博 PC 端的登录是弹窗，弹窗打开后画面会自动切过去。
      </template>
    </div>

    <template #footer>
      <el-button type="primary" :loading="saving" @click="saveNow">
        我已登录，保存
      </el-button>
      <el-button @click="close">{{ status.logged_in ? '完成' : '关闭窗口' }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Loading, RefreshRight } from '@element-plus/icons-vue'
import { accountApi } from '../api'
import { LoginBrowserSocket, type BrowserMessage } from '../api/ws'
import { channelLabel } from '../constants'

const props = defineProps<{ visible: boolean; channel: string; accountName: string }>()
const emit = defineEmits<{ (e: 'close', loggedIn: boolean): void }>()

const frame = ref('')
const frameWidth = ref(1280)
const frameHeight = ref(800)
const displayWidth = 940
const typeText = ref('')
const saving = ref(false)

const status = ref<{ state: string; message: string; logged_in?: boolean }>({
  state: 'connecting', message: '',
})

let socket: LoginBrowserSocket | null = null
let sessionId = ''

const loading = computed(() => !['logged_in', 'closed', 'error', 'timeout'].includes(status.value.state))

/**
 * 「我已登录，保存」。
 *
 * ⚠️ 为什么不能只靠自动识别：识别再准也有失手的时候（平台改接口、
 * 网络抖一下、弹窗流程和预期不一样），而"到底登没登上"这件事
 * 人看一眼画面就知道。没有这个按钮，一旦识别失手，
 * 用户明明扫码成功了也只能干瞪眼，下次打开还得重扫。
 */
async function saveNow() {
  if (!sessionId) return
  saving.value = true
  try {
    const result = await accountApi.saveLoginSession(sessionId)
    status.value = { ...status.value, logged_in: true, message: result.message }
    ElMessage.success(result.message || '登录态已保存')
  } catch (error: any) {
    ElMessage.error(error?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

const statusType = computed(() => {
  if (status.value.state === 'logged_in') return 'success'
  if (['error', 'timeout'].includes(status.value.state)) return 'error'
  return 'info'
})

/** 画面按 displayWidth 缩放显示，回传坐标要换算回真实像素 */
function toPageCoords(event: MouseEvent) {
  const target = event.target as HTMLImageElement
  const rect = target.getBoundingClientRect()
  const scale = frameWidth.value / rect.width
  return {
    x: (event.clientX - rect.left) * scale,
    y: (event.clientY - rect.top) * scale,
  }
}

function onClick(event: MouseEvent) {
  socket?.send({ type: 'click', ...toPageCoords(event) })
}
function onMouseDown(event: MouseEvent) {
  socket?.send({ type: 'mousedown', ...toPageCoords(event) })
}
function onMouseUp(event: MouseEvent) {
  socket?.send({ type: 'mouseup', ...toPageCoords(event) })
}
/** 鼠标移动：拖拽滑块验证需要连续轨迹，不能只有按下/松开两个点 */
function onMouseMove(event: MouseEvent) {
  socket?.send({ type: 'mousemove', ...toPageCoords(event) })
}
function onWheel(event: WheelEvent) {
  socket?.send({ type: 'wheel', deltaX: event.deltaX, deltaY: event.deltaY })
}
function sendText() {
  if (!typeText.value) return
  socket?.send({ type: 'type', text: typeText.value })
  typeText.value = ''
}
function sendKey(key: string) {
  socket?.send({ type: 'key', key })
}
function reload() {
  socket?.send({ type: 'reload' })
}

function handleMessage(message: BrowserMessage) {
  if (message.type === 'frame') {
    frame.value = message.data
    frameWidth.value = message.width || 1280
    frameHeight.value = message.height || 800
  } else if (message.type === 'status') {
    status.value = message
    if (message.state === 'logged_in') {
      ElMessage.success('登录成功，登录态已保存')
    }
  }
}

async function start() {
  frame.value = ''
  status.value = { state: 'connecting', message: '正在创建登录会话…' }
  try {
    const session = await accountApi.createLoginSession(props.channel, props.accountName)
    sessionId = session.session_id
    socket = new LoginBrowserSocket(sessionId, handleMessage, () => {
      status.value = { ...status.value, state: 'closed' }
    })
    socket.open()
  } catch {
    status.value = { state: 'error', message: '创建登录会话失败' }
  }
}

async function close() {
  const loggedIn = !!status.value.logged_in || status.value.state === 'logged_in'
  socket?.close()
  socket = null
  if (sessionId) {
    // 服务端也要收尾：保存 Cookie 并关掉浏览器
    await accountApi.closeLoginSession(sessionId).catch(() => undefined)
    sessionId = ''
  }
  frame.value = ''
  emit('close', loggedIn)
}

watch(() => props.visible, (value) => {
  if (value) start()
})
</script>

<style scoped>
.browser-stage {
  margin: 0 auto;
  background: #f5f7fa;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  overflow: hidden;
  min-height: 420px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.browser-screen {
  display: block;
  cursor: crosshair;
  user-select: none;
}
.browser-placeholder {
  text-align: center;
  color: #909399;
  padding: 60px 0;
}
.keyboard-bar {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
</style>
