# 场景写法大全

下面每个场景都能直接用在 `engine.js` 上（场景 id、时间按需改）。尺寸按 1920×1080 写；
竖屏版式见 `styles.md`。所有动画都从 `lt`（场景内秒数）算出来，不要用 CSS 动画。

可用工具：`clamp eo eio eback lerp rnd pulse $ $$ css rgba`，全局画布 `fx`（每帧自动清空，
适合画连线、粒子、光环、数据包），`VIDEO` 配置。

目录
1. 开场揭幕（光线 → Logo → 字符解码）
2. 痛点：散落 + 气泡 + 告警文字
3. 汇聚：元素绕核心轨道 + 数据流
4. 功能页：左文案 + 右 3D 示意面板（仪表盘）
5. 打字 + 预览（配置 / 命令 / 搜索框）
6. 状态网格 + 队列 + 通知弹窗
7. 卡片流穿过扫描线（AI 处理、自动标注）
8. 节点拓扑 + 数据包 + 故障接管
9. 快剪（每 1.5 秒一切）
10. 收尾（Logo + 口号 + 徽章 + 声明）
11. 接入真实截图（用户提供时）

---

## 1. 开场揭幕

```html
<div class="scene" id="intro">
  <img id="logo" src="logo.png" class="abs" style="left:50%;top:230px;height:170px">
  <div id="title" class="abs center mono" style="top:440px;font-size:136px;font-weight:800;letter-spacing:10px"></div>
  <div class="abs center sub" style="top:630px;font-size:46px;letter-spacing:18px" data-in="2.9">一句话定位</div>
</div>
```
```js
const TITLE = 'ProductName', GLYPH = 'ABCDEFGHJKLMNPQRSTUVWXYZ0123456789#$%&@';
scene('intro', 0, 6, lt => {
  const W = VIDEO.width, w = eo(lt / .9) * W * .78, y = lerp(540, 420, eio((lt - 1) / .8));
  fx.globalAlpha = lt < 1.8 ? 1 : 1 - clamp((lt - 1.8) / .6);
  fx.fillStyle = css('--accent'); fx.shadowColor = css('--accent'); fx.shadowBlur = 18;
  fx.fillRect(W / 2 - w / 2, y, w, 2); fx.shadowBlur = 0; fx.globalAlpha = 1;
  const lp = eo((lt - 1.3) / .8), logo = $('#logo');          // 深色主题把彩色 Logo 变白发光
  logo.style.opacity = lp; logo.style.transform = `translateX(-50%) scale(${.7 + .3 * lp})`;
  logo.style.filter = `brightness(0) invert(1) drop-shadow(0 0 20px ${css('--accent')}) blur(${(1 - lp) * 10}px)`;
  const n = Math.floor(clamp((lt - 2) / 1) * TITLE.length), f = Math.floor(lt * 30);   // 字符解码
  let s = ''; if (lt >= 1.85) for (let i = 0; i < TITLE.length; i++) s += i < n ? TITLE[i] : GLYPH[Math.floor(rnd(f * 31 + i) * GLYPH.length)];
  $('#title').textContent = s;
});
```
没有 Logo 就去掉 `<img>`，或者用一个 SVG 几何图形代替。旋转光环：SVG 里几个 `stroke-dasharray` 圆，
`setAttribute('transform', \`rotate(${lt * 30})\`)`。

## 2. 痛点：散落 + 气泡 + 告警

