"""Struktur pasar: range H1, level Monday, swing target.

Filosofi @eliz883: "Work on ranges in the lower timeframes, especially H1, focusing on
deviations from the range's highs and lows." Tanpa range valid = tidak ada keunggulan.
"""
from datetime import datetime, timedelta, timezone

from .indicators import pivots

H1_MS = 3_600_000
DAY_MS = 86_400_000


def _count_touches(flags):
    """Hitung klaster sentuhan (transisi False->True)."""
    touches, prev = 0, False
    for f in flags:
        if f and not prev:
            touches += 1
        prev = f
    return touches


def build_range(h1, end_off, window, atr_h1, cfg):
    rc = cfg["rules"]["range"]
    end = len(h1) - end_off
    start = end - window
    if start < 0 or end <= start:
        return None
    seg = h1[start:end]
    hi = max(c.h for c in seg)
    lo = min(c.l for c in seg)
    last = seg[-1].c
    if last <= 0 or hi <= lo:
        return None
    height = hi - lo
    height_pct = height / last * 100
    if not (rc["min_height_pct"] <= height_pct <= rc["max_height_pct"]):
        return None
    tol = rc["touch_tol_atr"] * atr_h1
    th = _count_touches([c.h >= hi - tol for c in seg])
    tl = _count_touches([c.l <= lo + tol for c in seg])
    if th < rc["min_touches_each"] or tl < rc["min_touches_each"]:
        return None
    if abs(seg[-1].c - seg[0].c) / height > rc["max_drift"]:
        return None  # trending, bukan range
    mid = (hi + lo) / 2
    crosses, prev_side = 0, None
    for c in seg:
        side = c.c > mid
        if prev_side is not None and side != prev_side:
            crosses += 1
        prev_side = side
    if crosses < rc["min_mid_crosses"]:
        return None
    return {
        "high": hi, "low": lo, "mid": mid, "height": height, "height_pct": round(height_pct, 3),
        "touches_high": th, "touches_low": tl, "window": window, "end_off": end_off,
        "start_t": seg[0].t, "end_t": seg[-1].t + H1_MS,
    }


def find_ranges(h1, atr_h1, cfg):
    """Cari semua range H1 valid (beberapa offset akhir supaya sweep/breakout terbaru tak mencemari)."""
    rc = cfg["rules"]["range"]
    found = []
    for end_off in rc["end_offsets_h1"]:
        for window in rc["windows_h1"]:
            r = build_range(h1, end_off, window, atr_h1, cfg)
            if not r:
                continue
            dup = any(abs(r["high"] - x["high"]) < 0.3 * atr_h1 and abs(r["low"] - x["low"]) < 0.3 * atr_h1
                      for x in found)
            if not dup:
                found.append(r)
            break  # window terbesar yang valid untuk offset ini
    return found


def monday_levels(h1, now_ms):
    """High/low Monday (UTC). Saat hari Senin masih berjalan, pakai Monday minggu lalu."""
    dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    start = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    prev_week = False
    if dt.weekday() == 0:
        start -= timedelta(days=7)
        prev_week = True
    s_ms = int(start.timestamp() * 1000)
    bars = [c for c in h1 if s_ms <= c.t < s_ms + DAY_MS]
    if len(bars) < 20:
        return None
    hi, lo = max(c.h for c in bars), min(c.l for c in bars)
    return {"high": hi, "low": lo, "mid": (hi + lo) / 2, "prev_week": prev_week, "start_t": s_ms}


def collect_levels(ranges, monday, atr_h1):
    """Level likuiditas untuk deteksi deviation. side: 'high' (sweep ke atas -> short) / 'low'."""
    levels = []

    def add(price, kind, side, touches, is_monday):
        for x in levels:
            if abs(x["price"] - price) < 0.15 * atr_h1 and x["side"] == side:
                if touches > x["touches"]:
                    x.update(price=price, kind=kind, touches=touches, monday=is_monday or x["monday"])
                return
        levels.append({"price": price, "kind": kind, "side": side, "touches": touches, "monday": is_monday})

    for r in ranges:
        add(r["high"], "range_high", "high", r["touches_high"], False)
        add(r["low"], "range_low", "low", r["touches_low"], False)
    if monday:
        add(monday["high"], "monday_high", "high", 0, True)
        add(monday["low"], "monday_low", "low", 0, True)
    return levels


def target_levels(ranges, monday, h1, pivot_n=3):
    """Kandidat target (likuiditas sisi seberang): mid/edge range, Monday, swing H1."""
    t = []
    for r in ranges:
        t += [{"price": r["mid"], "label": "range mid"}, {"price": r["high"], "label": "range high"},
              {"price": r["low"], "label": "range low"}]
    if monday:
        t += [{"price": monday["mid"], "label": "Monday mid"}, {"price": monday["high"], "label": "Monday high"},
              {"price": monday["low"], "label": "Monday low"}]
    for _, kind, price in pivots(h1[-120:], pivot_n):
        t.append({"price": price, "label": "swing high H1" if kind == "H" else "swing low H1"})
    return t
