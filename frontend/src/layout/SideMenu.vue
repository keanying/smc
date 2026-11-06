<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!--
    侧边栏树形菜单（参考 layui 的 lay-menu）。对齐规则：
    - 所有一级菜单的图标在同一列，文字在同一列；有子菜单的右边一个箭头
    - 子菜单的文字和一级菜单的文字左对齐；左边一根竖线从父级图标正下方引出，每项一个小横杠（树形引导线）
    - 收起后宽度刚好让图标留在原来那一列（展开 / 收起图标不跳），文字被裁掉；
      悬停一级菜单出提示，悬停有子菜单的出一张小卡片，卡片里还是同样的树形线
  -->
  <div class="side-nav" :class="{ 'is-collapsed': collapsed }">
  <nav class="nav-list" aria-label="主菜单">
    <template v-for="item in items" :key="item.key">
      <!-- 有子菜单：展开时点标题折叠 / 展开；收起时悬停弹卡片，点图标进第一个子页面 -->
      <div v-if="item.children" class="nav-group" :class="{ 'is-open': isOpen(item.key) }">
        <el-popover
          :disabled="!collapsed" placement="right-start" trigger="hover" :show-after="60" :hide-after="120"
          :offset="14" :width="168" :show-arrow="false" popper-class="side-flyout"
        >
          <template #reference>
            <button
              type="button" class="nav-item"
              :class="{ 'is-active': (collapsed || !isOpen(item.key)) && groupActive(item) }"
              :aria-expanded="collapsed ? undefined : isOpen(item.key)"
              @click="collapsed ? go(item.children[0].path) : toggle(item.key)"
            >
              <span class="nav-icon"><el-icon><component :is="item.icon" /></el-icon></span>
              <span class="nav-label" :class="{ 'is-strong': groupActive(item) }">{{ item.label }}</span>
              <el-icon class="nav-arrow"><ChevronDown /></el-icon>
            </button>
          </template>
          <!-- 收起时的弹出卡片。分组标题：子页面被折叠起来看不见时（侧栏收起或分组合上），由标题来高亮 -->
          <div class="flyout-title">{{ item.label }}</div>
          <div class="nav-tree">
            <router-link
              v-for="c in item.children" :key="c.path" :to="c.path"
              class="nav-child" :class="{ 'is-active': c.path === active }"
            >{{ c.label }}</router-link>
          </div>
        </el-popover>
        <!-- 展开时的子菜单：高度用 grid 0fr→1fr 过渡，不用算高度 -->
        <div class="nav-children" :aria-hidden="!isOpen(item.key) || collapsed">
          <div class="nav-children-inner">
            <div class="nav-tree">
              <router-link
                v-for="c in item.children" :key="c.path" :to="c.path" :tabindex="isOpen(item.key) && !collapsed ? 0 : -1"
                class="nav-child" :class="{ 'is-active': c.path === active }"
              >{{ c.label }}</router-link>
            </div>
          </div>
        </div>
      </div>

      <!-- 普通一级菜单 -->
      <el-tooltip
        v-else :disabled="!collapsed" :content="item.label" placement="right" :show-after="80"
        :offset="14"
      >
        <router-link :to="item.path!" class="nav-item" :class="{ 'is-active': item.path === active }">
          <span class="nav-icon"><el-icon><component :is="item.icon" /></el-icon></span>
          <span class="nav-label">{{ item.label }}</span>
        </router-link>
      </el-tooltip>
    </template>
  </nav>
  <!-- 底部操作（退出登录）：和菜单同一列对齐 -->
  <div v-if="actions?.length" class="nav-foot">
    <el-tooltip
      v-for="a in actions" :key="a.key" :disabled="!collapsed" :content="a.label" placement="right"
      :show-after="80" :offset="14"
    >
      <button type="button" class="nav-item" :aria-label="a.label" @click="a.onClick">
        <span class="nav-icon nav-icon-soft"><el-icon><component :is="a.icon" /></el-icon></span>
        <span class="nav-label">{{ a.label }}</span>
      </button>
    </el-tooltip>
  </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, type Component } from 'vue'
import { useRouter } from 'vue-router'
import { ChevronDown } from '@vicons/ionicons5'