```html
<div class="scene" id="pain">
  <div id="bubbles"></div>
  <div class="abs center hero" style="top:330px" data-in=".3" data-out="5.1">七个平台</div>
  <div class="abs center sub" style="top:500px;font-size:46px;letter-spacing:8px" data-in="1.3" data-out="5.1">各自翻 · 各自导 · 口径不一</div>
  <div class="abs center" id="warn" style="top:590px;font-size:46px;color:var(--bad)" data-in="2.6" data-out="5.1">人工处理跟不上</div>
</div>
```
```js
const BUB = ['这个好用吗？', '又出错了……', '找不到入口', '太慢了'];
$('#bubbles').innerHTML = BUB.map((b, i) => `<div class="card abs" id="b${i}" style="padding:12px 20px;font-size:22px;white-space:nowrap">${b}</div>`).join('');
scene('pain', 6, 12, lt => {
  BUB.forEach((_, i) => { const el = $('#b' + i);
    el.style.left = (120 + rnd(i + 40) * 1600) + 'px'; el.style.top = (1000 - rnd(i + 80) * 800 - lt * (30 + rnd(i) * 40)) + 'px';
    el.style.opacity = clamp((lt - .4 - i * .18) / .4) * .55 * (1 - clamp((lt - 5) / .6)); });
  if (lt > 2.6 && lt < 3.4) $('#warn').style.transform += ` translateX(${(rnd(Math.floor(lt * 30)) - .5) * 14}px)`; // 故障抖动
});
```

## 3. 汇聚：轨道 + 数据流

一组“平台 / 来源 / 模块”图块先散落抖动，再飞到椭圆轨道上绕中心旋转，最后吸进核心。
跨两个场景的图层单独注册成一个场景（`scene('tiles', 6, 18, fn, {fade:false})`），按全局时间算位置。

```js
const ITEMS = ['A', 'B', 'C', 'D', 'E', 'F'];       // 每个是 <div class="tile" id="t0">…</div>
const SCAT = ITEMS.map((_, i) => [200 + rnd(i) * 1500, 150 + rnd(i + 9) * 780]);
function tilePos(i, lt) {                               // lt 从图层开始算；第 6 秒开始汇聚
  const ang = (-90 + i * 360 / ITEMS.length + Math.max(0, lt - 6) * 9) * Math.PI / 180;
  const ox = 960 + Math.cos(ang) * 470, oy = 560 + Math.sin(ang) * 300, m = eio((lt - 6) / 1.6);
  let x = lerp(SCAT[i][0] + Math.sin(lt * 2.1 + i) * 14, ox, m), y = lerp(SCAT[i][1] + Math.cos(lt * 1.7 + i) * 12, oy, m);
  const suck = eio((lt - 11.1) / .7); return { x: lerp(x, 960, suck), y: lerp(y, 540, suck), chaos: 1 - m, suck };
}
// 数据流（在汇聚场景的 fn 里画）：虚线 + 流动光点
function streams(lt, toX, toY) {
  for (let i = 0; i < ITEMS.length; i++) {
    const { x, y } = tilePos(i, lt + 6);
    fx.strokeStyle = rgba(css('--accent'), .6); fx.setLineDash([8, 14]); fx.lineDashOffset = -lt * 90;
    fx.beginPath(); fx.moveTo(x, y); fx.lineTo(toX, toY); fx.stroke(); fx.setLineDash([]);
    for (let k = 0; k < 3; k++) { const u = (lt * .7 + i * .13 + k / 3) % 1;
      fx.fillStyle = '#fff'; fx.shadowColor = css('--accent'); fx.shadowBlur = 14;
      fx.beginPath(); fx.arc(lerp(x, toX, u), lerp(y, toY, u), 4, 0, 7); fx.fill(); fx.shadowBlur = 0; }
  }
}
```
核心：一个圆形 div（径向渐变 + 外发光），`transform: scale(eback(...))` 弹出；
收尾时 `scale(1 + boom * 6)` + 透明度归零，正好接下一个场景的闪光转场。外圈扩散光环用 `fx.arc` 半径随 `(lt*.5+k/3)%1` 增长。

## 4. 功能页：左文案 + 右 3D 面板

