# 环境依赖

先跑一遍自检，缺什么装什么：

```bash
python -c "import playwright, numpy, scipy, PIL; print('python ok')"
ffmpeg -version | head -1
fc-list :lang=zh family | head -3          # 有 Noto Sans CJK SC / 思源黑体 等即可
fc-list | grep -i "jetbrains" | head -1    # 等宽字体，可选（没有时回退到 DejaVu Sans Mono）
```

| 依赖 | 安装（Debian / Ubuntu） | 说明 |
| :--- | :--- | :--- |
| Python 包 | `pip install playwright numpy scipy pillow` | pillow 只用来拼静帧缩略图 |
| Chromium | 优先用已装的：`/opt/pw-browsers/chromium`、系统 chromium/chrome；都没有再 `playwright install chromium` | `render.py` 自动探测，或用环境变量 `CHROME` 指定 |
| ffmpeg | `apt-get install -y ffmpeg` | 需要 libx264 和 aac |
| 中文字体 | `apt-get install -y fonts-noto-cjk` | 没有中文字体会渲染成方块，这是最常见的翻车点 |
| 等宽字体 | `apt-get install -y fonts-jetbrains-mono`（可选） | |

macOS：`brew install ffmpeg`，系统自带苹方（PingFang SC），`themes.css` 的字体栈已包含。
Windows：装 ffmpeg 并加入 PATH，系统自带微软雅黑。

Playwright 版本和本机 Chromium 版本不匹配时会报 “Executable doesn't exist”，
不要重装 Playwright，用 `CHROME=/path/to/chrome` 指向现有浏览器即可。

渲染速度参考：1080p 30fps，每帧约 0.1–0.3 秒（1 秒成片 ≈ 3–10 秒，画面越复杂越慢）。`--preview` 半分辨率约快 3 倍。
