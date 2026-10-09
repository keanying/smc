// 全局注册的组件（main.ts 里 app.component 的那些），给模板类型检查用
export {}

declare module 'vue' {
  export interface GlobalComponents {
    InfoTip: typeof import('./components/InfoTip.vue')['default']
    AppIcon: typeof import('./components/AppIcon.vue')['default']
    IconAction: typeof import('./components/IconAction.vue')['default']
  }
}