```html
<div class="scene" id="f1">
  <div class="col">
    <div class="kick" data-in=".3" data-dx="-40">01 / OVERVIEW</div>
    <div class="ttl" data-in=".5">一屏总览<br>全局在握</div>
    <div class="sub" data-in=".9">一句话说明价值</div>
    <ul class="pts"><li data-in="1.4" data-dx="-30">要点一</li><li data-in="1.7" data-dx="-30">要点二</li></ul>
  </div>
  <div class="panel" id="dash" style="left:700px;top:170px;width:1120px;height:740px;transform-origin:30% 50%">
    <span class="demo-tag">示意数据</span>
    <div class="card stat" id="k0" style="left:24px;top:56px;width:250px;height:128px;padding:20px 22px">
      <div class="lbl">今日新增</div><div class="v" data-count="58312" data-at="1.2"></div></div>
    <!-- 折线图：<path id="line" pathLength="1" stroke-dasharray="1">，用 strokeDashoffset 画出来 -->
    <!-- 条形：<div class="track"><div class="fill" data-bar=".92" data-at="1.6"></div></div> -->
  </div>
</div>
```
```js
scene('f1', 18, 26, lt => {
  const p = eo(lt / 1.3), d = $('#dash');
  d.style.transform = `perspective(2000px) translateX(${(1 - p) * 260}px) rotateY(${lerp(-38, -12, p) + lerp(0, 6, eio((lt - 4) / 3.5))}deg) rotateX(${lerp(14, 5, p)}deg)`;
  d.style.opacity = clamp(lt / .5);
  const lp = eio((lt - 1.4) / 1.6); $('#line').style.strokeDashoffset = 1 - lp;
  const hk = pulse(lt, 3.4, .7);                     // 讲到哪亮哪
  $('#k0').style.boxShadow = `0 0 ${40 * hk}px ${rgba(css('--accent'), .8 * hk)}`;
});
```
平滑折线：点列 `pts`，`d += \` C${cx},${y0} ${cx},${y1} ${x1},${y1}\``（cx 取中点）；
面积填充同一条路径加 `L右下 L左下 Z`，渐变透明。光点跟随：`path.getPointAtLength(len * lp)`。
实时动态列表：容器 `overflow:hidden` + 遮罩渐隐，内层 `translateY(-max(0, lt-3)*26px)` 慢慢上滚。

## 5. 打字 + 预览

```html
<div class="card" style="left:30px;top:192px;width:560px;height:84px;padding:18px 26px;font:40px var(--mono);color:var(--accent)">
  <span data-type="2.5" data-text="0 3 * * *" data-cps="9"></span></div>
<div class="mono" style="font-size:22px">
  <div data-in="3.7" data-dx="-20">→ 2026-11-15 03:00:00</div>
  <div data-in="3.9" data-dx="-20">→ 2026-11-16 03:00:00</div></div>
```
选项卡依次点亮：`[...tabs.children].forEach((el, i) => el.classList.toggle('on', i === idx))`，
`idx` 按时间分段（0.6 / 1.1 / 1.6 / 2.1 秒——落在节拍上）。搜索框、聊天输入、命令行都是同一招。
24 小时钟：SVG 刻度 + 指针 `rotate((lt-3)*260)`，经过目标刻度时 `pulse` 放大高亮点。

## 6. 状态网格 + 队列 + 通知

- 网格：N 张 `.card`，每张头像色块 + 名称 + 状态 chip + 配额条。
- 轮换高亮：`cur = ORDER[Math.floor((lt - 1.2) / .5) % ORDER.length]`（每拍换一个），当前卡片描边发光。
- 状态变化：到某个时间点把某张卡改成“冷却 / 失效”（换 chip 文本和颜色）。
- 队列：一排胶囊，每 0.8 秒整体左移一格，越过分界线的变成“运行中”：
  `pos = i - (Math.floor(k) + eio((k % 1) / .4))`，`k = clamp((lt - 2.4) / .8, 0, 6)`。
- 通知弹窗：右上角 `.card`，`translateX((1 - eback((lt-4)/.6)) * 600px)` 滑入，几秒后淡出。

## 7. 卡片流穿过扫描线

```js
// 每张卡 <div class="card abs" id="c${i}">原文<div id="cg${i}">标签们</div></div>，高 122、间距 140
scene('ai', 42, 52, lt => {
  const SCAN = 540;
  CMTS.forEach((_, i) => {
    const el = $('#c' + i), y = 1100 + i * 140 - (lt + 1.2) * 120;
    el.style.top = y + 'px';
    const crossed = clamp((SCAN - (y + 61)) / 60);         // 越过扫描线 → 标签浮现
    $('#cg' + i).style.opacity = crossed;
    el.style.boxShadow = crossed > 0 && crossed < 1 ? `0 0 30px ${rgba(css('--accent'), .5)}` : 'none';
  });
});
```
流容器加 `-webkit-mask-image: linear-gradient(transparent, #000 18%, #000 82%, transparent)` 上下渐隐。
旁边配：环形图（三个 `<circle pathLength="100">`，`stroke-dasharray` 按比例增长、`stroke-dashoffset` 累加）、
热词 chip 依次 `data-pop`、流程步骤依次点亮（采集 → 处理 → 复核 → 回写）。

