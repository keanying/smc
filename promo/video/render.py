"""把 promo.html 逐帧渲染成视频。

用法：
  python render.py stills 3 15 22 ...   # 只出几张静帧检查画面（stills/ 下）
  python render.py video                 # 全片逐帧渲染，输出 out/promo_silent.mp4
"""
import asyncio, os, subprocess, sys
from pathlib import Path
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
FPS = 30
CHROME = os.getenv("CHROME", "/opt/pw-browsers/chromium")


async def open_page(p):
    b = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox", "--force-color-profile=srgb"])
    pg = await (await b.new_context(viewport={"width": 1920, "height": 1080})).new_page()
    await pg.goto((HERE / "promo.html").as_uri())
    await pg.evaluate("document.fonts.ready")
    await pg.evaluate("Promise.all([...document.images].map(i => i.decode().catch(() => 0)))")
    return b, pg


async def stills(times):
    out = HERE / "stills"; out.mkdir(exist_ok=True)
    async with async_playwright() as p:
        b, pg = await open_page(p)
        for t in times:
            await pg.evaluate(f"render({t})")
            await pg.screenshot(path=str(out / f"t{float(t):05.1f}.png"))
        await b.close()


async def video(start=0.0, end=None):
    out = HERE / "out"; out.mkdir(exist_ok=True)
    async with async_playwright() as p:
        b, pg = await open_page(p)
        dur = end or await pg.evaluate("DURATION")
        n = int(round((dur - start) * FPS))
        ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
                               "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                               str(out / "promo_silent.mp4")], stdin=subprocess.PIPE)
        for i in range(n):
            await pg.evaluate(f"render({start + i / FPS})")
            ff.stdin.write(await pg.screenshot(type="jpeg", quality=93))
            if i % 150 == 0:
                print(f"frame {i}/{n}", flush=True)
        ff.stdin.close(); ff.wait()
        await b.close()


if __name__ == "__main__":
    if sys.argv[1] == "stills":
        asyncio.run(stills([float(x) for x in sys.argv[2:]]))
    else:
        asyncio.run(video())
