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
