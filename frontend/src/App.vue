<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!-- 登录页单独一屏，不套菜单和顶栏 -->
  <router-view v-if="route.meta.bare" />
  <!-- 景区版（景区盒子）换一套深色外框布局 -->
  <BoxLayout v-else-if="route.name && isScenicEdition" />
  <MainLayout v-else-if="route.name" />
  <!-- 全站通知（composables/toast.ts）：放在 #app 里面，跟着全站缩放 -->
  <ToastHost />
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'
import MainLayout from './layout/MainLayout.vue'
import BoxLayout from './layout/BoxLayout.vue'
import ToastHost from './components/ToastHost.vue'
import { isScenicEdition } from './composables/edition'

const route = useRoute()
</script>
