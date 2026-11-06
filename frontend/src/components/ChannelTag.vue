<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!-- 平台标签：图标 + 名称，品牌色浅底、品牌色字，固定最小宽度——一列里上下对得齐。
       全站统一用它（以前各页面各写各的：实心胶囊、el-tag，宽度跟着字数走，一列参差不齐） -->
  <span class="channel-tag" :class="[`is-${size}`, { 'is-block': block }]" :style="style">
    <img v-if="icon" :src="icon" alt="" class="channel-icon" />
    <i v-else class="channel-dot" :style="{ background: color }" />
    <span class="channel-name">{{ channelLabel(channel) }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { CHANNEL_COLORS, channelLabel } from '../constants'
import douyinIcon from '../assets/icons/douyin.svg'
import kuaishouIcon from '../assets/icons/kuaishou.svg'
import xiaohongshuIcon from '../assets/icons/xiaohongshu.svg'
import weiboIcon from '../assets/icons/weibo.svg'

const props = withDefaults(defineProps<{
  channel: string
  /** small：表格行里、标签堆里；default：稍大一号 */
  size?: 'small' | 'default'
  /** 占满一个固定宽度（表格的平台列用，上下对齐） */
  block?: boolean
}>(), { size: 'small', block: false })

const ICONS: Record<string, string> = {
  douyin: douyinIcon, kuaishou: kuaishouIcon, xiaohongshu: xiaohongshuIcon, weibo: weiboIcon,
}

const icon = computed(() => ICONS[props.channel] || '')
const color = computed(() => CHANNEL_COLORS[props.channel] || '#6c5dd3')

/** 品牌色 → 浅底（约 10% 透明度）+ 淡边框；字用品牌色本身 */
const style = computed(() => ({
  color: color.value,
  background: `${color.value}14`,
  borderColor: `${color.value}33`,
}))
</script>

<style scoped>
.channel-tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 22px;
  padding: 0 8px;
  border: 1px solid transparent;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  line-height: 1;
  white-space: nowrap;
  vertical-align: middle;
}
.channel-tag.is-default { height: 26px; padding: 0 10px; font-size: 13px; gap: 6px; }
.channel-tag.is-block { min-width: 76px; }
.channel-icon { width: 14px; height: 14px; flex-shrink: 0; }
.is-default .channel-icon { width: 16px; height: 16px; }
.channel-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
</style>
