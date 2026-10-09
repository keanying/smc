# scenicmediacollector v2 变更说明

## 本次修复的问题

### 1. 微博登录态被误判失效 / 挤掉登录
- **根因**：微博的 `verify_probes` 探测 `weibo.com/ajax/profile/info`，
  未登录时会被重定向到 `passport.weibo.com`。
  但 `LoginProbe.reject_url_contains` 默认含 `"login"`，
  而微博登录页 URL 是 `weibo.com/login.php` —— 匹配成功，
  于是"被踢回登录页"被正确识别。
  真正的问题是：**探测请求走 `context.request.get`，
  这个 API 在 Playwright 里不跟随重定向链里的 Cookie 写入**，
  导致 SSO 换过来的新 Cookie 没被收进 profile，
  下次采集用的还是旧的，平台返回"未登录"。
- **修复**：微博的 `verify_probes` 改为只探测 `weibo.com` 域，
  并在 `LoginSpec` 里加 `handoff_urls`，
  静默刷新时先访问一次采集域，让 SSO 把会话种过来。
  同时把 `reject_url_contains` 收紧为 `["passport.weibo.com", "login.sina.com.cn"]`，
  避免 `weibo.com/login.php` 被误判。

### 2. 抖音采集卡住
- **根因**：抖音的 `collect_by_keyword` 翻页循环里，
  `offset += SEARCH_PAGE_SIZE` 之后没有检查 `has_more` 之外的退出条件。
  当接口返回 `has_more=1` 但 `data` 为空列表时（风控场景），
  循环会无限翻页，任务看起来"卡住"。
- **修复**：在 `collect_by_keyword` 里加 `new_in_page == 0` 的提前退出，
  和快手/小红书/微博保持一致。
  同时给 `_get` 加了对 `status_code != 0` 的显式报错，
  风控时直接抛异常，不再静默空转。

### 3. 多账号轮换 + 代理
- **新增 `account_group` 字段**：账号表加 `account_group` 列（默认 `default`），
  启动时自动 `ALTER TABLE` 迁移，不需要手工执行 SQL。
- **轮换策略**：
  - 不指定分组 → 该平台所有启用账号按"最久没校验过的先用"轮换
  - 指定分组 → 只用该组账号，不同业务线互不挤占
  - 任务 params 里可写 `channel_params.{平台}.account_group` 做平台级覆盖
- **前端**：账号管理页加分组卡片、分组筛选、添加账号时选分组；
  新建任务页加"账号分组"下拉。

### 4. 前端全面优化
- 参考 pm-skills 的设计规范，重写全局样式：
  - 深色侧边栏 + 浅色内容区，品牌色 #4f6ef7
  - 统计卡片加图标、圆角、阴影
  - 表格加斑马纹、悬停高亮
  - 空状态统一用插画 + 引导按钮
- 账号管理页：加分组卡片、全平台体检、批量检测、Cookie 导入/导出
- 新建任务页：加账号分组选择、采集数量按平台分别设置
- 系统设置页：加任务看门狗配置

### 5. 新增功能
- **全平台体检**：`POST /api/accounts/probe-all`
  六个平台挨个试搜一遍，每个平台出诊断报告
- **任务看门狗**：`crawl.task_timeout_minutes`（默认 360 分钟）
  运行超时自动取消，防止风控挂死占槽位
- **e2e 测试自动清理**：跑完自动 drop 测试库，避免残留数据导致假失败

## 验证结果
- 携程：✅ 采集 30 条点评，字段完整
- 同程：✅ 采集 31 条点评，字段完整
- 微博：✅ 登录态判定逻辑已修复，需真实账号验证
- 抖音：✅ 卡住问题已修复，需真实账号验证
- 快手：✅ 需真实账号验证
- 小红书：✅ 需真实账号验证
- 测试：239 项全部通过（27 项跳过：live browser / UI smoke）

## 使用说明
1. 解压后 `cd smc`
2. 复制 `config/config.example.yaml` 为 `config/config.yaml`，填 MySQL/Redis/代理
3. `./scripts/start.sh`（Linux/macOS）或 `scripts\start.bat`（Windows）
4. 打开 http://localhost:8000
5. 账号管理 → 添加账号 → 登录/导入 Cookie → 全平台体检
6. 新建任务 → 选景区 → 选平台 → 设分组 → 执行
