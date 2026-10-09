# 去哪儿解析器（原样引入，勿改）

来源：用户提供的 `qunar_sight_crawler`（作者 Manus AI，见 README.upstream.md）。

## 引入了什么、没引入什么

| 文件 | 状态 | 说明 |
|---|---|---|
| `parsers.py` | **原样** | 页面解析。这是整个项目最有价值的部分——去哪儿的 HTML 结构、`self.__next_f.push` 里那段内嵌 JSON、JSON-LD 兜底，全在这儿 |
| `models.py` `utils.py` | **原样** | parsers 的依赖 |
| `client.py` `crawler.py` `cli.py` | **没引入** | HTTP 层换成了 smc 自己的 `ProxiedClient`（代理轮换 + 指纹绑定 + 风控降级），命令行和 CSV 落盘换成了任务调度和入库 |

## 为什么原样保留 parsers.py

去哪儿改版时，页面结构知识是唯一要重写的东西。保持原样意味着：
上游出新版，把这三个文件替换掉即可，`app/collectors/qunar.py` 一行不用动。

改这里之前先想清楚：你要改的是"页面怎么解析"（属于这里），
还是"怎么发请求、怎么入库"（属于 app/collectors/qunar.py）。

## 上游没有做、我们这边补上的

- **代理**：上游用裸 `requests.Session`，没有代理。现在走 `ProxiedClient`，
  和其它六个平台同一套代理池、同一套风控降级。
- **节奏控制**：上游是固定 `delay + jitter`，现在走 `crawl.pace` 的平台配置。
- **robots.txt**：上游每次请求前查 robots 并在 disallow 时**拒绝抓取**。
  这条保留了，但做成可配置（`platforms.qunar.respect_robots`，默认开）。
