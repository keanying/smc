// CloudSense（云听）· Copyright (c) 2026 keanying
// SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
// 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
// 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
// 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

import { createRouter, createWebHashHistory } from 'vue-router'
import { authEnabled, currentUser, isAdmin, loadAuth } from '../composables/auth'
import { isScenicEdition, loadEdition } from '../composables/edition'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    // 登录页不套主框架（没有菜单和顶栏），不用登录就能进
    { path: '/login', name: 'login', component: () => import('../views/LoginView.vue'),
      meta: { title: '登录', bare: true, public: true } },
    // 景区版的概览是另一张页面（BoxHomeView）：只看本盒子分到的景区、本机账号
    { path: '/', name: 'home',
      component: () => (isScenicEdition.value
        ? import('../views/BoxHomeView.vue') : import('../views/HomeView.vue')),
      meta: { title: '概览' } },
    { path: '/scenics', name: 'scenics', component: () => import('../views/ScenicView.vue'),
      meta: { title: '景区管理', hq: true } },
    { path: '/archive', name: 'archive', component: () => import('../views/ArchiveView.vue'),
      meta: { title: '景区档案', hq: true } },
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
      meta: { title: '指标计算', hq: true } },
    { path: '/accounts', name: 'accounts', component: () => import('../views/AccountsView.vue'),
      meta: { title: '账号管理' } },
    { path: '/cluster', name: 'cluster', component: () => import('../views/ClusterView.vue'),
      meta: { title: '集群', hq: true } },
    { path: '/boxes', name: 'boxes', component: () => import('../views/BoxesView.vue'),
      meta: { title: '盒子管理', admin: true, hq: true } },
    // 用户管理并进了系统设置；旧地址跳过去
    { path: '/users', redirect: { path: '/settings', query: { tab: 'users' } } },
    { path: '/settings', name: 'settings', component: () => import('../views/SettingsView.vue'),
      meta: { title: '系统设置', admin: true, hq: true } },
  ],
})

/**
 * 没登录去登录页；只有管理员能进的页面，别的角色回概览；
 * 只有总部版才有的页面（meta.hq），景区版回概览。
 */
router.beforeEach(async (to) => {
  await Promise.all([loadAuth(), loadEdition()])
  if (to.meta.public) return true
  if (authEnabled.value && !currentUser.value) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (to.meta.hq && isScenicEdition.value) return { path: '/' }
  if (to.meta.admin && !isAdmin.value) return { path: '/' }
  return true
})

export default router
