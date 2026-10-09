# v6 变更说明

接着 v5 往下做的一轮。分两部分：**你上传的四个文件的合并结果**，
和**标注引擎接入实跑后修掉的问题**。

---

## 0. 先说一件重要的事：你上传的文件比我手上的新

你这次发的 `kuaishou_browser.py` 里有一整块我这边**没有**的功能——
作品列表按滑动次数往下滚（`list_scroll_times` / `DEFAULT_LIST_SCROLL_TIMES` /
`LIST_OVERFETCH_FACTOR` / 重写的 `collect_by_keyword`）。

我逐行比对过四个文件，结论是：

| 文件 | 结果 |
|---|---|
| `kuaishou_browser.py` | 你的是我的**超集**，我这边整块滚列表的逻辑是缺的 → **整份采用你的** |
| `verify_kuaishou_browser.py` | 你的多了滚列表的自检项，还修了我一个 bug（`Pacer(load_config(), ...)`，我那版传的是函数不是结果）→ **采用你的** |
| `verify_kuaishou_offline.py` | 你的离线夹具多了可滚容器和分页关键字 → **采用你的** |
| `kuaishou.py` | 完全一致，无需处理 |

采用后重跑：离线验证 14 项全过，`preflight` 30+ 项全绿，无一失败。

> 这块逻辑是必须保住的：快手搜索接口没有时间范围参数，时间窗和关键字
> 过滤都在上层做。基类那句「连续 3 轮没有新作品就结束」会在第一屏之后
> 就收工——现象正是你之前报的「第一个关键字直接就没采」。

---

## 1. 标注引擎：接进来之后实跑，抓到 3 个问题

引擎本体**一行没改**（和你发的 zip 逐文件比对，字节一致）。
问题全在 smc 这一侧的接入层。

### 1.1 点「AI 再标注」，复核标记退回 0 ❗

**现象**：审核页上点「AI 再标注」，五个标注字段都更新了，
`label_review_flag` 却写成 **0（未标注）**。这条评论在审核页上又变成
"从没标过"，点多少次都一样，而接口返回一切正常。

**根因**（在引擎里，属于设计如此，不改它）：
`label_sync(row, write=True)` 内部调 `repository.update_one(record, result)`，
`review_flag` 走默认值 0。只有**队列那条路**（`worker/consumer.py`）
会先算好 flag 再写。

**改法**：smc 这边自己算 flag 再显式传进去，规则**借引擎自己的**
`LabelingWorker._review_flag`（只依赖 `cfg`，拿个空壳绑上去调），
而不是在这边把阈值判断重写一遍——两边各写一份迟早漂。

### 1.2 自检 10 项全绿，启动却报「失败池连不上 Redis」

**根因**：任务队列和失败池是**两个**组件。队列切成 memory 不代表
失败池也是；引擎对 redis 后端的失败池会**启动期直接炸**。
自检绿灯 → 启动报错，是最难看的一种，用户会以为是偶发。

**改法**：自检补上「失败池」一项（9 项 → 10 项），
关掉 / memory / redis 三种情况分别给出人话说明。
已验证：把 Redis 停掉，这一项确实变红并给出 `redis-cli ping` 的排查命令。

### 1.3 `SMC_LABELING_ARK_API_KEY` 是个**安静失效**的假名字

自检的提示里写着"或设环境变量 `SMC_LABELING_ARK_API_KEY`"，
但这个名字会被解析成 `labeling.ark_api_key`（真正的路径是
`labeling.ark.api_key`），然后被静默跳过——变量设了、日志一声不吭、
程序照跑，只是 Key 压根没进配置。

**改法**：
- 提示改成正确的 `SMC_LABELING__ARK__API_KEY`（嵌套超过一层用双下划线）
- 更要紧的：**任何**匹配不上配置项的 `SMC_` 变量现在都会打 WARNING，
  并且会去真实配置树里找出你本来想写的那个名字：

  ```
  WARNING 环境变量 SMC_LABELING_ARK_API_KEY 没有对应的配置项
          （解析成 labeling.ark_api_key），已忽略。
          你要找的应该是 SMC_LABELING__ARK__API_KEY
  ```