export interface NavItem {
  key: string
  label: string
  icon: Component
  path?: string
  children?: Array<{ path: string; label: string }>
}

export interface NavAction {
  key: string
  label: string
  icon: Component
  onClick: () => void
}

const props = defineProps<{
  items: NavItem[]
  actions?: NavAction[]
  /** 当前高亮的菜单路径（子页面已经映射成它所属的那一项） */
  active: string
  collapsed: boolean
}>()

const router = useRouter()

/** 哪些分组是展开的：记在本机，下次进来还是这样；默认全展开 */
const OPEN_KEY = 'smc.ui.navClosed'
const closed = ref<Set<string>>(new Set(JSON.parse(localStorage.getItem(OPEN_KEY) || '[]')))
function isOpen(key: string) {
  return !closed.value.has(key)
}
function toggle(key: string) {
  const next = new Set(closed.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  closed.value = next
  localStorage.setItem(OPEN_KEY, JSON.stringify([...next]))
}
function groupActive(item: NavItem) {
  return !!item.children?.some((c) => c.path === props.active)
}
function go(path: string) {
  router.push(path)
}

// 进到某个分组下的页面（比如从别处链接到运行记录）：那个分组自动展开，不然高亮的那项被折叠着看不见
watch(() => props.active, () => {
  const group = props.items.find((i) => groupActive(i))
  if (group && !isOpen(group.key)) toggle(group.key)
}, { immediate: true })
</script>

<style scoped>
/*
 * 尺寸都从这几个变量来，展开 / 收起 / 子菜单对齐全靠它们：
 *   图标中心 = 菜单左内边距 + 项左内边距 + 图标框一半
 *   一级文字起点 = 项左内边距 + 图标框 + 间距
 */
.side-nav {
  --nav-pad: 10px;      /* 菜单整体左右内边距 */
  --item-pad: 10px;     /* 每一项左内边距 */
  --icon-box: 26px;     /* 图标框 */
  --gap: 10px;          /* 图标和文字的间距 */
  --item-h: 40px;
  --child-h: 34px;
  --line: rgba(255, 255, 255, 0.24);
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.nav-list {
  flex: 1;
  min-height: 0;
  padding: 10px var(--nav-pad);
  overflow-y: auto;
  overflow-x: hidden;
  scrollbar-width: none;
}
.nav-list::-webkit-scrollbar { display: none; }
.nav-foot {
  padding: 8px var(--nav-pad);
  border-top: 1px solid rgba(255, 255, 255, 0.14);
}
.nav-icon-soft { background: rgba(255, 255, 255, 0.14); }

.nav-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--gap);
  width: 100%;
  height: var(--item-h);
  margin: 2px 0;
  padding: 0 10px 0 var(--item-pad);
  border: none;
  border-radius: 12px;
  background: transparent;
  color: rgba(255, 255, 255, 0.74);
  font: inherit;
  font-size: 13px;
  text-align: left;
  text-decoration: none;
  white-space: nowrap;
  overflow: hidden;
  cursor: pointer;
  transition: background-color 0.15s ease, color 0.15s ease;
}
.nav-item:hover { background: rgba(255, 255, 255, 0.10); color: #fff; }
.nav-item:focus-visible, .nav-child:focus-visible { outline: 2px solid rgba(255, 255, 255, 0.7); outline-offset: -2px; }
.nav-item.is-active {
  background: rgba(255, 255, 255, 0.18);
  color: #fff;
  font-weight: 600;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.12);
}

