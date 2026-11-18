<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!--
    今日调度：0~24 点一条横条，每小时一格。
    只要任务设置了调度（cron / 固定间隔 / 定时一次），它启动的那个小时就高亮，格子里写目标名称；
    暂停了定时的也画（颜色浅一点）。鼠标移到任意一格，看这个小时里谁、几点启动。
    时间按服务器时间，和任务列表里的「下次」一致。
  -->
  <div class="sched">
    <span class="sched-label">
      今日调度
      <InfoTip content="每个任务按调度设置在今天哪些时刻启动，有调度的小时会高亮并写上目标名称，浅色表示定时已暂停。鼠标移到格子上看是哪些任务、几点启动。" />
    </span>
    <div class="sched-line">
      <div class="strip">
        <el-tooltip
          v-for="h in hours" :key="h.hour" placement="top" :show-after="60"
          popper-class="info-tip-popper"
        >
          <template #content>
            <div class="tip">
              <div class="tip-title">
                {{ pad(h.hour) }}:00 – {{ pad(h.hour + 1) }}:00
                <span v-if="h.items.length" class="tip-count">{{ h.items.length }} 次</span>
              </div>
              <div v-if="!h.items.length" class="tip-line">这个小时没有调度</div>
              <div v-for="(item, i) in h.items.slice(0, MAX_LINES)" :key="i" class="tip-line">
                {{ item.approx ? '≈' : '' }}{{ item.at }}　{{ item.target }}
                <span v-if="item.taskName !== item.target" class="tip-sub">· {{ item.taskName }}</span>
                <span v-if="item.paused" class="tip-sub">（已暂停）</span>
              </div>
              <div v-if="h.items.length > MAX_LINES" class="tip-line tip-sub">
                …还有 {{ h.items.length - MAX_LINES }} 次
              </div>
            </div>
          </template>
          <div
            class="cell"
            :class="{ active: h.items.length, paused: h.items.length && h.allPaused }"
          >
            <template v-if="h.targets.length">
              <span class="cell-name">{{ h.targets[0] }}</span>
              <span v-if="h.targets.length > 1" class="cell-more">+{{ h.targets.length - 1 }}</span>
            </template>
          </div>
        </el-tooltip>
      </div>
      <div class="axis">
        <span v-for="h in 25" :key="h" :style="{ left: `${((h - 1) / 24) * 100}%` }">{{ h - 1 }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

import { taskApi, type ScheduleTimeline } from '../api'

const MAX_LINES = 15
const data = ref<ScheduleTimeline | null>(null)

interface HourItem {
  at: string
  minute: number
  target: string
  taskName: string
  paused: boolean
  approx: boolean
}

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/** 按小时分好：每个小时里谁、几点启动（按时间排），以及格子里要写的目标名称（去重） */
const hours = computed(() => {
  const buckets = Array.from({ length: 24 }, (_, hour) => ({ hour, items: [] as HourItem[] }))
  for (const t of data.value?.tasks || []) {
    for (const e of t.events) {
      buckets[Math.min(23, Math.floor(e.minute / 60))].items.push({
        at: e.at.slice(0, 5),
        minute: e.minute,
        target: t.scenic_name || t.task_name,
        taskName: t.task_name,
        paused: !t.schedule_enabled,
        approx: e.approx,
      })
    }
  }
  return buckets.map((b) => {
    const items = b.items.sort((x, y) => x.minute - y.minute)
    return {
      ...b,
      items,
      targets: [...new Set(items.map((i) => i.target))],
      allPaused: items.every((i) => i.paused),
    }
  })
})

async function load() {
  data.value = await taskApi.scheduleTimeline('', true)
}

// 每分钟刷一次：别处改了调度能跟上，过了 0 点换成新的一天
let timer: number | undefined
onMounted(() => {
  load()
  timer = window.setInterval(() => { if (!document.hidden) load() }, 60_000)
})
onUnmounted(() => window.clearInterval(timer))

/** 任务列表里暂停定时 / 编辑 / 删除之后，父页面调这个让横条跟着变 */
defineExpose({ reload: load })
</script>

<style scoped>
.sched { display: flex; align-items: flex-start; gap: 12px; }
.sched-label {
  flex: none; display: inline-flex; align-items: center; gap: 2px;
  font-size: 12px; color: var(--smc-text-secondary); line-height: 24px; white-space: nowrap;
}
.sched-line { flex: 1; min-width: 0; }

.strip { display: flex; gap: 2px; }
.cell {
  height: 24px; flex: 1 1 0; min-width: 0; border-radius: 4px;
  background: var(--smc-border);
  display: flex; align-items: center; gap: 2px; padding: 0 4px; box-sizing: border-box;
  font-size: 11px; line-height: 1; color: #fff; cursor: default;
}
.cell.active { background: var(--smc-primary); }
.cell.active:hover { background: var(--smc-primary-dark); }
/* 这个小时里的任务定时全都暂停了：浅色 */
.cell.paused { background: #d6d1f5; color: var(--smc-primary-dark); }
.cell.paused:hover { background: #c4bdf0; }
.cell-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cell-more { flex: none; opacity: 0.8; font-variant-numeric: tabular-nums; }

.axis { position: relative; height: 14px; margin-top: 3px; }
.axis span {
  position: absolute; transform: translateX(-50%);
  font-size: 10px; line-height: 14px; color: var(--smc-text-tertiary); font-variant-numeric: tabular-nums;
}
.axis span:first-child { transform: none; }
.axis span:last-child { transform: translateX(-100%); }

.tip { font-size: 12px; line-height: 1.7; max-width: 360px; }
.tip-title { font-weight: 600; }
.tip-count { margin-left: 6px; font-weight: 400; opacity: 0.7; }
.tip-line { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tip-sub { opacity: 0.65; }
</style>
