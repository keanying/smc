#!/usr/bin/env python3
"""把动画页面逐帧渲染成视频（或只出几张静帧检查画面）。

页面要求：用 assets/engine.js，声明 window.VIDEO 并调用 boot()，提供 render(t)。

用法：
  python render.py PAGE.html --stills 1.5 8 20 [--out DIR]   # 静帧 + 拼成一张 contact.png
  python render.py PAGE.html [--out DIR] [--scale 0.5]       # 全片 → DIR/silent.mp4，并写 DIR/video.json

  --scale 0.5 先出半分辨率小样，速度约快 3 倍，适合给用户先看节奏。
环境变量 CHROME 可指定 Chromium 可执行文件。
"""
import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright


def find_chrome():
    for c in [os.getenv("CHROME"), "/opt/pw-browsers/chromium", shutil.which("chromium"),
              shutil.which("chromium-browser"), shutil.which("google-chrome")]:
        if c and Path(c).exists():
            return c
    return None   # 交给 Playwright 自带的浏览器


async def open_page(p, page, scale=1.0):
    exe = find_chrome()
    b = await p.chromium.launch(**({"executable_path": exe} if exe else {}),
                                args=["--no-sandbox", "--force-color-profile=srgb", "--disable-dev-shm-usage"])
    ctx = await b.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=scale)
    pg = await ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    await pg.goto(Path(page).resolve().as_uri())
    await pg.wait_for_function("typeof window.render === 'function'", timeout=15000)
    cfg = await pg.evaluate("window.__videoConfig()")
    await pg.set_viewport_size({"width": cfg["width"], "height": cfg["height"]})
    await pg.evaluate("document.fonts.ready")
    await pg.evaluate("Promise.all([...document.images].map(i => i.decode().catch(() => 0)))")
    return b, pg, cfg, errors


async def stills(page, out, times):
    out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        b, pg, cfg, errors = await open_page(p, page)
        files = []
        for t in times:
            await pg.evaluate(f"render({t})")
            f = out / f"still_{t:06.2f}.png"
            await pg.screenshot(path=str(f))
            files.append(f)
        await b.close()
    if errors:
        print("页面报错：", *errors[:5], sep="\n  ")
    try:   # 拼成一张缩略图，方便一次看完
        from PIL import Image
        cols = 2 if cfg["width"] >= cfg["height"] else 4
        tw = 960 if cols == 2 else 480
        th = round(tw * cfg["height"] / cfg["width"])
        rows = (len(files) + cols - 1) // cols
        sheet = Image.new("RGB", (tw * cols, th * rows))
        for i, f in enumerate(files):
            sheet.paste(Image.open(f).convert("RGB").resize((tw, th)), ((i % cols) * tw, (i // cols) * th))
        sheet.save(out / "contact.png")
        print("contact sheet:", out / "contact.png")
    except ImportError:
        pass
    for f in files:
        print(f)


async def video(page, out, scale):
    out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        b, pg, cfg, errors = await open_page(p, page, scale)
        (out / "video.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        fps, dur = cfg["fps"], cfg["duration"]
        n = int(round(dur * fps))
        w, h = int(cfg["width"] * scale) // 2 * 2, int(cfg["height"] * scale) // 2 * 2
        name = "silent.mp4" if scale == 1 else "silent_preview.mp4"
        ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps),
                               "-c:v", "mjpeg", "-i", "-", "-vf", f"scale={w}:{h}",
                               "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
                               "-movflags", "+faststart", str(out / name)], stdin=subprocess.PIPE)
        t0 = time.time()
        for i in range(n):
            await pg.evaluate(f"render({i / fps})")
            ff.stdin.write(await pg.screenshot(type="jpeg", quality=93))
            if i % (fps * 5) == 0 and i:
                el = time.time() - t0
                print(f"frame {i}/{n}  已用 {el:.0f}s  预计还要 {el / i * (n - i):.0f}s", flush=True)
        ff.stdin.close()
        ff.wait()
        await b.close()
    if errors:
        print("页面报错：", *errors[:5], sep="\n  ")
    print(out / name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("page")
    ap.add_argument("--out", default=None, help="输出目录，默认为页面旁边的 out/")
    ap.add_argument("--stills", nargs="*", type=float)
    ap.add_argument("--scale", type=float, default=1.0)
    a = ap.parse_args()
    out = Path(a.out) if a.out else Path(a.page).resolve().parent / "out"
    if a.stills is not None:
        asyncio.run(stills(a.page, out / "stills", a.stills))
    else:
        asyncio.run(video(a.page, out, a.scale))


if __name__ == "__main__":
    sys.exit(main())
