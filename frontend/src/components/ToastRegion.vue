<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!--
    一个位置的一摞通知（照 HeroUI v3 Toast 的叠放算法）：
    - 收起：第 i 张往后错开 i×12px、缩小 i×5%，高度统一成最前那张的高度，内容淡掉
    - 展开（鼠标移上来）：每张按真实高度依次排开，间距 12px；整摞倒计时暂停
    - 进场从边外滑进来；退场最前那张滑回去，其余的缩一点淡出
  -->
  <section
    class="toast-region" :class="`toast-region--${slug}`" aria-label="通知" :data-expanded="expanded"
    @pointerenter="onEnter" @pointerleave="onLeave" @focusin="onEnter" @focusout="onFocusOut"
    @keydown.esc="onLeave"
  >
    <div
      v-for="t in list" :key="t.key"
      class="toast" :class="[`toast--${slug}`, `toast--${t.variant}`]"
      :style="styleOf(t)"
      :role="t.variant === 'danger' ? 'alert' : 'status'"
      :aria-live="t.variant === 'danger' ? 'assertive' : 'polite'"
      :data-frontmost="indexOf(t) === 0"
      :data-expanded="expanded"
      :data-entering="entering.has(t.key)"
      :data-exiting="t.exiting"
      :data-hidden="!t.exiting && indexOf(t) >= MAX_VISIBLE"
    >
      <div :ref="(el) => observe(t.key, el as HTMLElement | null)" class="toast__body">
        <div class="toast__indicator" :data-swapped="t.revision > 0" :key="`${t.key}-${t.revision}`">
          <span v-if="t.isLoading" class="toast__spinner" aria-label="处理中" />
          <el-icon v-else><component :is="ICONS[t.variant]" /></el-icon>
        </div>
        <div class="toast__content">
          <div class="toast__title">{{ t.title }}</div>
          <div v-if="t.description" class="toast__description">{{ t.description }}</div>
        </div>
        <button v-if="t.action" type="button" class="toast__action" @click="runAction(t)">{{ t.action.label }}</button>
      </div>
      <button type="button" class="toast__close-button" aria-label="关闭" @click="close(t.key)">
        <el-icon><Close /></el-icon>
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import {
  AlertCircleOutline, CheckmarkCircleOutline, Close, InformationCircleOutline, WarningOutline,
} from '@vicons/ionicons5'
import {
  close, pausePlacement, resumePlacement, toastState, type ToastItem, type ToastPlacement,
} from '../composables/toast'

const props = defineProps<{ placement: ToastPlacement }>()

/** 和 HeroUI 的默认值一致 */
const GAP = 12
const SCALE_FACTOR = 0.05
const MAX_VISIBLE = 3

const ICONS = {
  default: InformationCircleOutline,
  accent: InformationCircleOutline,
  success: CheckmarkCircleOutline,
  warning: WarningOutline,
  danger: AlertCircleOutline,
}

const slug = computed(() => props.placement.replace(' ', '-'))

/** 这个位置的通知，最新的在前 */
const list = computed(() => toastState.items.filter((t) => t.placement === props.placement).reverse())
/** 还在的（不含正在退场的）：叠放序号只在它们之间排，关掉一张后面的马上补上来 */
const live = computed(() => list.value.filter((t) => !t.exiting))

// 退场中的那张保持它关掉那一刻的序号，原地播动画
const lastIndex = new Map<string, number>()
function indexOf(t: ToastItem): number {
  if (t.exiting) return lastIndex.get(t.key) ?? 0
  const i = live.value.indexOf(t)
  lastIndex.set(t.key, i)
  return i
}

// ------------------------------------------------------------------ 高度测量
const heights = reactive(new Map<string, number>())
const observers = new Map<string, ResizeObserver>()
function observe(key: string, el: HTMLElement | null) {
  if (!el) {
    observers.get(key)?.disconnect()
    observers.delete(key)
    heights.delete(key)
    return
  }
  if (observers.has(key)) return
  const ro = new ResizeObserver(() => heights.set(key, el.offsetHeight))
  ro.observe(el)
  observers.set(key, ro)
  heights.set(key, el.offsetHeight)
}

function styleOf(t: ToastItem): Record<string, string | number> {
  const index = indexOf(t)
  const front = live.value[0]
  const frontHeight = (front && heights.get(front.key)) || heights.get(t.key) || 0
  let before = 0
  for (const other of live.value.slice(0, Math.max(0, index))) before += heights.get(other.key) ?? frontHeight
  const own = heights.get(t.key)
  return {
    '--offset-collapsed': `${index * GAP}px`,
    '--offset-expanded': `${before + index * GAP}px`,
    '--scale-collapsed': `${1 - index * SCALE_FACTOR}`,
    '--front-height': `${frontHeight}px`,
    ...(own ? { '--toast-height': `${own}px` } : {}),
    '--gap': `${GAP}px`,
    zIndex: list.value.length - index,
  }
}

