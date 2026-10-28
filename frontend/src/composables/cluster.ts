// CloudSense（云听）· Copyright (c) 2026 keanying
// SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
// 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
// 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
// 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

import { computed, ref } from 'vue'
import { clusterApi, type ClusterNode, type ClusterOverview } from '../api'

/**
 * 集群里的机器。账号页、任务页要用它把机器编号翻成名字、给添加账号的下拉出选项。
 * 只有一台机器时 isCluster 为 false，那些「机器」列和下拉都不显示，页面和以前一样。
 */
export const clusterInfo = ref<ClusterOverview | null>(null)
export const clusterNodes = computed<ClusterNode[]>(() => clusterInfo.value?.nodes || [])
export const isCluster = computed(() => clusterNodes.value.length > 1)
/** 能放账号、能跑任务的在线机器（角色 主+从 / 从） */
export const workerNodes = computed(() =>
  clusterNodes.value.filter((n) => n.role !== 'master'))

export async function loadCluster(): Promise<void> {
  try {
    clusterInfo.value = await clusterApi.overview()
  } catch {
    // 拿不到就当单机，别让账号页、任务页因为它打不开
  }
}

export function nodeOf(nodeId?: string | null): ClusterNode | undefined {
  return nodeId ? clusterNodes.value.find((n) => n.node_id === nodeId) : undefined
}

export function nodeLabel(nodeId?: string | null): string {
  if (!nodeId) return '未分配'
  return nodeOf(nodeId)?.node_name || nodeId
}

/** 添加账号时默认放哪台：本机能跑任务就放本机，否则第一台在线的从机 */
export function defaultAccountNode(): string {
  const self = clusterNodes.value.find((n) => n.is_self)
  if (self && self.role !== 'master') return self.node_id
  return workerNodes.value.find((n) => n.online)?.node_id || ''
}
