# libs

平台签名脚本存放处。

| 文件 | 用途 | 来源 |
|---|---|---|
| `douyin.cjs` | 抖音 `a_bogus` 签名，导出 `sign_datail` / `sign_reply` | MediaTrace 基座项目 `libs/douyin.cjs` |
| `stealth.min.js` | 浏览器指纹伪装脚本，页面导航前注入 | MediaCrawler / puppeteer-extra-plugin-stealth |
| `_bridge.cjs` | Node 桥接脚本，运行时自动生成，不要手工改 | `app/utils/jsvm.py` 生成 |

## 调用方式

`app/utils/jsvm.py` 维持一个常驻 Node 进程，按行收发 JSON 调用这些脚本。
Node 的查找顺序：`SMC_NODE_BIN` 环境变量 → `PATH` 里的 `node` →
Playwright 自带的 `playwright/driver/node`。所以装了 Playwright 就不用另外装 Node。

## 更新签名脚本

抖音改版导致 `a_bogus` 失效时，替换 `douyin.cjs` 即可，
只要它仍然导出 `sign_datail(queryString, userAgent)` 和 `sign_reply(...)` 两个函数。
换完跑一下：

```bash
python scripts/selfcheck.py     # 「抖音签名 a_bogus」那一项会验证
```
