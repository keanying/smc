# v5.1 修复说明（2026-09-05）

针对「从页面发起任务跑不起来」的三个 bug。第一个是崩溃，
后两个才是真正危险的——它们让任务**带着 0 条数据报成功**。

---

## 1. 同一个 profile 目录被开了两次（崩溃）

```
15:45:08 [kuaishou/小尹呀] 采集页面已就绪，12 个 Cookie   ← 接口模式开的第 1 个
15:45:14 [快手] 采集客户端就绪，账号 小尹呀
15:45:14 [快手] 任务选的是「拟人」，跳过接口直接用浏览器模拟真人采集
15:45:15 失败：BrowserType.launch_persistent_context:
         Target page, context or browser has been closed   ← 拟人开第 2 个
```

`KuaishouCollector.prepare()` 不管三七二十一先开一个 PageSession 取 Cookie、
等签名；等到 `collect_by_keyword` 里才发现"任务选的是拟人"，
再让 `KuaishouBrowserCollector.prepare()` 开**第二个**——
两个 `launch_persistent_context` 指向同一个 `user-data-dir`。

Chrome 不会在同一个 profile 上开第二个实例：它把请求交给已经在跑的进程，
然后自己退出（`exitCode=0`）。Playwright 只看到"进程没了"，
报的却是一句看不出因果的 `Target page, context or browser has been closed`。

Chrome 自己那句提示还被 GBK 糊成了乱码。解出来是：

```
已在现有的浏览器会话中打开。
```

（把这句用 GBK 编码再按 UTF-8 解，得到的乱码和日志里那串**逐字符相同**——
这不是猜的。）

**修**：`prepare()` 一开头就判断拟人模式，直接转手 `_init_browser_fallback`
并 return，根本不开接口那个浏览器。
**抖音一直是这么写的**（`DouyinCollector.prepare` 第一句就是这个判断），
快手漏了这一步。

**顺带修混合模式**：接口跑到一半被风控挡了才切拟人，那时候接口的浏览器
还开着（快手签名要常驻页面），再开一个照样撞。现在把开着的会话
**交接**过去（`adopt_session`）——不重开、不换 Cookie，也没有
"先关再开"那一瞬间 profile 锁还没释放的竞争。所有权不转移：谁开的谁关。

---

## 2. 失败的采集器被缓存，重试形同虚设 ⭐

```
15:45:17 [快手] 任务选的是「拟人」…       ← 重试
15:45:21 [快手] 搜索结果已就绪
15:45:24 [kuaishou] 连续 3 次滚动无新数据，结束
15:45:24 关键字 [八大处公园] 采集到 0 条作品
（后面 7 个关键字，全部 0 条）
```

`_init_browser_fallback` 的第一句是 `if self._browser_collector is not None: return`，
而 `self._browser_collector = XxxBrowserCollector(...)` 在 `prepare()` **之前**赋值。
所以第 1 次 prepare 崩了之后，半成品被留在手里，
**之后每一次重试都直接 return，再也没有 prepare 过**。

**修**：prepare 失败就把 `_browser_collector` 清掉再抛，让重试真的能重来。
快手和抖音都改了。

---

## 3. 没有页面时静悄悄返回 0 条 ⭐⭐ 最危险

上面那个半成品采集器 `_page` 一直是 `None`，
而 `_goto_search`、`_scroll_comments`、`_extract_new_works` 里每一个
都写着 `if self._page is None: return` ——
一路安静地返回，最后报「连续 3 次滚动无新数据」「采集到 0 条作品」，
八个关键字全 0，**任务最后还报成功**。

日志里没有任何异常,看上去就像"这几个关键字快手上确实没内容"。

**修**：`collect_by_keyword` / `collect_comments` 进门先 `_require_page()`，
没有页面直接抛 RuntimeError，把真正的原因说清楚：

```
[kuaishou] 搜索关键字时没有可用页面：采集器没 prepare 成功就被拿来用了。
多半是上一次 prepare() 失败（比如同一个账号的 profile 被另一个浏览器占着），
但失败的采集器被缓存了下来。
```

少采一条是小事；**采了个寂寞还说自己成功**，是必须炸出来的事。

---

## 验证

新增 `tests/test_browser_fallback_profile_clash.py`（7 条）。
四个修复**逐条改坏验证**过，还原任何一个测试都会红：

```
✅ 抓到  改坏「拟人模式不先开接口浏览器」
✅ 抓到  改坏「prepare 失败清掉半成品」
✅ 抓到  改坏「没页面要炸」
✅ 抓到  改坏「交接会话不重开」
```

另有一个可跑的复现脚本验证行为（不是只看代码文本）：
prepare 失败后 `_browser_collector` 确实变回 None、重试确实又抛了一次、
没有页面时 `collect_by_keyword` 确实炸而不是返回 0 条。

- `pytest tests -q`（排除 UI smoke）：**336 passed**
- `verify_kuaishou_offline.py` 8 段全过
- `verify_kuaishou_browser.py` preflight 24 项 0 个 ❌
- `verify_boot.py` 全过

### 有一件事我没能验证

**同一个 profile 开两次这个崩溃，我在 Linux 上复现不出来**——
这里的 Chromium 允许两个 context 共用一个 `user-data-dir`，不报错。
这是 Windows 上 Chrome 的行为。

所以第 1 条的**修法**是靠日志 + 代码推断的（证据链是完整的：
Chrome 那句提示解出来就是"已在现有的浏览器会话中打开"，
`prepare()` 里确实有第二次 launch，抖音的正确写法就摆在旁边），
但它**没有在真实 Windows 环境上跑通过**。请你这边实跑确认。

第 2、3 条和平台无关，已经在这里跑通验证过。

---

## 你这次实跑该看到什么

启动后应该**不再有** `[kuaishou/小尹呀] 采集页面已就绪，12 个 Cookie`
这一行（拟人模式不开接口浏览器了），而是直接：

```
[kuaishou] 浏览器模拟采集就绪，账号 小尹呀
搜索关键字：八大处公园
[快手] 打开首页：https://www.kuaishou.com/
[快手] 已在搜索框输入「八大处公园」
[快手] 已点「搜索」
[快手] 搜索结果已就绪（这个平台没有排序/时间筛选，直接开始采）
  ┌─ 作品 1/200 ────────────────────────────────
  ...
```

**上一版那三行「打开首页 / 已在搜索框输入 / 已点搜索」在你的日志里一行都没有**——
这正是第 3 条的症状：`_goto_search` 因为 `_page is None` 在第一行就 return 了。
这次要是还看不到它们，说明问题在别处，把日志再发我。
