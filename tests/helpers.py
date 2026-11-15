"""Data sintetis deterministik untuk pengujian (tanpa jaringan)."""
import math
from datetime import datetime, timezone

from scalp.config import load_config
from scalp.indicators import Candle

H1, M5 = 3_600_000, 300_000
# Rabu 11 Nov 2026 12:00 UTC (bukan Senin)
NOW = int(datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)


def cfg():
    return load_config()


def make_h1(n=200, lo=100.0, hi=104.0, period=24, end=NOW):
    """Range osilasi rapi antara lo..hi, candle terakhir tertutup tepat di `end`."""
    mid, amp = (lo + hi) / 2, (hi - lo) / 2 - 0.4
    out, prev = [], mid
    start = end - n * H1
    for i in range(n):
        c = mid + amp * math.sin(2 * math.pi * i / period)
        h = max(prev, c) + 0.4
        l = min(prev, c) - 0.4
        out.append(Candle(start + i * H1, prev, h, l, c, 1000.0))
        prev = c
    return out


def make_m5_flat(n=300, price=102.0, end=NOW, noise=0.05, vol=100.0):
    start = end - n * M5
    out, p = [], price
    for i in range(n):
        d = noise * math.sin(i * 0.9)
        o, c = p, price + d
        out.append(Candle(start + i * M5, o, max(o, c) + noise * 0.5, min(o, c) - noise * 0.5, c, vol))
        p = c
    return out


def candle(t, o, h, l, c, v=100.0):
    return Candle(t, o, h, l, c, v)


def m5_with_sweep_short(level=104.0, extreme=104.35, end=NOW, vol_spike=400.0):
    """Harga naik perlahan ke level, sweep wick di atas lalu reclaim dan turun (pola deviation SHORT)."""
    base = make_m5_flat(n=250, price=102.0, end=end - 36 * M5)
    out = list(base)
    t = out[-1].t + M5
    p = 102.0
    steps = 30
    for i in range(steps):  # naik pelan ke 103.8
        c = 102.0 + (level - 0.2 - 102.0) * (i + 1) / steps
        out.append(candle(t, p, max(p, c) + 0.02, min(p, c) - 0.02, c, 100.0))
        p, t = c, t + M5
    # candle sweep: tembus level, close kembali di dalam
    out.append(candle(t, p, extreme, p - 0.05, p - 0.1, vol_spike))
    p, t = p - 0.1, t + M5
    for c in (103.6, 103.5, 103.45, 103.4, 103.4):  # turun, tidak menyentuh level lagi
        out.append(candle(t, p, p + 0.02, c - 0.02, c, 100.0))
        p, t = c, t + M5
    assert out[-1].t + M5 == end, (out[-1].t + M5, end)
    return out


def mirror(candles, axis=104.0):
    """Cerminkan harga terhadap `axis` (mengubah setup SHORT menjadi LONG)."""
    return [Candle(c.t, 2 * axis - c.o, 2 * axis - c.l, 2 * axis - c.h, 2 * axis - c.c, c.v) for c in candles]


def make_h4(n=100, price=102.0, end=NOW):
    start = end - n * 4 * H1
    return [Candle(start + i * 4 * H1, price, price + 0.5, price - 0.5, price, 1000.0) for i in range(n)]


def make_h1_breakout(end=NOW, closes=(104.5, 104.8, 105.0, 104.9, 104.9)):
    """Range 100-104 lalu 5 candle terakhir menutup di atas 104 (breakout diterima)."""
    h1 = make_h1(n=200, end=end)
    out = h1[:195]
    prev = out[-1].c
    for k, c in enumerate(closes):
        t = h1[195 + k].t
        out.append(candle(t, prev, max(prev, c) + 0.25, min(prev, c) - 0.1, c, 1000.0))
        prev = c
    return out


def m5_with_acceptance_then_fail(level=104.0, end=NOW):
    """Harga bertahan di atas level >6 candle (acceptance) lalu kembali turun: BUKAN deviation."""
    out = list(make_m5_flat(n=250, price=102.0, end=end - 42 * M5))
    t, p = out[-1].t + M5, 102.0
    for i in range(30):
        c = 102.0 + (level - 0.2 - 102.0) * (i + 1) / 30
        out.append(candle(t, p, max(p, c) + 0.02, min(p, c) - 0.02, c))
        p, t = c, t + M5
    for c in (104.1, 104.15, 104.2, 104.15, 104.1, 104.12, 104.18, 104.1):  # 8 close di luar
        out.append(candle(t, p, max(p, c) + 0.22, min(p, c) - 0.02, c))
        p, t = c, t + M5
    for c in (103.7, 103.6, 103.5, 103.45):
        out.append(candle(t, p, p + 0.02, c - 0.02, c))
        p, t = c, t + M5
    assert out[-1].t + M5 == end, (out[-1].t + M5, end)
    return out
