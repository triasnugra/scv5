"""Konteks pasar: 'musim' long/short (H4), posisi BTC di range, guncangan volatilitas."""
from .indicators import atr, ema
from .structure import find_ranges


def season(h4):
    """Musim long/short dari H4: 'scalp long in long season, scalp short in short season'."""
    closes = [c.c for c in h4]
    if len(closes) < 60:
        return "neutral"
    e21 = ema(closes, 21)
    e55 = ema(closes, 55)
    last = closes[-1]
    slope = e55[-1] - e55[-6]
    if last > e55[-1] and e21[-1] > e55[-1] and slope > 0:
        return "long"
    if last < e55[-1] and e21[-1] < e55[-1] and slope < 0:
        return "short"
    return "neutral"


def range_position(price, rng):
    if not rng:
        return None
    return (price - rng["low"]) / (rng["high"] - rng["low"])


def position_label(pos):
    if pos is None:
        return "no_range"
    if pos >= 0.85:
        return "range_top"
    if pos <= 0.15:
        return "range_bottom"
    if 0.35 <= pos <= 0.65:
        return "mid_no_mans_land"
    return "upper" if pos > 0.5 else "lower"


def shock(h1, cfg):
    """True bila candle H1 terakhir (tertutup) jauh lebih liar dari normal: jangan scalp saat flash move."""
    if len(h1) < 20:
        return False
    a = atr(h1[:-1], 14)
    c = h1[-1]
    return a > 0 and (c.h - c.l) > cfg["rules"]["filters"]["btc_shock_atr"] * a


def build_btc_context(btc, cfg):
    """btc: dict {'5m','1h','4h'} candle. Return konteks pasar."""
    if not btc or len(btc.get("1h", [])) < 100:
        return {"available": False, "season": "neutral", "pos": None, "label": "no_data", "shock": False}
    h1 = btc["1h"]
    a = atr(h1, 14)
    ranges = find_ranges(h1, a, cfg)
    price = (btc["5m"][-1].c if btc.get("5m") else h1[-1].c)
    rng = ranges[0] if ranges else None
    pos = range_position(price, rng)
    return {
        "available": True,
        "season": season(btc.get("4h", [])),
        "pos": None if pos is None else round(pos, 3),
        "label": position_label(pos),
        "shock": shock(h1, cfg),
        "range": rng,
        "price": price,
    }
