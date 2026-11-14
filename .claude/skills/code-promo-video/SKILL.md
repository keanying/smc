---
name: code-promo-video
description: 用代码做宣传片 / 产品介绍视频 / 功能演示短片，导出带配乐的 MP4：HTML 动画画面逐帧渲染 + 代码合成配乐，转场卡在节拍上。用户只需一句话（“给这个项目做个宣传片”“给我的奶茶店做 30 秒竖屏视频”“产品发布会开场动画”“把这个仓库做成介绍视频”），就能自动提炼卖点、写分镜、做画面、配乐、出片。只要用户想要视频、短片、宣传片、promo、介绍动画、motion graphics、发布会片头、抖音/视频号竖屏短片、产品演示动画，就使用这个技能——即使他们没说“用代码做”，也没提到 HTML 或 ffmpeg。
---

# 代码宣传片（code-promo-video）

用一页 HTML 当画布，`render(t)` 把画面摆到第 t 秒，Playwright 逐帧截图交给 ffmpeg；
配乐由 `scripts/music.py` 按**同一份转场时间点**合成，所以画面切换和鼓点天然对齐。
全部素材自产：不需要剪辑软件，不用任何有版权的音乐或图片。

这条路线的优势是**每一帧都可控、可重现**：文字清晰、排版精确、改一句文案重渲染即可。
它适合信息型视频（产品、功能、数据、流程、品牌口号），不适合实拍感或写实人物动画。

## 工作流程

### 1. 定需求（尽量不打扰用户）

从用户一句话里提取下面几项；没说的用默认值，并在交付时说明你用了什么。
只有真正影响结果、又猜不出来的才问（比如“给我们公司做个视频”却不知道公司是做什么的）。

| 项 | 默认 | 说明 |
| :--- | :--- | :--- |
| 主题素材 | — | 仓库 / 文档 / 网址 / 一段文字。仓库就去读 README、docs、路由、页面、CHANGELOG 提炼卖点 |
| 时长 | 60 秒 | 15–30 秒短视频，45–90 秒产品介绍；超过 90 秒信息密度会塌 |
| 画幅 | 16:9（1920×1080） | 竖屏 9:16 用 1080×1920，方形 1080×1080 |
| 风格 | 按主题推断 | 主题 `tech / fresh / business / guochao`，见 `references/styles.md` |
| 配乐 | 有 | 情绪 `tech / bright / warm / epic / corporate / none`，跟风格配对 |
| 画面 | 示意界面 | 默认用代码画的示意 UI。用户提供截图才用真实画面 |

### 2. 写分镜（先想清楚再写代码）

在工作目录写 `storyboard.md`：一张表，每行一个场景——起止秒、画面、文案、强调点。规则：

- **一个场景只讲一件事**，5–10 秒；快剪段可以 1–2 秒一切。
- **转场全部落在节拍上**：120 BPM 时一拍 0.5 秒、一小节 2 秒，场景边界尽量放在偶数秒。
  其它 BPM 下拍长 = 60/BPM。
- 挑 3–5 个最重要的转场放进 `big`（开场亮相、主体出现、高潮、收尾），配乐会在这里放冲击音和前置上扬。
- 经典结构：**开场亮相 → 痛点 / 场景 → 解决方案汇聚 → 3–5 个核心能力 → 快剪补充 → 收尾口号**。
- 文案要短：大标题 ≤ 8 字一行，副标题一句话，要点 ≤ 14 字。观众只有几秒钟读。
- 数字要么来自素材，要么明确是示意；面板角上放 `示意` 标签。

用户要求先看分镜时，停在这里给他看；没要求就直接往下做，最后一起交付。

### 3. 做画面

1. 新建工作目录，把 `assets/template.html`、`assets/engine.js`、`assets/themes.css` 复制进去，
   模板改名为 `<项目名>_promo.html`（输出文件名跟它走）。
2. 改 `window.VIDEO`：尺寸、`duration`、`bpm`、`cuts`、`big`、`mood`、`theme`。
   这是画面和配乐唯一的配置来源，`cuts` 必须和场景边界一致。