// ------------------------------------------------------------------ 进场：先放在边外，下一帧再滑进来
const entering = reactive(new Set<string>())
watch(() => list.value.map((t) => t.key), (keys, old) => {
  const before = new Set(old || [])
  keys.filter((k) => !before.has(k)).forEach((k) => {
    entering.add(k)
    requestAnimationFrame(() => requestAnimationFrame(() => entering.delete(k)))
  })
  // 已经移除的清掉序号缓存
  for (const k of lastIndex.keys()) if (!keys.includes(k)) lastIndex.delete(k)
}, { immediate: true })

// ------------------------------------------------------------------ 展开 / 收起，暂停倒计时
const expanded = ref(false)
function onEnter() {
  expanded.value = true
  pausePlacement(props.placement)
}
function onLeave() {
  expanded.value = false
  resumePlacement(props.placement)
}
function onFocusOut(e: FocusEvent) {
  const region = e.currentTarget as HTMLElement
  if (!region.contains(e.relatedTarget as Node | null)) onLeave()
}
// 最后一张关掉、这一摞被卸载时鼠标可能还停在上面：别让这个位置一直处于暂停
onBeforeUnmount(() => {
  resumePlacement(props.placement)
  observers.forEach((ro) => ro.disconnect())
})

function runAction(t: ToastItem) {
  t.action?.onClick()
  if (t.action?.closeOnClick !== false) close(t.key)
}
</script>

<style scoped>
/* ---------- 区域：固定在屏幕一角 / 正中，本身不挡鼠标 ---------- */
.toast-region {
  --toast-width: 380px;
  --toast-ease: cubic-bezier(0.32, 0.72, 0, 1);
  position: fixed;
  z-index: 9000;
  width: min(var(--toast-width), calc(100vw / var(--app-zoom, 1) - 32px));
  pointer-events: none;
  outline: none;
}
.toast-region--bottom { bottom: 16px; left: 50%; transform: translateX(-50%); }
.toast-region--bottom-start { bottom: 16px; left: 16px; }
.toast-region--bottom-end { bottom: 16px; right: 16px; }
.toast-region--top { top: 16px; left: 50%; transform: translateX(-50%); }
.toast-region--top-start { top: 16px; left: 16px; }
.toast-region--top-end { top: 16px; right: 16px; }

/* ---------- 单张通知 ---------- */
.toast {
  position: absolute;
  left: 0;
  right: 0;
  border-radius: 20px;
  background: #fff;
  box-shadow: 0 10px 30px rgba(40, 30, 110, 0.13), 0 2px 6px rgba(40, 30, 110, 0.06), 0 0 0 1px rgba(40, 30, 110, 0.04);
  pointer-events: auto;

  --toast-y: var(--offset-collapsed, 0px);
  --toast-enter: 0%;
  --toast-scale: var(--scale-collapsed, 1);
  --toast-translate-duration: 350ms;
  --toast-opacity-duration: 150ms;
  transform: translateY(calc(var(--dir, 1) * (var(--toast-y) + var(--toast-enter)))) scale(var(--toast-scale));
  height: var(--toast-height, auto);
  transition:
    transform var(--toast-translate-duration) var(--toast-ease),
    height 350ms var(--toast-ease),
    opacity var(--toast-opacity-duration) var(--toast-ease);
}
/* 叠放方向：在下面的往上叠，在上面的往下叠 */
.toast--bottom, .toast--bottom-start, .toast--bottom-end { --dir: -1; bottom: 0; transform-origin: bottom center; }
.toast--top, .toast--top-start, .toast--top-end { --dir: 1; top: 0; transform-origin: top center; }

.toast[data-entering="true"] { --toast-enter: -100%; }

/* 收起时后面那几张：高度跟最前那张一样，内容淡掉，只露个边 */
.toast > .toast__body { transition: opacity 200ms var(--toast-ease); }
.toast:not([data-frontmost="true"]):not([data-expanded="true"]) {
  height: var(--front-height, 0px);
  overflow: hidden;
}
.toast:not([data-frontmost="true"]):not([data-expanded="true"]) > .toast__body { opacity: 0; }
.toast:not([data-frontmost="true"]):not([data-expanded="true"]) .toast__close-button { opacity: 0; pointer-events: none; }

