// CloudSense（云听）· Copyright (c) 2026 keanying
// SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
// 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
// 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
// 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

/**
 * 侧边栏菜单图标：统一用 Ionicons 5 的线框款（iView 用的就是 Ionicons 这一套，这是它的新版）。
 *
 * - 为什么不用 Element 自带图标：Element 的图标实心、线框混着，粗细不一，排成一列参差不齐
 * - 为什么不直接引 iView / Semantic UI 的字体图标：整套字体几百 KB、要单独引 CSS，
 *   还会和 Element 的类名打架；@vicons/ionicons5 是一个图标一个 Vue 组件，只打包用到的那几个
 * - 两种侧栏（MainLayout 的总部版、BoxLayout 的景区版）都从这里取，同一个菜单两边图标一样
 */
import {
  AnalyticsOutline, HomeOutline, KeyOutline, LayersOutline, LocationOutline, LogOutOutline,
  PeopleOutline, PricetagsOutline, ReceiptOutline, ServerOutline, SettingsOutline, TrendingUpOutline,
} from '@vicons/ionicons5'

export const NAV_ICONS = {
  home: HomeOutline, // 概览
  target: LocationOutline, // 目标管理
  task: LayersOutline, // 任务管理
  run: ReceiptOutline, // 运行记录（景区版单独一项时用）
  data: AnalyticsOutline, // 数据中心
  label: PricetagsOutline, // 标注审核
  metrics: TrendingUpOutline, // 指标计算
  account: PeopleOutline, // 账号管理
  cluster: ServerOutline, // 集群
  settings: SettingsOutline, // 系统设置
  password: KeyOutline, // 修改密码
  logout: LogOutOutline, // 退出登录
}