## 8. 节点拓扑 + 数据包 + 故障接管

节点用 DOM（`.card` 绝对定位，中心坐标存在常量里），连线和数据包画在 `fx` 上：
```js
const edge = (a, b, color, alpha) => { fx.strokeStyle = rgba(color, alpha); fx.setLineDash([6, 10]); fx.lineDashOffset = -lt * 60;
  fx.beginPath(); fx.moveTo(...a); fx.lineTo(...b); fx.stroke(); fx.setLineDash([]); };
const packet = (a, b, u, color) => { fx.fillStyle = color; fx.shadowColor = color; fx.shadowBlur = 16;
  fx.beginPath(); fx.arc(lerp(a[0], b[0], u), lerp(a[1], b[1], u), 6, 0, 7); fx.fill(); fx.shadowBlur = 0; };
// u = (lt * .6 + k / 3) % 1 循环流动；某节点在 lt=4 掉线：描边变红、徽章改 OFFLINE、抖动 0.6 秒、
// 它的数据包停掉，改一条红色数据包从它流向接手节点，再弹出说明标签。
```
弯曲连线用 `quadraticCurveTo`，数据包位置用二次贝塞尔公式算。主节点加一圈扩散椭圆光环表示 Leader。

## 9. 快剪

每 1.5 秒一个独立场景（`scene('m1', 60, 61.5, ...)`），统一版式：左边大标题，右边示意面板。
快剪场景关掉尾部淡出（`{fade:false}`），靠转场闪光硬切更有节奏。面板内容要“一眼看懂”：
- 实时日志：等宽字逐行出现，`[INFO]/[WARN]` 上色，每 0.085 秒一行。
- 扫码登录：SVG 随机点阵 + 三个定位角当二维码，扫描线上下走，0.95 秒时变“登录成功”。
- 表格导出：行依次出现 + 底部“⬇ xxx.csv ✓”。
- 通知堆叠：卡片依次从右侧弹入。

## 10. 收尾

```html
<div class="scene" id="outro">
  <img src="logo.png" class="abs" style="left:50%;top:200px;height:170px;transform:translateX(-50%)">
  <div class="abs center mono" style="top:396px;font-size:110px;font-weight:800;letter-spacing:10px" data-in=".5">ProductName</div>
  <div class="abs center" style="top:560px;font-size:50px;letter-spacing:14px" data-in="1.2">让每一条声音，都被听见</div>
  <div class="abs center" style="top:690px" data-in="2.0"><span class="badge">一键部署</span><span class="badge">开源</span></div>
  <div class="abs center lbl" style="top:990px" data-in="2.6">画面为示意演示</div>
</div>
```
外圈用 `fx.ellipse` 画几层缓慢扩散的光环。最后 1 秒整体淡出由引擎处理（`VIDEO.endFade`）。

## 11. 接入真实截图（只在用户提供时）

```html
<div class="panel" id="frame" style="left:520px;top:140px;width:1320px;height:790px">
  <div class="browser-bar"><i style="background:#f87171"></i><i style="background:#fbbf24"></i><i style="background:#34d399"></i></div>
  <div class="shot"><img id="shot" src="shots/home.png"></div>
</div>
```
推镜头（Ken Burns）：`s = lerp(1, 1.6, eio((lt-2)/4))`，目标点 `(cx, cy)` 是图片上的百分比坐标，
`tx = clamp(W/2 - cx*W*s, W - W*s, 0)`，`img.style.transform = \`translate(${tx}px,${ty}px) scale(${s})\``。
高亮框用同一套坐标换算后绝对定位在 `.shot` 里。截图里的个人信息、真实数据要先确认用户同意公开。