.nav-icon {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: var(--icon-box);
  height: var(--icon-box);
  border-radius: 8px;
  font-size: 17px;
  transition: background-color 0.15s ease, color 0.15s ease;
}
/* 选中项：图标垫白底、图标变主题紫 */
.nav-item.is-active .nav-icon {
  background: #fff;
  color: var(--smc-primary);
  box-shadow: 0 4px 10px rgba(40, 30, 110, 0.25);
}
.nav-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: opacity 0.15s ease;
}
.nav-label.is-strong { color: #fff; font-weight: 600; }
.nav-arrow {
  flex: none;
  font-size: 13px;
  opacity: 0.7;
  transition: transform 0.2s ease, opacity 0.15s ease;
}
.nav-group:not(.is-open) .nav-arrow { transform: rotate(-90deg); }

/* ---------- 子菜单（树形） ---------- */
.nav-children {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows 0.2s ease;
}
.nav-group.is-open .nav-children { grid-template-rows: 1fr; }
.nav-children-inner { overflow: hidden; min-height: 0; }
/* 竖线从父级图标的正中往下走 */
.nav-children .nav-tree { margin-left: calc(var(--item-pad) + var(--icon-box) / 2); }
.nav-tree {
  position: relative;
  padding: 2px 0 4px;
  border-left: 1px solid var(--line);
}
/* 子项：文字和一级文字对齐 → 子项左边距 + 内边距 = 图标框一半 + 间距 */
.nav-child {
  --child-indent: 10px;
  position: relative;
  display: flex;
  align-items: center;
  height: var(--child-h);
  margin: 2px 0 2px var(--child-indent);
  padding-left: calc(var(--icon-box) / 2 + var(--gap) - var(--child-indent) - 1px);
  border-radius: 10px;
  color: rgba(255, 255, 255, 0.68);
  font-size: 13px;
  text-decoration: none;
  white-space: nowrap;
  transition: background-color 0.15s ease, color 0.15s ease;
}
/* 树形线上的小横杠 */
.nav-child::before {
  content: '';
  position: absolute;
  left: calc(-1 * var(--child-indent));
  top: 50%;
  width: calc(var(--child-indent) - 3px);
  height: 1px;
  background: var(--line);
}
.nav-child:hover { background: rgba(255, 255, 255, 0.10); color: #fff; }
.nav-child.is-active { background: rgba(255, 255, 255, 0.18); color: #fff; font-weight: 600; }
/* 选中的子项：横杠变白，竖线上点一个白点 */
.nav-child.is-active::before { background: #fff; }
.nav-child.is-active::after {
  content: '';
  position: absolute;
  left: calc(-1 * var(--child-indent) - 3px);
  top: calc(50% - 2.5px);
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #fff;
}

/* ---------- 收起：图标留在原来那一列，只把文字、箭头、子菜单藏起来 ---------- */
.side-nav.is-collapsed .nav-label,
.side-nav.is-collapsed .nav-arrow { opacity: 0; }
.side-nav.is-collapsed .nav-children { grid-template-rows: 0fr; }
</style>

<style>
/* 收起时悬停分组弹出的小卡片（挂在 body 下，不能用 scoped）：白底，里面同样是树形线 */
.side-flyout.el-popover.el-popper {
  padding: 10px 10px 8px;
  border: none;
  border-radius: 12px;
  box-shadow: 0 12px 32px rgba(40, 30, 110, 0.18);
}
.side-flyout .flyout-title {
  padding: 0 4px 6px 6px;
  font-size: 12px;
  color: var(--smc-text-secondary);
}
.side-flyout .nav-tree {
  --line: var(--el-border-color);
  --icon-box: 26px;
  --gap: 10px;
  --child-h: 34px;
  position: relative;
  margin-left: 10px;
  padding: 0 0 2px;
  border-left: 1px solid var(--line);
}
.side-flyout .nav-child {
  --child-indent: 10px;
  position: relative;
  display: flex;
  align-items: center;
  height: var(--child-h);
  margin: 2px 0 2px var(--child-indent);
  padding-left: 10px;
  border-radius: 8px;
  color: var(--smc-text);
  font-size: 13px;
  text-decoration: none;
  white-space: nowrap;
}
.side-flyout .nav-child::before {
  content: '';
  position: absolute;
  left: calc(-1 * var(--child-indent));
  top: 50%;
  width: calc(var(--child-indent) - 3px);
  height: 1px;
  background: var(--line);
}
.side-flyout .nav-child:hover { background: var(--el-fill-color-light); color: var(--el-color-primary); }
.side-flyout .nav-child.is-active { background: var(--el-color-primary-light-9); color: var(--el-color-primary); font-weight: 600; }
.side-flyout .nav-child.is-active::before { background: var(--el-color-primary); }
.side-flyout .nav-child.is-active::after {
  content: '';
  position: absolute;
  left: calc(-1 * var(--child-indent) - 3px);
  top: calc(50% - 2.5px);
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--el-color-primary);
}
</style>
