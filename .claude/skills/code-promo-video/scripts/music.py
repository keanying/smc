#!/usr/bin/env python3
"""按转场点合成配乐：纯代码合成，无采样、无版权素材。

读 render.py 写出的 video.json（duration / bpm / cuts / big / mood），
或者直接用命令行参数。转场点（cuts）落在拍点上效果最好。

用法：
  python music.py --config out/video.json --out out/music.wav
  python music.py --duration 30 --bpm 110 --cuts 2 8 16 24 --big 2 16 --mood bright --out music.wav

mood（情绪）：
  tech       电子、四拍底鼓、锯齿铺底 + 琶音（科技、互联网、数据产品）
  bright     轻快、明亮大调、木琴感拨弦、拍手（消费品、餐饮、生活方式、清新）
  warm       温暖、慢、钢琴感 + 弦乐铺底、轻打击（品牌故事、公益、情感）
  epic       史诗、低音鼓 + 大冲击、宽铺底（发布会、大事件、国潮）
  corporate  商务、克制、闷音拨弦 + 轻鼓（企业介绍、B2B、SaaS）

段落自动划分：开头到“第一个不早于 1.5 小节的 big”之前是前奏（不进鼓；可用 VIDEO.grooveAt 指定），中段是主歌（全编制），
最后一个转场之后是尾声（撤鼓、铺底延音、淡出）。每个 big 前放上扬、落点放冲击音；
普通转场放镲片 + 呼啸。
"""
import argparse
import json
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, fftconvolve, sosfilt

SR = 44100

MOODS = {
    #           进行（根音 MIDI、和弦音、是否小调）       默认bpm  鼓型        铺底    主奏音色
    "tech": dict(prog=[(45, "m"), (41, "M"), (48, "M"), (43, "M")], bpm=120, drums="four", pad="saw", lead="pluck", bass="octave", impact=1.0, air=1.0),
    "bright": dict(prog=[(48, "M"), (43, "M"), (45, "m"), (41, "M")], bpm=116, drums="pop", pad="soft", lead="mallet", bass="bounce", impact=.6, air=.35),
    "warm": dict(prog=[(41, "M"), (48, "M"), (50, "m"), (46, "M")], bpm=88, drums="light", pad="strings", lead="piano", bass="long", impact=.55, air=.25),
    "epic": dict(prog=[(50, "m"), (46, "M"), (41, "M"), (48, "M")], bpm=100, drums="taiko", pad="strings", lead="brass", bass="long", impact=1.2, air=.9),
    "corporate": dict(prog=[(48, "M"), (45, "m"), (41, "M"), (43, "M")], bpm=110, drums="soft", pad="soft", lead="muted", bass="octave", impact=.7, air=.45),
}


def chord(root, q):
    third = 3 if q == "m" else 4
    return [root, root + third, root + 7]


class Mixer:
    def __init__(self, dur):
        self.n = int(dur * SR)
        self.bus = {k: np.zeros(self.n) for k in ("kick", "drums", "bass", "pad", "lead", "fx")}
        self.rng = np.random.default_rng(7)

    def add(self, bus, t0, sig):
        i = int(t0 * SR)
        if i >= self.n or i + len(sig) <= 0:
            return
        if i < 0:
            sig, i = sig[-i:], 0
        j = min(self.n, i + len(sig))
        self.bus[bus][i:j] += sig[: j - i]

    def noise(self, n):
        return self.rng.standard_normal(n)


