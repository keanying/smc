<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!-- 全站通知：每个用到的位置一摞（composables/toast.ts）。挂在 App.vue 里（#app 内），自动跟着全站缩放 -->
  <ToastRegion v-for="p in placements" :key="p" :placement="p" />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { toastState, type ToastPlacement } from '../composables/toast'
import ToastRegion from './ToastRegion.vue'

const placements = computed(() => [...new Set(toastState.items.map((t) => t.placement))] as ToastPlacement[])
</script>