/* 展开 */
.toast[data-expanded="true"] {
  --toast-y: var(--offset-expanded, 0px);
  --toast-scale: 1;
}
/* 展开后两张之间的缝也算在悬停范围里，鼠标从一张挪到下一张不会收起来 */
.toast[data-expanded="true"]::after {
  content: '';
  position: absolute;
  z-index: -1;
  left: 0;
  right: 0;
}
.toast--bottom[data-expanded="true"]::after, .toast--bottom-start[data-expanded="true"]::after,
.toast--bottom-end[data-expanded="true"]::after { top: calc(-1 * var(--gap, 12px) - 1px); bottom: 0; }
.toast--top[data-expanded="true"]::after, .toast--top-start[data-expanded="true"]::after,
.toast--top-end[data-expanded="true"]::after { top: 0; bottom: calc(-1 * var(--gap, 12px) - 1px); }

/* 退场 */
.toast[data-exiting="true"] { opacity: 0; pointer-events: none; }
/* 最前那张：滑回边外；后面的：缩一点淡掉 */
.toast[data-exiting="true"]:not([data-frontmost="true"]) { --toast-translate-duration: 200ms; }
.toast[data-exiting="true"]:not([data-frontmost="true"]):not([data-expanded="true"]) { --toast-scale: calc(var(--scale-collapsed, 1) * 0.96); }
.toast[data-exiting="true"]:not([data-frontmost="true"])[data-expanded="true"] { --toast-scale: 0.96; }
.toast[data-exiting="true"][data-frontmost="true"] {
  --toast-translate-duration: 250ms;
  --toast-enter: -100%;
  --toast-scale: 1;
}

/* 超过 3 张的淡出（倒计时照走） */
.toast[data-hidden="true"] { opacity: 0; pointer-events: none; }

/* ---------- 内容：图标 | 标题+说明 | 操作按钮 ---------- */
.toast__body {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: start;
  column-gap: 8px;
  padding: 12px 16px;
}
.toast__indicator {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 2px;
  color: var(--smc-text);
  font-size: 18px;
}
.toast__indicator[data-swapped="true"] > * { animation: toast-icon-in 200ms var(--toast-ease); }
@keyframes toast-icon-in { from { opacity: 0; transform: scale(0.92); } }
.toast__content {
  grid-column: 2;
  display: flex;
  flex-direction: column;
  align-self: center;
  min-width: 0;
}
.toast__title {
  font-size: 14px;
  line-height: 22px;
  font-weight: 500;
  color: var(--smc-text);
  word-break: break-word;
}
.toast__description {
  font-size: 13px;
  line-height: 20px;
  color: var(--smc-text-secondary);
  word-break: break-word;
}
.toast__action {
  grid-column: 3;
  align-self: center;
  margin-left: 4px;
  padding: 4px 12px;
  border: none;
  border-radius: 999px;
  background: var(--smc-primary-light);
  color: var(--smc-primary);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
}
.toast__action:hover { background: var(--el-color-primary-light-8); }

/* 不同类型：标题和图标换颜色（底色保持白，和 HeroUI 一样克制） */
.toast--accent .toast__title, .toast--accent .toast__indicator { color: var(--smc-primary); }
.toast--success .toast__title, .toast--success .toast__indicator { color: #15803d; }
.toast--warning .toast__title, .toast--warning .toast__indicator { color: #b45309; }
.toast--danger .toast__title, .toast--danger .toast__indicator { color: #c2281f; }

/* 处理中：转圈 */
.toast__spinner {
  width: 16px;
  height: 16px;
  margin: 1px;
  border: 2px solid var(--el-border-color);
  border-top-color: var(--smc-primary);
  border-radius: 50%;
  animation: toast-spin 0.7s linear infinite;
}
@keyframes toast-spin { to { transform: rotate(360deg); } }

/* ---------- 关闭按钮：骑在右上角，鼠标移上来才出现 ---------- */
.toast__close-button {
  position: absolute;
  top: -4px;
  right: -4px;
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 50%;
  background: #fff;
  color: var(--smc-text-secondary);
  font-size: 11px;
  cursor: pointer;
  opacity: 0;
  pointer-events: none;
  transition: opacity 150ms ease, color 150ms ease;
}
.toast__close-button:hover { color: var(--smc-text); background: var(--el-fill-color-light); }
.toast[data-frontmost="true"]:hover .toast__close-button,
.toast[data-expanded="true"]:hover .toast__close-button,
.toast__close-button:focus-visible { opacity: 1; pointer-events: auto; }
.toast[data-exiting="true"] .toast__close-button { opacity: 0; pointer-events: none; }
.toast__close-button:focus-visible, .toast__action:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 1px; }

@media (prefers-reduced-motion: reduce) {
  .toast, .toast > .toast__body, .toast__close-button { transition: none; }
  .toast__indicator[data-swapped="true"] > * { animation: none; }
}
</style>
