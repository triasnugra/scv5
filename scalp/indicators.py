"""Indikator dasar tanpa dependensi eksternal (stdlib only)."""
from collections import namedtuple

# t = open time (ms), o/h/l/c = harga, v = volume
Candle = namedtuple("Candle", "t o h l c v")


def true_range(c, prev_close):
    return max(c.h - c.l, abs(c.h - prev_close), abs(c.l - prev_close))


def atr(candles, n=14):
    """ATR Wilder pada candle terakhir."""
    if len(candles) < 2:
        return 0.0
    trs = [true_range(candles[i], candles[i - 1].c) for i in range(1, len(candles))]
    if len(trs) < n:
        return sum(trs) / len(trs)
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def ema(values, n):
    if not values:
        return []
    k = 2.0 / (n + 1)
    out, e = [], None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def sma_last(values, n):
    if not values:
        return 0.0
    seg = values[-n:]
    return sum(seg) / len(seg)


def obv(candles):
    """On-Balance Volume kumulatif."""
    out = [0.0]
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        if c.c > p.c:
            out.append(out[-1] + c.v)
        elif c.c < p.c:
            out.append(out[-1] - c.v)
        else:
            out.append(out[-1])
    return out


def pivots(candles, n=3):
    """Swing high/low (fractal n kiri-n kanan). Return list (idx, 'H'|'L', price)."""
    res = []
    for i in range(n, len(candles) - n):
        hi, lo = candles[i].h, candles[i].l
        left = candles[i - n:i]
        right = candles[i + 1:i + 1 + n]
        if all(hi > x.h for x in left) and all(hi >= x.h for x in right):
            res.append((i, "H", hi))
        if all(lo < x.l for x in left) and all(lo <= x.l for x in right):
            res.append((i, "L", lo))
    return res
