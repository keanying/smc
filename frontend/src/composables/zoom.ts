// CloudSense（云听）· Copyright (c) 2026 keanying
// SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
// 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
// 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
// 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

/**
 * 页面显示比例：按屏幕宽度自动缩放，效果等同浏览器的缩放（Ctrl + 滚轮）。
 *
 * - 自动（默认）：1920 宽的屏幕约 80%，越宽越大、到 2400 宽是 100%；窄屏最低 80%
 * - 也可以手动固定成 100% / 90% / 80% / 70%，记在本机浏览器里
 *
 * 实现：给 #app 设 CSS zoom（值放在 --app-zoom 变量里）。
 * ⚠️ 不能直接缩整个 html：Element Plus 的下拉框、提示框挂在 body 下、按坐标定位，
 *    整页缩放后坐标会被再缩一遍，弹层整体偏到左上角（实测 1440 宽时下拉框盖住了输入框）。
 *    所以只缩 #app，弹层在 style.css 里单独缩它里面的内容。
 */
import { computed, ref } from 'vue'

export type ZoomMode = 'auto' | '1' | '0.9' | '0.8' | '0.7'

export const ZOOM_OPTIONS: Array<{ value: ZoomMode; label: string }> = [
  { value: 'auto', label: '自动（按屏幕宽度）' },
  { value: '1', label: '100%' },
  { value: '0.9', label: '90%' },
  { value: '0.8', label: '80%' },
  { value: '0.7', label: '70%' },
]

const STORAGE_KEY = 'smc.ui.zoom'
/** 自动模式下内容区「看起来舒服」的宽度：屏幕宽度 / 这个值 = 缩放比例 */
const AUTO_BASE_WIDTH = 2400
const AUTO_MIN = 0.8
const AUTO_MAX = 1

function readMode(): ZoomMode {
  const saved = (typeof localStorage !== 'undefined' && localStorage.getItem(STORAGE_KEY)) || 'auto'
  return (ZOOM_OPTIONS.some((o) => o.value === saved) ? saved : 'auto') as ZoomMode
}

export const zoomMode = ref<ZoomMode>(readMode())
/** 当前生效的缩放比例 */
export const zoom = ref(1)
/** 浏览器窗口宽度（不含缩放） */
export const viewportWidth = ref(typeof window !== 'undefined' ? window.innerWidth : 1920)
/** 页面布局实际能用的宽度：缩放后内容能摆开的宽度，按它决定侧边栏要不要收起 */
export const layoutWidth = computed(() => viewportWidth.value / zoom.value)

/** 自动模式的比例：按 0.05 取整，窗口拖动时不至于一像素一变 */
export function autoZoom(width: number): number {
  const raw = Math.min(AUTO_MAX, Math.max(AUTO_MIN, width / AUTO_BASE_WIDTH))
  return Math.round(raw * 20) / 20
}

function apply() {
  viewportWidth.value = window.innerWidth
  const z = zoomMode.value === 'auto' ? autoZoom(window.innerWidth) : Number(zoomMode.value)
  zoom.value = z
  document.documentElement.style.setProperty('--app-zoom', String(z))
}

export function setZoomMode(mode: ZoomMode) {
  zoomMode.value = mode
  try {
    localStorage.setItem(STORAGE_KEY, mode)
  } catch { /* 隐私模式写不进去就只管这次 */ }
  apply()
}

let started = false
/** 应用启动时调一次（main.ts） */
export function initZoom() {
  if (started || typeof window === 'undefined') return
  started = true
  apply()
  let pending: number | undefined
  window.addEventListener('resize', () => {
    window.clearTimeout(pending)
    pending = window.setTimeout(apply, 120)
  })
}
