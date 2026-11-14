/*
 * code-promo-video 动画引擎：逐帧确定性渲染。
 *
 * 页面里先声明 window.VIDEO（尺寸、时长、节拍、转场点），再用 scene() 注册场景，
 * 最后调用 boot()。render(t) 会把整个画面摆到第 t 秒——同一个 t 永远得到同一帧，
 * 所以不要用 CSS transition / animation / setTimeout / Math.random，一切都从 t 算出来。
 *
 * 用浏览器直接打开页面会循环实时预览；被 render.py 驱动时（navigator.webdriver）不自动播放。
 */
(function () {
  const V = window.VIDEO = Object.assign({
    width: 1920, height: 1080, fps: 30, duration: 60, bpm: 120,
    cuts: [], big: [], mood: 'tech', theme: 'tech', transition: 'flash', endFade: 1,
  }, window.VIDEO || {});

  // ---------- 数学工具 ----------
  const clamp = (x, a = 0, b = 1) => x < a ? a : x > b ? b : x;
  const eo = x => 1 - Math.pow(1 - clamp(x), 3);                       // ease-out
  const eio = x => { x = clamp(x); return x < .5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2 };
  const eback = x => { x = clamp(x); const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.pow(x - 1, 3) + c1 * Math.pow(x - 1, 2) };
  const lerp = (a, b, p) => a + (b - a) * p;
  const rnd = i => { const x = Math.sin(i * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x) }; // 确定性"随机"
  const pulse = (lt, at, width = .6) => Math.exp(-Math.pow((lt - at) / width, 2));                  // 高亮脉冲 0..1
  const $ = s => document.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  Object.assign(window, { clamp, eo, eio, eback, lerp, rnd, pulse, $, $$ });

  // ---------- 场景注册 ----------
  const SCENES = [];
  /** scene(id, 开始秒, 结束秒, fn(lt, t), opts)；opts.fade=false 关掉尾部淡出，opts.keepAlive 用于跨场景的图层 */
  window.scene = (id, a, b, fn = () => {}, opts = {}) => SCENES.push({ id, a, b, fn, opts });

  // ---------- 通用入场动画（声明式） ----------
  // data-in="秒"        淡入 + 上移 + 去模糊；data-dx="-40" 改为横向滑入；data-out="秒" 到点淡出
  // data-pop            配合 data-in，改为弹出缩放
  // data-tf="translateX(-50%)"  基础变换：带 data-in 的元素 transform 由引擎接管，居中等变换要写在这里，写在 style 里会被覆盖
  // data-type="秒" data-text="..." data-cps="18"   打字机
  // data-count="12860" data-at="秒" data-dur="1.4" data-fmt="int|1|%"   数字滚动
  // data-bar="0.8" data-at="秒"   宽度增长到 80%
  function animIn(root, lt) {
    $$('[data-in]', root).forEach(el => {
      const a = +el.dataset.in, dur = +(el.dataset.d || .55);
      if (el.hasAttribute('data-pop')) {
        const p = eback((lt - a) / dur);
        el.style.opacity = clamp((lt - a) / (dur * .5));
        el.style.transform = `${el.dataset.tf || ''} scale(${Math.max(0, p)})`;
      } else {
        const p = eo((lt - a) / dur), dx = +(el.dataset.dx || 0), dy = el.dataset.dy != null ? +el.dataset.dy : (dx ? 0 : 30);
        el.style.opacity = p;
        el.style.transform = `${el.dataset.tf || ''} translate(${(1 - p) * dx}px,${(1 - p) * dy}px)`;
        el.style.filter = p < 1 ? `blur(${(1 - p) * 8}px)` : 'none';
      }
      if (el.dataset.out) el.style.opacity *= 1 - clamp((lt - +el.dataset.out) / .4);
    });
    $$('[data-type]', root).forEach(el => {
      const a = +el.dataset.type, txt = el.dataset.text, cps = +(el.dataset.cps || 18);
      const n = Math.floor(clamp((lt - a) * cps, 0, txt.length));
      const caret = el.dataset.caret !== '0' && lt > a - .6 && (n < txt.length || Math.floor(lt * 2.4) % 2 === 0);
      el.textContent = txt.slice(0, n) + (caret ? '▌' : '');
    });
    $$('[data-count]', root).forEach(el => {
      const v = +el.dataset.count * eo((lt - +(el.dataset.at || 0)) / +(el.dataset.dur || 1.4));
      const f = el.dataset.fmt || 'int';
      el.textContent = f === 'int' ? Math.round(v).toLocaleString('en-US') : f === '%' ? Math.round(v) + '%' : v.toFixed(+f);
    });
    $$('[data-bar]', root).forEach(el => {
      el.style.width = (+el.dataset.bar * eo((lt - +(el.dataset.at || 0)) / .9) * 100) + '%';
    });
  }

  // ---------- 背景（按主题） ----------
  let bg, fx, W, H, PTS;
  const BG = {
    // 科技：深色 + 透视网格 + 粒子网络
    tech(t, c) {
      c.fillStyle = css('--bg'); c.fillRect(0, 0, W, H);
      glow(c, W * .2 + Math.sin(t * .2) * W * .1, H * .18, W * .5, css('--glow1'));
      glow(c, W * .8, H * .85 + Math.cos(t * .17) * H * .1, W * .5, css('--glow2'));
      const hz = H * .7, vx = W / 2;
      c.lineWidth = 1;
      for (let i = -24; i <= 24; i++) { c.strokeStyle = rgba(css('--accent2'), .16 - Math.abs(i) * .004); c.beginPath(); c.moveTo(vx + i * W / 64, hz); c.lineTo(vx + i * W / 7.4, H); c.stroke(); }
      const off = (t * .6) % 1;
      for (let k = 0; k < 12; k++) { const z = (k + off) / 12, y = hz + Math.pow(z, 2.2) * (H - hz); c.strokeStyle = rgba(css('--accent'), .03 + z * .14); c.beginPath(); c.moveTo(0, y); c.lineTo(W, y); c.stroke(); }
      network(c, t, 'rgba(148,163,255,', 'rgba(186,230,253,.55)');
    },
    // 清新：浅色 + 柔和漂浮光斑
    fresh(t, c) {
      c.fillStyle = css('--bg'); c.fillRect(0, 0, W, H);
      const cols = [css('--glow1'), css('--glow2'), css('--glow3')];
      for (let i = 0; i < 6; i++) {
        const x = W * (.1 + rnd(i) * .8) + Math.sin(t * .25 + i) * W * .06, y = H * (.1 + rnd(i + 9) * .8) + Math.cos(t * .21 + i * 2) * H * .05;
        glow(c, x, y, Math.max(W, H) * (.25 + rnd(i + 3) * .2), cols[i % 3]);
      }
      for (let i = 0; i < 40; i++) {
        const x = (rnd(i + 50) * W + t * 12 * (rnd(i) - .3)) % W, y = (rnd(i + 80) * H - t * 18 * (.4 + rnd(i + 2))) % H;
        c.fillStyle = 'rgba(255,255,255,.7)'; c.beginPath(); c.arc((x + W) % W, (y + H) % H, 2 + rnd(i) * 4, 0, 7); c.fill();
      }
    },
    // 商务：干净浅灰 + 细网格 + 缓慢斜光
    business(t, c) {
      c.fillStyle = css('--bg'); c.fillRect(0, 0, W, H);
      c.strokeStyle = 'rgba(15,23,42,.05)'; c.lineWidth = 1;
      const s = Math.round(Math.min(W, H) / 18);
      for (let x = (t * 6) % s; x < W; x += s) { c.beginPath(); c.moveTo(x, 0); c.lineTo(x, H); c.stroke(); }
      for (let y = 0; y < H; y += s) { c.beginPath(); c.moveTo(0, y); c.lineTo(W, y); c.stroke(); }
      glow(c, W * .85, H * .1, W * .45, css('--glow1'));
      glow(c, W * .1, H * .95, W * .4, css('--glow2'));
    },
    // 国潮：暖红底 + 云纹圆环 + 金色微粒
    guochao(t, c) {
      c.fillStyle = css('--bg'); c.fillRect(0, 0, W, H);
      glow(c, W * .5, H * .45, Math.max(W, H) * .6, css('--glow1'));
      c.lineWidth = 2;
      for (let i = 0; i < 7; i++) {
        const cx = W * rnd(i + 20), cy = H * rnd(i + 30), r = Math.min(W, H) * (.08 + rnd(i) * .12);
        for (let k = 0; k < 3; k++) { c.strokeStyle = rgba(css('--accent'), .10 - k * .025); c.beginPath(); c.arc(cx, cy, r * (1 + k * .35) + Math.sin(t + i) * 4, Math.PI * (1 + rnd(i)), Math.PI * (2.2 + rnd(i))); c.stroke(); }
      }
      for (let i = 0; i < 60; i++) {
        const x = rnd(i + 3) * W + Math.sin(t * .5 + i) * 20, y = (rnd(i + 7) * H - t * 10 * (.5 + rnd(i))) % H;
        c.fillStyle = rgba(css('--accent'), .35 + rnd(i) * .4); c.beginPath(); c.arc(x, (y + H) % H, 1.2 + rnd(i + 1) * 2, 0, 7); c.fill();
      }
    },
  };
  function glow(c, x, y, r, color) { const g = c.createRadialGradient(x, y, 0, x, y, r); g.addColorStop(0, color); g.addColorStop(1, transparentOf(color)); c.fillStyle = g; c.fillRect(0, 0, W, H) }
  function network(c, t, line, dot) {
    const ps = PTS.map(p => ({ x: ((p.x + p.vx * t) % W + W) % W, y: ((p.y + p.vy * t) % H + H) % H, r: p.r }));
    for (let i = 0; i < ps.length; i++) for (let j = i + 1; j < ps.length; j++) {
      const dx = ps[i].x - ps[j].x, dy = ps[i].y - ps[j].y, d = dx * dx + dy * dy;
      if (d < 19600) { c.strokeStyle = line + ((1 - Math.sqrt(d) / 140) * .14) + ')'; c.beginPath(); c.moveTo(ps[i].x, ps[i].y); c.lineTo(ps[j].x, ps[j].y); c.stroke(); }
    }
    c.fillStyle = dot; for (const p of ps) { c.beginPath(); c.arc(p.x, p.y, p.r, 0, 7); c.fill(); }
  }
  // 渐变终点要用"同一颜色的透明"，用 rgba(0,0,0,0) 会在浅色主题上把光斑混成灰色
  function transparentOf(color) { return color.startsWith('rgba') ? color.replace(/,[^,]*\)$/, ',0)') : color.startsWith('#') ? rgba(color, 0) : 'transparent' }
  const cssCache = {};
  function css(name) { return cssCache[name] || (cssCache[name] = getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#000') }
  function rgba(hex, a) { hex = hex.replace('#', ''); if (hex.length === 3) hex = [...hex].map(h => h + h).join(''); const n = parseInt(hex, 16); return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a})` }
  window.rgba = rgba; window.css = css;

  // ---------- 主渲染 ----------
  function render(t) {
    const theme = V.theme in BG ? V.theme : 'tech';
    BG[theme](t, bg);
    fx.clearRect(0, 0, W, H);
    for (const s of SCENES) {
      const el = document.getElementById(s.id);
      if (!el) continue;
      if (t >= s.a && t < s.b) {
        el.style.display = 'block';
        const lt = t - s.a;
        animIn(el, lt);
        s.fn(lt, t, el);
        const fade = s.opts.fade === false ? 1 : 1 - clamp((lt - (s.b - s.a - .3)) / .3);
        el.style.opacity = fade;
      } else el.style.display = 'none';
    }
    // 转场：闪光 + 光带 + 轻微抖动
    let f = 0, sk = -1;
    if (V.transition !== 'none') for (const c of V.cuts) {
      const d = t - c;
      if (d >= 0 && d < .6) f = Math.max(f, Math.exp(-d * 9) * (V.big.includes(c) ? 1 : .7));
      if (d > -.35 && d < .25) sk = (d + .35) / .6;
    }
    $('#__flash').style.opacity = f * (V.transition === 'soft' ? .25 : .45);
    const st = $('#__streak'); st.style.opacity = sk >= 0 ? Math.sin(sk * Math.PI) * (V.transition === 'soft' ? .3 : .9) : 0; st.style.left = (-40 + sk * 150) + '%';
    $('#stage').style.transform = V.transition === 'flash' && f > .3 ? `translate(${(rnd(t * 97) - .5) * 10 * f}px,${(rnd(t * 53) - .5) * 6 * f}px)` : 'none';
    document.body.style.opacity = 1 - clamp((t - (V.duration - V.endFade)) / V.endFade);
  }

  // ---------- 启动 ----------
  window.boot = function () {
    W = V.width; H = V.height;
    document.documentElement.style.setProperty('--W', W + 'px');
    document.documentElement.style.setProperty('--H', H + 'px');
    document.documentElement.dataset.theme = V.theme;
    const stage = $('#stage');
    stage.insertAdjacentHTML('afterbegin', `<canvas id="__bg" width="${W}" height="${H}"></canvas><canvas id="__fx" width="${W}" height="${H}"></canvas>`);
    stage.insertAdjacentHTML('beforeend', '<div id="__vignette"></div><div id="__streak"></div><div id="__flash"></div>');
    bg = $('#__bg').getContext('2d'); fx = $('#__fx').getContext('2d'); window.fx = fx;
    PTS = Array.from({ length: Math.round(W * H / 19000) }, (_, i) => ({ x: rnd(i) * W, y: rnd(i + 500) * H, vx: (rnd(i + 900) - .5) * 30, vy: (rnd(i + 1300) - .5) * 20, r: 1 + rnd(i + 1700) * 2 }));
    window.render = render;
    // 给 render.py 读的配置：同一份转场点驱动画面和配乐
    window.__videoConfig = () => JSON.parse(JSON.stringify(V));
    if (!navigator.webdriver) { const t0 = performance.now(); const loop = () => { render(((performance.now() - t0) / 1000) % V.duration); requestAnimationFrame(loop) }; loop(); }
  };
})();
