<template>
  <img :src="ICONS[name]" :alt="name" class="app-icon" :style="{ width: `${size}px`, height: `${size}px` }" />
</template>

<script lang="ts">
// 客户提供的图标集（assets/icons/actions/*.png，64px 彩色）。文件名 → 用途：
//   edit 编辑  delete 删除  view 查看  copy 复制  export 导出  publish 发布/执行
//   lock 锁定  unlock 解锁  pending 审核中/等待  online 上线/启用  offline 下线/停用/退出
//   revoke 撤回/取消/重置  clear 清理缓存  query 查询/试搜/体检  task 任务
//   transfer 转交/挂接  unlink 失效/断开(sso)  compare 对比  dev 开发/档案  info 说明
// ⚠️ online/copy/export/view/clear 是黑色的，放深色背景上看不见——只用在浅色内容区。
const files = import.meta.glob('../assets/icons/actions/*.png', { eager: true, import: 'default' })
export const ICONS: Record<string, string> = Object.fromEntries(
  Object.entries(files).map(([path, url]) => [path.split('/').pop()!.replace('.png', ''), url as string]),
)
export type IconName =
  | 'edit' | 'delete' | 'view' | 'copy' | 'export' | 'publish' | 'lock' | 'unlock'
  | 'pending' | 'online' | 'offline' | 'revoke' | 'clear' | 'query' | 'task'
  | 'transfer' | 'unlink' | 'compare' | 'dev' | 'info'
</script>

<script setup lang="ts">
withDefaults(defineProps<{ name: IconName; size?: number }>(), { size: 16 })
</script>

<style scoped>
.app-icon { display: inline-block; vertical-align: middle; object-fit: contain; user-select: none; }
</style>
