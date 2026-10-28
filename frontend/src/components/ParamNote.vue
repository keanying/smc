<!--
  景区社媒采集系统 · Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!--
    参数下方的一行说明：一句话 + 影响哪几张表；细节（几条要点）收进 ⓘ，一条一行，不挤成一段。
    全局注册：<ParamNote text="…" :tables="['core']" :tips="['…', '…']" />
  -->
  <div class="param-note">
    <span class="pn-text">{{ text }}</span>
    <InfoTip v-if="tips?.length" :width="340">
      <ul class="pn-tips"><li v-for="t in tips" :key="t">{{ t }}</li></ul>
    </InfoTip>
    <span class="pn-tables">
      <span class="pn-label">影响</span>
      <span v-if="tables === 'all'" class="pn-chip all">全部 6 张表</span>
      <span v-for="k in tableList" v-else :key="k" class="pn-chip">{{ TABLE_LABELS[k] || k }}</span>
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ text: string; tables: string[] | 'all'; tips?: string[] }>()

const TABLE_LABELS: Record<string, string> = {
  core: '核心指标', platform: '平台分布', dimension: '维度得分',
  content: '关键词云', drill: '下钻明细', macro: '大盘 KPI',
}
const tableList = computed(() => (props.tables === 'all' ? [] : props.tables))
</script>

<style scoped>
.param-note {
  flex: 0 0 100%;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 6px;
  margin-top: 6px;
  font-size: 12px;
  line-height: 18px;
  color: var(--smc-text-secondary);
}
.pn-text { margin-right: 4px; }
.pn-tables { display: inline-flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.pn-label { color: var(--el-text-color-placeholder); }
.pn-chip {
  padding: 0 6px;
  border-radius: 4px;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.pn-chip.all { color: var(--el-text-color-regular); background: var(--el-fill-color-light); }
.pn-tips { margin: 0; padding-left: 16px; }
.pn-tips li + li { margin-top: 4px; }
</style>
