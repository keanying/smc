<template>
  <!--
    表格「操作」列的图标按钮：只显示图标，鼠标移上去显示名字。
    一排文字链接（编辑 建任务 删除 …）换成它，表格清爽很多。
    危险操作仍然由调用方自己套 el-popconfirm / ElMessageBox 确认。
    全局注册：<IconAction icon="edit" tip="编辑" @click="..." />
  -->
  <el-tooltip :content="tip" placement="top" :show-after="200" :disabled="!tip">
    <button
      type="button" class="icon-action" :class="{ 'is-disabled': disabled || loading }"
      :disabled="disabled || loading" :aria-label="tip" @click="$emit('click', $event)"
    >
      <el-icon v-if="loading" class="is-loading" :size="size"><Loading /></el-icon>
      <AppIcon v-else :name="icon" :size="size" />
    </button>
  </el-tooltip>
</template>

<script setup lang="ts">
import { Loading } from '@element-plus/icons-vue'
import AppIcon, { type IconName } from './AppIcon.vue'

withDefaults(defineProps<{
  icon: IconName
  tip?: string
  size?: number
  disabled?: boolean
  loading?: boolean
}>(), { tip: '', size: 16, disabled: false, loading: false })
defineEmits<{ (e: 'click', ev: MouseEvent): void }>()
</script>

<style scoped>
.icon-action {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  border-radius: 6px;
  cursor: pointer;
  padding: 0;
  transition: background-color 0.15s ease;
  vertical-align: middle;
}
.icon-action:hover { background: #f1f3f6; }
.icon-action.is-disabled { opacity: 0.35; cursor: not-allowed; }
.icon-action.is-disabled:hover { background: transparent; }
.icon-action + .icon-action { margin-left: 2px; }
</style>
