"""宣传片配乐：纯代码合成（无采样、无版权素材）。

120 BPM，一小节 2 秒，A 小调 Am-F-C-G 循环。画面每个转场都落在小节线上，
关键节点（CUTS）放冲击音 + 前置上扬，突出重点。

用法：python music.py   → out/music.wav
"""
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt, fftconvolve

SR = 44100
DUR = 74.0
N = int(SR * DUR)
BEAT = 0.5
BAR = 2.0
rng = np.random.default_rng(42)

# 和画面一致的转场时间点
CUTS = [2, 6, 12, 18, 26, 34, 42, 52, 60, 61.5, 63, 64.5, 66]
BIG = {2, 12, 42, 60, 66}           # 最重的几个节点

L = np.zeros(N)
R = np.zeros(N)
BUS = {k: np.zeros(N) for k in ("kick", "drums", "bass", "pad", "arp", "fx", "lead")}


def midi(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def lp(x, f, order=2):
    return sosfilt(butter(order, min(f, SR / 2 - 100) / (SR / 2), "low", output="sos"), x)


def hp(x, f, order=2):
    return sosfilt(butter(order, f / (SR / 2), "high", output="sos"), x)


def bp(x, lo, hi, order=2):
    return sosfilt(butter(order, [lo / (SR / 2), hi / (SR / 2)], "band", output="sos"), x)


def add(bus, t0, sig):
    i = int(t0 * SR)
    if i >= N:
        return
    j = min(N, i + len(sig))
    BUS[bus][i:j] += sig[: j - i]


_tables = {}


def saw(freq, n, phase=0.0):
    """带限锯齿波（波表，按频率截谐波，防混叠）。"""
    h = max(1, int((SR / 2 - 500) / freq))
    h = min(h, 64)
    if h not in _tables:
        x = np.linspace(0, 1, 4096, endpoint=False)
        tb = sum(np.sin(2 * np.pi * k * x) / k for k in range(1, h + 1)) * (2 / np.pi)
        _tables[h] = tb
    tb = _tables[h]
    ph = (phase + freq * np.arange(n) / SR) % 1.0
    return tb[(ph * 4096).astype(int)]


def env_exp(n, decay):
    return np.exp(-np.arange(n) / SR / decay)


def adsr(n, a, r):
    e = np.ones(n)
    na, nr = int(a * SR), int(r * SR)
    if na:
        e[:na] = np.linspace(0, 1, na)
    if nr:
        e[-nr:] *= np.linspace(1, 0, nr)
    return e


# ---------------- 乐器 ----------------
def kick(t0, v=1.0):
    n = int(0.45 * SR)
    tt = np.arange(n) / SR
    f = 45 + 110 * np.exp(-tt / 0.035)
    ph = 2 * np.pi * np.cumsum(f) / SR
    s = np.sin(ph) * env_exp(n, 0.18)
    s[:300] += rng.standard_normal(300) * np.linspace(.4, 0, 300)
    add("kick", t0, np.tanh(s * 1.6) * v)


def clap(t0, v=.5):
    n = int(.3 * SR)
    nz = bp(rng.standard_normal(n), 900, 4000)
    e = env_exp(n, .09)
    for k in (0.0, .011, .022):
        e[int(k * SR):int(k * SR) + 200] += .8
    add("drums", t0, nz * e * v)


def hat(t0, v=.12, open_=False):
    n = int((.22 if open_ else .05) * SR)
    s = hp(rng.standard_normal(n), 7000) * env_exp(n, .08 if open_ else .015)
    add("drums", t0, s * v)


def crash(t0, v=.35):
    n = int(2.2 * SR)
    s = hp(rng.standard_normal(n), 4500) * env_exp(n, .7)
    add("fx", t0, s * v)


def impact(t0, v=1.0):
    n = int(2.6 * SR)
    tt = np.arange(n) / SR
    f = 32 + 60 * np.exp(-tt / 0.12)
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * env_exp(n, .8)
    nz = lp(rng.standard_normal(n), 1800) * env_exp(n, .25) * .5
    add("fx", t0, np.tanh((boom + nz) * 1.4) * v)
    crash(t0, .28 * v)


def riser(t_end, length=2.0, v=.35):
    n = int(length * SR)
    s = rng.standard_normal(n)
    # 分段扫频：越往后越亮
    out = np.zeros(n)
    seg = 8
    for k in range(seg):
        a, b = k * n // seg, (k + 1) * n // seg
        fc = 400 * (12000 / 400) ** (k / (seg - 1))
        out[a:b] = bp(s, fc * .6, min(fc * 1.6, 20000))[a:b]
    tt = np.arange(n) / SR
    tone = np.sin(2 * np.pi * np.cumsum(220 * 2 ** (tt / length * 2)) / SR) * .25
    e = (tt / length) ** 2.2
    add("fx", t_end - length, (out + tone) * e * v)


def whoosh(t_c, v=.25):
    n = int(.6 * SR)
    tt = np.arange(n) / SR
    e = np.sin(np.pi * np.clip(tt / .6, 0, 1)) ** 2
    s = bp(rng.standard_normal(n), 600, 6000) * e
    add("fx", t_c - .4, s * v)


def bass_note(t0, m, length, v=.5):
    n = int(length * SR)
    f = midi(m)
    s = saw(f, n) * .7 + np.sin(2 * np.pi * f * np.arange(n) / SR) * .6
    cut = 300 + 1600 * env_exp(n, .06)
    # 时变低通：分两段近似
    s = lp(s, 900) * (env_exp(n, .09) * .6 + .4) * adsr(n, .003, .02)
    add("bass", t0, s * v)


def pad_chord(t0, notes, length, v=.12):
    n = int(length * SR)
    s = np.zeros(n)
    for m in notes:
        for d in (-0.09, 0, 0.09):
            s += saw(midi(m + d), n, phase=rng.random())
    s = lp(s, 1400, 2) * adsr(n, .35, .5)
    add("pad", t0, s * v / len(notes))


def pluck(t0, m, v=.16, bus="arp", decay=.13):
    n = int(.5 * SR)
    f = midi(m)
    s = saw(f, n) * .6 + np.sign(np.sin(2 * np.pi * f * np.arange(n) / SR)) * .25
    s = lp(s, 3200) * env_exp(n, decay) * adsr(n, .002, .05)
    add(bus, t0, s * v)


# ---------------- 编曲 ----------------
PROG = [  # (bass, pad, arp)
    (33, [57, 60, 64], [69, 72, 76, 81]),   # Am
    (29, [57, 60, 65], [65, 69, 72, 77]),   # F
    (36, [55, 60, 64], [67, 72, 76, 79]),   # C
    (31, [55, 59, 62], [67, 71, 74, 79]),   # G
]
ARP = [0, 1, 2, 3, 2, 1, 0, 2, 1, 3, 2, 0, 3, 1, 2, 3]

for b in range(int(DUR / BAR)):
    t0 = b * BAR
    root, pad, arp = PROG[b % 4]
    # 段落：intro(0-6) / tension(6-12) / groove(12-60) / montage(60-66) / outro(66-74)
    if t0 < 66:
        pad_chord(t0, pad, BAR + .5, .13 if t0 >= 12 else .11)
    else:
        pad_chord(t0, pad, BAR + 1.5, .14 * (1 - (t0 - 66) / 10))
    if 2 <= t0 < 6:
        for k in range(4):
            pluck(t0 + k * .5, arp[k % 4] - 12, .08, decay=.25)
    if 6 <= t0 < 12:
        kick(t0, .8)
        for k in range(16):
            hat(t0 + k * .125, .05 + .03 * (k % 4 == 2))
        for k in range(4):
            bass_note(t0 + k * .5, root, .45, .35)
    if 12 <= t0 < 66:
        for k in range(4):
            kick(t0 + k * BEAT)
            hat(t0 + k * BEAT + .25, .13, open_=True)
        clap(t0 + .5); clap(t0 + 1.5)
        for k in range(16):
            if k % 2:
                hat(t0 + k * .125, .05)
        for k in range(8):
            bass_note(t0 + k * .25, root + (12 if k % 2 else 0), .23, .42)
        lv = .11 if t0 < 42 else .14
        for k in range(16):
            pluck(t0 + k * .125, arp[ARP[k]], lv)
        if 42 <= t0 < 52 or 60 <= t0 < 66:   # AI 段和快剪段加高八度旋律
            for k, step in enumerate([0, 3, 2, 1]):
                pluck(t0 + k * .5, arp[step] + 12, .09, bus="lead", decay=.35)
    if t0 >= 66 and t0 < 72:
        for k in range(4):
            pluck(t0 + k * .5, arp[(k * 2) % 4], .07 * (1 - (t0 - 66) / 8), decay=.4)

# 小节前的军鼓滚奏，推进到大节点
for tc in (12, 42, 60):
    for k in range(8):
        clap(tc - 1 + k * .125, .12 + .05 * k)

# 关键节点：冲击 + 上扬 + 转场呼啸
riser(2.0, 2.0, .3)
for tc in CUTS:
    if tc in BIG:
        impact(tc, 1.0 if tc != 2 else .9)
        if tc != 2:
            riser(tc, 2.0, .32)
    else:
        crash(tc, .22)
        kick(tc, .6)
        whoosh(tc, .22)
impact(73.2, .35)   # 最后一下余韵

# 侧链：鼓点压一压铺底和贝斯，律动更清楚
duck = np.ones(N)
for b in range(int(12 / BEAT), int(66 / BEAT)):
    i = int(b * BEAT * SR)
    n = int(.35 * SR)
    duck[i:i + n] = np.minimum(duck[i:i + n], 1 - .55 * np.exp(-np.arange(n) / SR / .12))
for k in ("pad", "bass", "arp"):
    BUS[k] *= duck

# 混响（指数衰减噪声卷积）+ 乒乓延迟
def reverb(x, decay=2.0, seed=0):
    g = np.random.default_rng(seed)
    n = int(decay * 1.5 * SR)
    ir = g.standard_normal(n) * np.exp(-np.arange(n) / SR / (decay / 4))
    ir = lp(ir, 5000)
    ir /= np.sqrt(np.sum(ir ** 2))
    return fftconvolve(x, ir)[:N]


def delay(x, t, fb, n_taps=4):
    out = np.zeros(N)
    d = int(t * SR)
    for k in range(1, n_taps + 1):
        out[d * k:] += x[:N - d * k] * fb ** k
    return out


arp = BUS["arp"] + BUS["lead"]
dl = delay(arp, .375, .45)
dr = delay(arp, .5625, .4)
send = BUS["pad"] * .5 + arp * .6 + BUS["fx"] * .5 + BUS["drums"] * .15
revL, revR = reverb(send, 2.4, 1), reverb(send, 2.4, 2)

dry = BUS["kick"] * .95 + BUS["drums"] * .8 + BUS["bass"] * .85 + BUS["pad"] + BUS["fx"] * 1.6
L = dry + arp * .85 + dl * .35 + revL * .55
R = dry + arp * .85 + dr * .35 + revR * .55
# 轻微立体声展宽：拍点 hat 略偏
L += BUS["drums"] * .1
R -= BUS["drums"] * .05

st = np.stack([L, R], 1)
st = hp(st.T, 28).T
st = np.tanh(st * 1.3) / np.tanh(1.3)          # 柔和削波
fade = np.ones(N)
fn = int(2.5 * SR)
fade[-fn:] = np.linspace(1, 0, fn) ** 1.5
st *= fade[:, None]
st *= .89 / np.max(np.abs(st))                  # 峰值约 -1 dBFS

out = Path(__file__).resolve().parent / "out"
out.mkdir(exist_ok=True)
import wave
with wave.open(str(out / "music.wav"), "wb") as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes((st * 32767).astype(np.int16).tobytes())
print("ok", out / "music.wav")
