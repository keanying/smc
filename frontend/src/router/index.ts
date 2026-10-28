// 景区社媒采集系统 · Copyright (c) 2026 keanying
// SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
// 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
// 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
// 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

import { createRouter, createWebHashHistory } from 'vue-router'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'home', component: () => import('../views/HomeView.vue'),
      meta: { title: '概览' } },
    { path: '/scenics', name: 'scenics', component: () => import('../views/ScenicView.vue'),
      meta: { title: '景区管理' } },
    { path: '/archive', name: 'archive', component: () => import('../views/ArchiveView.vue'),
      meta: { title: '景区档案' } },
    { path: '/tasks', name: 'tasks', component: () => import('../views/TasksView.vue'),
      meta: { title: '任务管理' } },
    { path: '/tasks/new', name: 'task-new', component: () => import('../views/NewTaskView.vue'),
      meta: { title: '新建任务' } },
    { path: '/tasks/:taskId', name: 'task-detail', component: () => import('../views/TaskDetailView.vue'),
      meta: { title: '任务详情' } },
    { path: '/data', name: 'data', component: () => import('../views/DataView.vue'),
      meta: { title: '数据中心' } },
    { path: '/labeling', name: 'labeling', component: () => import('../views/LabelingView.vue'),
      meta: { title: '标注审核' } },
    { path: '/metrics', name: 'metrics', component: () => import('../views/MetricsView.vue'),
      meta: { title: '指标计算' } },
    { path: '/accounts', name: 'accounts', component: () => import('../views/AccountsView.vue'),
      meta: { title: '账号管理' } },
    { path: '/settings', name: 'settings', component: () => import('../views/SettingsView.vue'),
      meta: { title: '系统设置' } },
  ],
})

export default router
