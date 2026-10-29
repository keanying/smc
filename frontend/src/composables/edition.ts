// CloudSense（云听）· Copyright (c) 2026 keanying
// SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
// 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
// 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
// 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

import { computed, ref } from 'vue'
import { editionApi, type EditionInfo } from '../api'

/**
 * 总部版还是景区版。路由守卫第一次进页面时拉一次（后端启动后就不会变）。
 * 景区版换一套深色外框布局（BoxLayout），菜单只有概览、任务、数据、标注、账号；
 * 景区管理、景区档案、指标计算、集群、系统设置这些页面进不去（后端也拦着）。
 */
export const editionInfo = ref<EditionInfo | null>(null)
export const isScenicEdition = computed(() => editionInfo.value?.edition === 'scenic')

let pending: Promise<EditionInfo | null> | null = null

export function loadEdition(): Promise<EditionInfo | null> {
  if (editionInfo.value) return Promise.resolve(editionInfo.value)
  if (!pending) {
    pending = editionApi.info()
      .then((info) => {
        editionInfo.value = info
        // 全局样式按这个类换配色（style.css 里的 html.edition-scenic）
        document.documentElement.classList.toggle('edition-scenic', info.edition === 'scenic')
        return info
      })
      .catch(() => null)
      .finally(() => { pending = null })
  }
  return pending
}