def midi(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def _filt(x, kind, f):
    f = np.clip(np.asarray(f, float), 20, SR / 2 - 200)
    if f.ndim == 0:
        f = float(f)
    return sosfilt(butter(2, f / (SR / 2), kind, output="sos"), x)


def lp(x, f): return _filt(x, "low", f)
def hp(x, f): return _filt(x, "high", f)
def bp(x, lo, hi): return _filt(x, "band", [lo, hi])


def env(n, decay):
    return np.exp(-np.arange(n) / SR / decay)


def ar(n, a, r):
    e = np.ones(n)
    na, nr = min(n, int(a * SR)), min(n, int(r * SR))
    if na: e[:na] = np.linspace(0, 1, na)
    if nr: e[-nr:] *= np.linspace(1, 0, nr)
    return e


_tab = {}


def saw(f, n, ph=0.0):
    h = int(min(48, max(1, (SR / 2 - 500) // f)))
    if h not in _tab:
        x = np.linspace(0, 1, 4096, endpoint=False)
        _tab[h] = sum(np.sin(2 * np.pi * k * x) / k for k in range(1, h + 1)) * (2 / np.pi)
    p = (ph + f * np.arange(n) / SR) % 1.0
    return _tab[h][(p * 4096).astype(int)]


def sine(f, n, ph=0.0):
    return np.sin(2 * np.pi * (ph + f * np.arange(n) / SR))


# ---------------- 音色 ----------------
def kick(m, t, v=1.0, low=45, punch=110):
    n = int(.45 * SR); tt = np.arange(n) / SR
    s = np.sin(2 * np.pi * np.cumsum(low + punch * np.exp(-tt / .035)) / SR) * env(n, .18)
    s[:300] += m.noise(300) * np.linspace(.35, 0, 300)
    m.add("kick", t, np.tanh(s * 1.6) * v)


def taiko(m, t, v=1.0):
    n = int(.9 * SR); tt = np.arange(n) / SR
    s = np.sin(2 * np.pi * np.cumsum(60 + 70 * np.exp(-tt / .05)) / SR) * env(n, .35)
    s += lp(m.noise(n), 900) * env(n, .08) * .5
    m.add("kick", t, np.tanh(s * 1.4) * v)


def clap(m, t, v=.5):
    n = int(.3 * SR); e = env(n, .09)
    for k in (0, .011, .022): e[int(k * SR):int(k * SR) + 200] += .8
    m.add("drums", t, bp(m.noise(n), 900, 4000) * e * v)


def snap(m, t, v=.35):
    n = int(.12 * SR)
    m.add("drums", t, bp(m.noise(n), 1800, 6000) * env(n, .03) * v)


def hat(m, t, v=.12, open_=False):
    n = int((.22 if open_ else .05) * SR)
    m.add("drums", t, hp(m.noise(n), 7000) * env(n, .08 if open_ else .015) * v)


def shaker(m, t, v=.06):
    n = int(.08 * SR)
    m.add("drums", t, bp(m.noise(n), 4000, 10000) * ar(n, .02, .05) * v)


def crash(m, t, v=.3):
    n = int(2.2 * SR)
    m.add("fx", t, hp(m.noise(n), 4500) * env(n, .7) * v)


def impact(m, t, v=1.0, air=1.0):
    n = int(2.6 * SR); tt = np.arange(n) / SR
    boom = np.sin(2 * np.pi * np.cumsum(32 + 60 * np.exp(-tt / .12)) / SR) * env(n, .8)
    s = boom + lp(m.noise(n), 1800) * env(n, .25) * .5
    m.add("fx", t, np.tanh(s * 1.4) * v)
    crash(m, t, .28 * v * air)


def riser(m, t_end, length=2.0, v=.3):
    n = int(length * SR); s = m.noise(n); out = np.zeros(n)
    for k in range(8):
        a, b = k * n // 8, (k + 1) * n // 8
        fc = 400 * 30 ** (k / 7)
        out[a:b] = bp(s, fc * .6, min(fc * 1.6, 19000))[a:b]
    tt = np.arange(n) / SR
    tone = np.sin(2 * np.pi * np.cumsum(220 * 2 ** (tt / length * 2)) / SR) * .25
    m.add("fx", t_end - length, (out + tone) * (tt / length) ** 2.2 * v)


def whoosh(m, t, v=.22):
    n = int(.6 * SR); tt = np.arange(n) / SR
    m.add("fx", t - .4, bp(m.noise(n), 600, 6000) * np.sin(np.pi * tt / .6) ** 2 * v)


def chime(m, t, note, v=.2):
    n = int(1.5 * SR)
    s = sum(sine(midi(note) * r, n) * a for r, a in ((1, 1), (2.76, .4), (5.4, .2))) * env(n, .5)
    m.add("fx", t, s * v)


def lead_note(m, t, note, kind, v, bus="lead"):
    f = midi(note)
    if kind == "pluck":
        n = int(.5 * SR); s = lp(saw(f, n) * .6 + np.sign(sine(f, n)) * .25, 3200) * env(n, .13)
    elif kind == "mallet":
        n = int(.7 * SR); s = (sine(f, n) + sine(f * 4, n) * .25 * env(n, .05) + sine(f * 2, n) * .2) * env(n, .22)
    elif kind == "piano":
        n = int(1.8 * SR); s = sum(sine(f * k, n) * (.6 / k) * env(n, .9 / k) for k in range(1, 6)); s *= ar(n, .004, .3)
    elif kind == "brass":
        n = int(.9 * SR); s = lp(saw(f, n) + saw(f * 1.004, n), 1800) * ar(n, .06, .3) * .6
    else:  # muted
        n = int(.35 * SR); s = lp(saw(f, n) * .5 + sine(f, n) * .5, 1500) * env(n, .07)
    m.add(bus, t, s * v * ar(len(s), .002, .03))


def bass_note(m, t, note, length, v):
    n = int(length * SR); f = midi(note)
    s = lp(saw(f, n) * .7 + sine(f, n) * .6, 900) * (env(n, .09) * .6 + .4) * ar(n, .003, .02)
    m.add("bass", t, s * v)


def pad_chord(m, t, notes, length, kind, v):
    n = int(length * SR); s = np.zeros(n)
    for note in notes:
        for d in ((-0.08, 0, 0.08) if kind != "soft" else (-0.04, 0.04)):
            f = midi(note + d)
            s += saw(f, n, m.rng.random()) if kind in ("saw", "strings") else sine(f, n, m.rng.random()) + sine(f * 2, n) * .2
    s = lp(s, {"saw": 1400, "strings": 2200, "soft": 2500}[kind]) * ar(n, .5 if kind == "strings" else .35, .6)
    m.add("pad", t, s * v / len(notes))


# ---------------- 编曲 ----------------
def compose(duration, bpm, cuts, big, mood, groove_at=None):
    M = MOODS[mood]
    beat = 60.0 / bpm
    bar = beat * 4
    m = Mixer(duration)
    cuts = sorted(c for c in cuts if 0 < c < duration)
    big = sorted(b for b in big if 0 < b < duration) or cuts[:1]
    # 鼓从哪里进：VIDEO.grooveAt 指定优先；否则取第一个不早于 1.5 小节的 big
    # （开场 2 秒的 Logo 亮相只放冲击音，鼓留到主体出现时再进，形成“蓄力 → 释放”）
    groove_on = groove_at if groove_at is not None else next((b for b in big if b >= bar * 1.5), big[0] if big else bar * 2)
    outro_on = cuts[-1] if cuts and cuts[-1] > duration - 12 else duration - min(6, duration / 4)

    for c in cuts:  # 提醒：转场不在拍点上会"踩不准"
        if abs(c / beat - round(c / beat)) > .06:
            print(f"提示：转场 {c}s 不在拍点上（{bpm} BPM 每拍 {beat:.3f}s），节奏会对不齐")

    nbars = int(np.ceil(duration / bar))
    for b in range(nbars):
        t0 = b * bar
        root, q = M["prog"][b % len(M["prog"])]
        ch = chord(root, q)
        pad_notes = [x + 12 for x in ch]
        arp = [x + 24 for x in ch] + [root + 36]
        in_intro, in_outro = t0 < groove_on, t0 >= outro_on
        fade = 1 - (t0 - outro_on) / max(1, duration - outro_on) if in_outro else 1
        pad_chord(m, t0, pad_notes, bar + (1.5 if in_outro else .5), M["pad"], (.13 if not in_intro else .11) * max(.2, fade))

        if in_intro:
            if t0 >= bar * .5 or groove_on > bar * 2:  # 前奏：稀疏旋律
                for k in range(4):
                    lead_note(m, t0 + k * beat, arp[k % 4] - 12, M["lead"], .08)
            continue
        if in_outro:
            for k in range(0, 4, 2):
                lead_note(m, t0 + k * beat, arp[k % 4], M["lead"], .07 * fade)
            continue

        # 主歌：鼓
        d = M["drums"]
        for k in range(4):
            tb = t0 + k * beat
            if d == "four": kick(m, tb); hat(m, tb + beat / 2, .13, True)
            elif d == "pop":
                if k in (0, 2): kick(m, tb, .8, 55, 90)
                shaker(m, tb); shaker(m, tb + beat / 2)
            elif d == "light":
                if k == 0: kick(m, tb, .5, 50, 60)
                shaker(m, tb + beat / 2, .04)
            elif d == "taiko":
                if k in (0, 2): taiko(m, tb)
                if k == 3: taiko(m, tb + beat / 2, .6)
            elif d == "soft":
                if k in (0, 2): kick(m, tb, .65, 50, 70)
                hat(m, tb + beat / 2, .07)
        if d in ("four", "pop", "soft"):
            (clap if d != "soft" else snap)(m, t0 + beat, .5 if d != "soft" else .3)
            (clap if d != "soft" else snap)(m, t0 + 3 * beat, .5 if d != "soft" else .3)
        if d == "four":
            for k in range(1, 8, 2): hat(m, t0 + k * beat / 2, .05)
        if d == "taiko":
            clap(m, t0 + 3 * beat, .25)

        # 贝斯
        bk = M["bass"]
        if bk == "octave":
            for k in range(8): bass_note(m, t0 + k * beat / 2, root - 12 + (12 if k % 2 else 0), beat / 2 - .02, .42)
        elif bk == "bounce":
            for k, off in enumerate((0, 1.5, 2, 3)): bass_note(m, t0 + off * beat, root - 12 + (7 if k == 1 else 0), beat * .9, .4)
        else:
            bass_note(m, t0, root - 12, bar * .95, .38)

        # 旋律
        lk = M["lead"]
        step = beat / 4 if lk in ("pluck", "muted") else beat / 2 if lk in ("mallet", "brass") else beat
        pattern = [0, 1, 2, 3, 2, 1, 0, 2, 1, 3, 2, 0, 3, 1, 2, 3]
        for k in range(int(round(bar / step))):
            lead_note(m, t0 + k * step, arp[pattern[k % 16]], lk, {"pluck": .11, "muted": .12, "mallet": .14, "brass": .1, "piano": .13}[lk])

    # 关键节点：大节点 = 上扬 + 冲击；普通转场 = 镲 + 呼啸
    iv, air = M["impact"], M["air"]
    for c in cuts:
        if c in big:
            if c >= 1.5: riser(m, c, min(2.0, c), .3 * air)
            if mood in ("bright", "warm"):
                chime(m, c, M["prog"][0][0] + 36, .3); crash(m, c, .2 * air); kick(m, c, .6)
            else:
                impact(m, c, iv, air)
            for k in range(8 if c >= beat * 4 and mood not in ("warm",) else 0):   # 军鼓滚奏推进
                clap(m, c - beat * 2 + k * beat / 4, (.1 + .05 * k) * .7)
        else:
            crash(m, c, .2 * air); whoosh(m, c, .2 * max(air, .4))
            if mood not in ("warm",): kick(m, c, .55)
    if cuts and cuts[-1] < duration - 3:
        (chime if mood in ("bright", "warm") else impact)(m, duration - 1.0, *( (M["prog"][0][0] + 36, .15) if mood in ("bright", "warm") else (.3,)))

    # 侧链：主歌段用底鼓压铺底/贝斯/旋律
    duck = np.ones(m.n)
    if M["drums"] in ("four", "pop", "taiko"):
        t = groove_on
        while t < outro_on:
            i, k = int(t * SR), int(.35 * SR)
            duck[i:i + k] = np.minimum(duck[i:i + k], 1 - .5 * np.exp(-np.arange(min(k, m.n - i)) / SR / .12))
            t += beat
    for k in ("pad", "bass", "lead"):
        m.bus[k] *= duck
    return m, beat


def reverb(x, n_out, decay=2.4, seed=0):
    g = np.random.default_rng(seed)
    n = int(decay * 1.5 * SR)
    ir = lp(g.standard_normal(n) * np.exp(-np.arange(n) / SR / (decay / 4)), 5000)
    ir /= np.sqrt(np.sum(ir ** 2))
    return fftconvolve(x, ir)[:n_out]


def delay(x, t, fb, taps=4):
    out = np.zeros_like(x); d = int(t * SR)
    for k in range(1, taps + 1):
        if d * k < len(x): out[d * k:] += x[:len(x) - d * k] * fb ** k
    return out


def mixdown(m, beat, duration):
    B = m.bus
    lead = B["lead"]
    send = B["pad"] * .5 + lead * .6 + B["fx"] * .5 + B["drums"] * .15
    dry = B["kick"] * .95 + B["drums"] * .8 + B["bass"] * .85 + B["pad"] + B["fx"] * 1.6
    L = dry + lead * .85 + delay(lead, beat * .75, .45) * .35 + reverb(send, m.n, seed=1) * .55 + B["drums"] * .1
    R = dry + lead * .85 + delay(lead, beat * 1.125, .4) * .35 + reverb(send, m.n, seed=2) * .55 - B["drums"] * .05
    st = hp(np.stack([L, R]), 28).T
    st = np.tanh(st * 1.3) / np.tanh(1.3)
    fn = int(min(2.5, duration / 6) * SR)
    st[-fn:] *= (np.linspace(1, 0, fn) ** 1.5)[:, None]
    return st * (.89 / max(1e-9, np.max(np.abs(st))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="render.py 写出的 video.json")
    ap.add_argument("--duration", type=float)
    ap.add_argument("--bpm", type=float)
    ap.add_argument("--cuts", type=float, nargs="*")
    ap.add_argument("--big", type=float, nargs="*")
    ap.add_argument("--mood", choices=list(MOODS) + ["none"])
    ap.add_argument("--out", default="music.wav")
    a = ap.parse_args()
    cfg = json.loads(Path(a.config).read_text()) if a.config else {}
    duration = a.duration or cfg.get("duration")
    mood = a.mood or cfg.get("mood", "tech")
    if not duration:
        ap.error("需要 --duration 或 --config")
    if mood == "none":
        print("mood=none，不生成配乐"); return
    bpm = a.bpm or cfg.get("bpm") or MOODS[mood]["bpm"]
    cuts = a.cuts if a.cuts is not None else cfg.get("cuts", [])
    big = a.big if a.big is not None else cfg.get("big", [])
    m, beat = compose(duration, bpm, cuts, big, mood, cfg.get("grooveAt"))
    st = mixdown(m, beat, duration)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((st * 32767).astype(np.int16).tobytes())
    print(out)


if __name__ == "__main__":
    main()
