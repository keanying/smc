<template>
  <!--
    统一的"说明"入口：页面上不再铺大段描述文字，需要解释的地方放这个图标，
    鼠标移上去才显示。全局注册（main.ts），任何页面直接写 <InfoTip content="..." />。
    内容长或要加粗/换行时用默认插槽：<InfoTip>第一行<br>第二行</InfoTip>
  -->
  <el-tooltip :placement="placement" :show-after="120" :popper-class="'info-tip-popper'">
    <template #content>
      <div class="info-tip-content" :style="{ maxWidth: `${width}px` }">
        <slot>{{ content }}</slot>
      </div>
    </template>
    <img :src="icon" class="info-tip" alt="说明" @click.stop.prevent />
  </el-tooltip>
</template>

<script setup lang="ts">
import icon from '../assets/icons/actions/info.png'

withDefaults(defineProps<{
  content?: string
  placement?: string
  width?: number
}>(), { content: '', placement: 'top', width: 320 })
</script>

<style scoped>
.info-tip {
  width: 14px;
  height: 14px;
  margin-left: 4px;
  vertical-align: -2px;
  opacity: 0.4;
  cursor: help;
  transition: opacity 0.15s ease;
  user-select: none;
}
.info-tip:hover { opacity: 0.85; }
.info-tip-content {
  font-size: 12px;
  line-height: 1.7;
  white-space: normal;
}
</style>
