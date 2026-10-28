// 景区社媒采集系统 · Copyright (c) 2026 keanying
// SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
// 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
// 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
// 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

import { ref } from 'vue'
import { settingsApi } from '../api'

/**
 * 可选模块的开关（在「系统设置」里打开）。菜单根据它决定显不显示对应页面。
 * 设置页保存后会直接改这里，菜单立刻跟着变，不用刷新。
 */
export const metricsEnabled = ref(false)

export async function loadFeatures(): Promise<void> {
  try {
    const s = await settingsApi.get()
    metricsEnabled.value = !!s.config?.metrics?.enabled
  } catch {
    // 拿不到就保持现状，别因为一个开关把菜单弄坏
  }
}