### 端到端验证脚本（新增）

`backend/verify_labeling.py`：塞一条评论 → 走完整标注流程 → 检查
五个字段和复核标记，跑完删掉测试数据。

模型调用是**打桩**的（不花钱、不依赖外网），但引擎代码一行没改：
只在运行时替换 `ArkClient.complete`，走的还是引擎自己的
prompt / parser / taxonomy 校验 / repository 写回。

```bash
cd backend
SMC_LABELING__ARK__API_KEY=你的key SMC_LABELING__ARK__MODEL=你的模型 \
    python verify_labeling.py
```

实跑输出（已验证通过）：

```
sentiment_label      = '负向'
sentiment_score      = -1
dimension_tags       = '[{"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光", "sentiment": 1}, ...]'
entity_tags          = '[{"type": "景区地名", "value": "盘山"}]'
keyword_tags         = '["红叶"]'
label_review_flag    = 4
```

顺带确认了三件**不是 bug** 的事，免得你看到觉得不对：

- `sentiment_score` 是 **1 / 0 / -1** 三档整数，不是小数。
  引擎自己的 SQL 就是这么用的（`SUM(sentiment_score = 1) AS positive`），
  表里 `int(11)` 是对的。
- `sentiment_label` 的规范写法是「负向」不是「负面」，引擎会归一。
- 模型给的关键字如果**没在评论原文里出现过**会被丢掉
  （`keyword_must_appear_in_content`），这是防模型编词的，是对的。

---

## 2. 「需要登录」暂停：没配过账号时不再空等 30 分钟

**压根没配过账号 ≠ 账号需要重新登录**。前者等多久都没用——
没人会去"恢复"一个从来不存在的账号，而任务会白白卡 30 分钟，
日志上还写着"等待人工处理"，看着像在干活。

现在会先查这个平台有没有配过账号，一个都没有就直接报错跳过：

```
douyin 一个账号都没配，等待没有意义——去账号管理里添加并登录一个账号再跑
```

---

## 3. 采集水印现在能在页面上改了

之前只能改 `config.yaml`。「系统设置 → 采集参数」底部新增一组：

- **启用水印**（总开关）
- **关键字水印有效期**：默认 14400 秒，页面上直接显示「4 小时」
- **作品水印有效期**：默认 259200 秒，显示「3 天」

作品水印**不分景区**：同一条作品被两个景区的关键字搜到也只采一次——
这是有意的，页面上写明了。

---

## 4. 测试

`413 passed`，全绿。新增 `tests/test_labeling_relabel_flag.py`（5 条）。

其中一条是"低置信度要写 6"——**就是它抓出了 1.1 那个改法里的二次 bug**：
我第一版把引擎的类名写成了 `LabelConsumer`（真名 `LabelingWorker`），
`ImportError` 被 except 吞掉，所有评论都退回兜底值 4，接口一切正常。
现在借不到规则会打 WARNING 并指明要改哪个文件。

还修了一个**测试自身**的问题：`test_ui_smoke.py` 里 4 个用例依赖全局
状态，第二次跑必红 4 个（代码一个字没改）。这种假红比不测还糟——
查半天发现是残留，下次就不敢信测试结果了。现在每轮跑之前会清空冒烟库，
连跑两次都是 12 passed。

---

## 5. 安全（和之前几版一样，再提醒一次）

- 包里**不含** `config/config.yaml` 和 `data/`，不会覆盖你本地的
  密钥和浏览器登录态。
- `data/labeling/engine.runtime.yaml` 是运行时生成的，里面有明文
  API Key 和库密码，已 chmod 0600，也不进包。
- `config.yaml` 里那四组明文凭据建议挪到环境变量：
  `SMC_MYSQL_PASSWORD` / `SMC_REDIS_PASSWORD` / `SMC_PROXY_SECRET_KEY`
  （快代理那个 key 尤其建议轮换，它是会产生费用的）。
- 实时看板会显示已登录的平台页面，服务本身没有鉴权，**别暴露到公网**。
