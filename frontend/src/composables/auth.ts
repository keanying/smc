// CloudSense（云听）· Copyright (c) 2026 keanying
// SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
// 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
// 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
// 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

import { computed, ref } from 'vue'
import { authApi, type AuthStatus, type CurrentUser } from '../api'

/**
 * 当前登录的人。路由守卫第一次进页面时拉一次，登录 / 退出后更新。
 * 登录关掉时（enabled=false）把所有权限都当成有，页面和以前一样。
 */
export const authStatus = ref<AuthStatus | null>(null)
export const currentUser = computed<CurrentUser | null>(() => authStatus.value?.user || null)
export const authEnabled = computed(() => !!authStatus.value?.enabled)
export const isAdmin = computed(() => !authEnabled.value || currentUser.value?.role === 'admin')
/** 能改东西（操作员和管理员）。只读用户看到的写按钮会被后端拦下 */
export const canWrite = computed(() => !authEnabled.value || currentUser.value?.role !== 'viewer')

let pending: Promise<AuthStatus | null> | null = null

export function loadAuth(force = false): Promise<AuthStatus | null> {
  if (authStatus.value && !force) return Promise.resolve(authStatus.value)
  if (!pending || force) {
    pending = authApi.status()
      .then((s) => { authStatus.value = s; return s })
      .catch(() => null)
      .finally(() => { pending = null })
  }
  return pending
}

export function setLoggedIn(user: CurrentUser): void {
  if (authStatus.value) authStatus.value = { ...authStatus.value, user, needs_setup: false }
}