3. 每个场景一个 `<div class="scene" id="...">`，再用 `scene(id, 开始, 结束, fn)` 注册。
   简单的入场用 data 属性（`data-in` 淡入上移、`data-dx` 横滑、`data-pop` 弹出、`data-type` 打字、
   `data-count` 数字滚动、`data-bar` 进度条），复杂的在 `fn(lt)` 里逐帧算。
   带 `data-in` 的元素 transform 由引擎接管：要居中（`translateX(-50%)`）等基础变换，写进 `data-tf`，写在 style 里会被覆盖。
   竖屏用 `.vcol`（文字区）和 `.vpanel`（面板）代替 `.col`，版式细节见 `references/styles.md`。
4. 场景写法照抄 `references/scene-patterns.md`：开场揭幕、混乱→汇聚、3D 仪表盘、
   终端日志、节点拓扑、卡片流 + 扫描线、快剪、收尾。先读它再写，能省很多时间。

**确定性是硬约束**：同一个 t 必须得到同一帧，因为逐帧截图之间没有真实时间流逝。
所以不要用 CSS transition / animation、`setTimeout`、`Math.random()`、`Date.now()`；
随机用 `rnd(i)`，动画全部从 `lt` 算。用了就会出现“预览正常、成片卡顿或静止”。

### 4. 检查静帧（必须做）

```bash
python <skill>/scripts/render.py work/xxx_promo.html --stills 1.5 4 9 15 22 ...
```

每个场景挑 1–2 个时间点（入场动画完成后），打开 `out/stills/contact.png` **亲眼看**：
文字重叠、溢出面板、被裁切、对比度不够、元素还没出现、跨场景残留——这些问题在成片里只会更明显。
修完再出一轮静帧，直到干净。这一步最能决定成品质量。

### 5. 出片

```bash
bash <skill>/scripts/build.sh work/xxx_promo.html            # 正式版
bash <skill>/scripts/build.sh work/xxx_promo.html --preview  # 半分辨率小样，约快 3 倍
```

`build.sh` 依次：逐帧渲染 → 读 `out/video.json` 合成配乐 → 合成并把响度归一到 -14 LUFS。
1080p 30fps 每帧约 0.1–0.3 秒（60 秒片约 3–10 分钟，视机器和画面复杂度），放后台跑，期间可以做别的。
渲染完抽几帧（`ffmpeg -ss T -i out.mp4 -frames:v 1 f.png`）确认，再交付。

### 6. 交付

- 把 `out/<名字>.mp4` 发给用户（有发文件的工具就用它），并给出：时长、画幅、分镜摘要、
  用了哪些默认值、配乐情绪；说明配乐是你看频谱检查的，没有真正“听”，请用户自己听一下。
- 工程文件（html / storyboard / 两个 js·css）保留在工作目录，方便用户改文案后重出。
- 输出目录 `out/` 很大，不要提交进 git。

## 风格与边界

- **不冒充真实品牌**：不画别家的 Logo、界面或商标。第三方平台用文字徽标（“携”“抖”色块即可）；
  用户自己的 Logo / 素材可以用。
- **配乐只用 `music.py` 合成**。用户提供了音乐文件，就用 ffmpeg 直接合成它，并在交付时提醒版权由用户负责。
- 素材里有许可限制（比如“仅限非商业”）时，片尾加一行小字声明。
- 中文字体缺失会变方块：先按 `references/setup.md` 检查环境（字体、Chromium、ffmpeg、Python 包）。

## 文件

- `assets/template.html`：起步模板（三个示例场景）
- `assets/engine.js`：引擎——场景调度、声明式入场、转场闪光、四种主题背景、`fx` 画布、工具函数
- `assets/themes.css`：四套主题 token 和通用组件（`.col .kick .ttl .sub .pts .panel .card .chip .badge .track .fill`）
- `scripts/render.py`：静帧 / 全片渲染，自动读取页面尺寸和帧率
- `scripts/music.py`：配乐合成，五种情绪
- `scripts/build.sh`：一键出片
- `references/scene-patterns.md`：场景写法大全（带代码）
- `references/styles.md`：主题 × 情绪搭配、竖屏版式、文案节奏
- `references/setup.md`：环境依赖和安装
