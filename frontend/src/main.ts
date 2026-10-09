import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'

import App from './App.vue'
import InfoTip from './components/InfoTip.vue'
import AppIcon from './components/AppIcon.vue'
import IconAction from './components/IconAction.vue'
import router from './router'
import './style.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })

for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

// 说明图标：全局可用，页面上需要解释的地方一律用它，不再铺大段文字
app.component('InfoTip', InfoTip)
// 客户图标集：AppIcon 单个图标，IconAction 表格操作列的图标按钮
app.component('AppIcon', AppIcon)
app.component('IconAction', IconAction)

app.mount('#app')
