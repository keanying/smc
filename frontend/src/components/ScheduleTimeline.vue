<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!--
    调度时间分布：今天 0~24 点一条横条，有任务启动的小时高亮，
    鼠标移上去看这个小时里哪些任务、几点启动。
    过去的时间是运行记录里实际启动的，没到的时间按调度设置推算。时间按服务器时间。
  -->
  <div class="sched">
    <span class="sched-label">
      今日调度
      <InfoTip content="今天 24 小时里任务的启动时刻。有任务的小时会高亮，鼠标移上去看是哪些任务、几点启动。已过去的是实际启动，没到的是按调度设置推算。" />
    </span>
    <div class="sched-line">
      <div class="strip">
        <el-tooltip
          v-for="h in hours" :key="h.hour" placement="top" :show-after="80"
          :disabled="!h.items.length" popper-class="info-tip-popper"
        >
          <template #content>
            <div class="tip">
              <div class="tip-title">{{ pad(h.hour) }}:00 – {{ pad(h.hour + 1) }}:00</div>
              <div v-for="(item, i) in h.items.slice(0, MAX_LINES)" :key="i" class="tip-line">
                {{ item.at }}　{{ item.name }}<span class="tip-kind">{{ item.kind }}</span>
              </div>
              <div v-if="h.items.length > MAX_LINES" class="tip-line">
                …还有 {{ h.items.length - MAX_LINES }} 次
              </div>
            </div>
          </template>
          <div class="cell" :class="{ active: h.items.length, current: h.hour === nowHour }" />
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

const MAX_LINES = 12
const data = ref<ScheduleTimeline | null>(null)

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/** 按小时分好：每个小时里谁、几点启动（按时间排） */
const hours = computed(() => {
  const buckets = Array.from({ length: 24 }, (_, hour) => ({
    hour, items: [] as Array<{ at: string; minute: number; name: string; kind: string }>,
  }))
  for (const t of data.value?.tasks || []) {
    for (const e of t.events) {
      const kind = e.kind === 'run' ? '已启动' : '计划'
      buckets[Math.min(23, Math.floor(e.minute / 60))].items.push({
        at: e.at.slice(0, 5), minute: e.minute, name: t.task_name, kind,
      })
    }
  }
  for (const b of buckets) b.items.sort((a, b2) => a.minute - b2.minute)
  return buckets
})

const nowHour = computed(() =>
  data.value?.now_minute == null ? -1 : Math.floor(data.value.now_minute / 60))

async function load() {
  data.value = await taskApi.scheduleTimeline('', true)
}

// 每分钟刷一次：过了的点从「计划」变成「已启动」
let timer: number | undefined
onMounted(() => {
  load()
  timer = window.setInterval(() => { if (!document.hidden) load() }, 60_000)
})
onUnmounted(() => window.clearInterval(timer))

/** 任务列表里执行 / 暂停定时 / 编辑 / 删除之后，父页面调这个让横条跟着变 */
defineExpose({ reload: load })
</script>

<style scoped>
/* 放在任务列表分页那一行的左边：不加卡片，高度和分页差不多 */
.sched { display: flex; align-items: flex-start; gap: 12px; }
.sched-label {
  flex: none; display: inline-flex; align-items: center; gap: 2px;
  font-size: 12px; color: var(--smc-text-secondary); line-height: 14px; white-space: nowrap;
}
.sched-line { flex: 1; min-width: 0; }

.strip { display: flex; gap: 2px; }
.cell {
  height: 14px; flex: 1; border-radius: 3px;
  background: var(--smc-border);
}
.cell.active { background: var(--smc-primary); cursor: pointer; }
.cell.active:hover { background: var(--smc-primary-dark); }
/* 当前这个小时：下面一条粉色细线 */
.cell.current { box-shadow: inset 0 -2px 0 var(--smc-accent); }

.axis { position: relative; height: 14px; margin-top: 3px; }
.axis span {
  position: absolute; transform: translateX(-50%);
  font-size: 10px; line-height: 14px; color: var(--smc-text-tertiary); font-variant-numeric: tabular-nums;
}
.axis span:first-child { transform: none; }
.axis span:last-child { transform: translateX(-100%); }

.tip { font-size: 12px; line-height: 1.7; max-width: 320px; }
.tip-title { font-weight: 600; }
.tip-line { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tip-kind { margin-left: 6px; opacity: 0.65; }
</style>
