"""Deteksi setup scalping ala @eliz883.

Tiga pola, semuanya berbasis TRIGGER (bukan prediksi) dan entry LIMIT di level presisi (tidak mengejar harga):
  1. DEVIATION        : sweep likuiditas di atas/bawah level (range H1 / Monday) -> reclaim -> retest.  (stop run)
  2. BREAKOUT_RETEST  : close H1 di luar range, terima di luar, entry pada retest pertama.
  3. RANGE_EDGE       : penolakan (wick) di tepi range tanpa sweep; kualitas lebih rendah, butuh konfluensi.
Semua kandidat harus lolos: stop kecil tapi tidak terlalu sempit, R:R minimal, biaya wajar.
"""
from .indicators import atr, obv, sma_last
from .context import season
from .structure import (H1_MS, collect_levels, find_ranges, monday_levels, target_levels)

LONG, SHORT = "LONG", "SHORT"


def fmt(p):
    return f"{p:.6g}"


def plan_trade(direction, entry, stop, targets, cfg):
    """Validasi risiko & pilih TP1/TP2. Return (plan|None, alasan_tolak)."""
    p = cfg["rules"]["plan"]
    sgn = 1 if direction == LONG else -1
    risk = (entry - stop) * sgn
    if risk <= 0:
        return None, "stop_salah_sisi"
    risk_pct = risk / entry * 100
    if risk_pct < p["min_stop_pct"]:
        return None, "stop_terlalu_sempit"
    if risk_pct > p["max_stop_pct"]:
        return None, "stop_terlalu_lebar"
    cost_r = cfg["sim"]["cost_pct"] / risk_pct
    if cost_r > p["max_cost_r"]:
        return None, "biaya_terlalu_besar"
    ahead = sorted([t for t in targets if (t["price"] - entry) * sgn > 0], key=lambda t: (t["price"] - entry) * sgn)
    tp1 = next((t for t in ahead if (t["price"] - entry) * sgn / risk >= p["min_rr"]), None)
    if not tp1:
        return None, "rr_terlalu_rendah"
    later = [t for t in ahead if (t["price"] - tp1["price"]) * sgn >= 0.5 * risk]
    tp2 = later[0] if later else {"price": tp1["price"] + sgn * risk, "label": "TP1 + 1R"}
    return {
        "tp1": tp1["price"], "tp1_label": tp1["label"], "tp2": tp2["price"], "tp2_label": tp2["label"],
        "risk_pct": round(risk_pct, 4), "rr1": round((tp1["price"] - entry) * sgn / risk, 2),
        "rr2": round((tp2["price"] - entry) * sgn / risk, 2), "cost_r": round(cost_r, 3),
    }, None


def _vol_ratio(m5, idxs):
    first = min(idxs)
    base = [c.v for c in m5[max(0, first - 20):first]]
    if not base or sum(base) == 0:
        return 0.0
    avg = sum(base) / len(base)
    return max(m5[i].v for i in idxs) / avg


def detect_deviation(m5, obv5, level, atr5, atr_h1, cfg):
    d = cfg["rules"]["deviation"]
    L, side = level["price"], level["side"]
    n = len(m5)
    start = max(0, n - d["lookback_5m"])
    eps = L * d["pierce_min_pct"] / 100
    high_side = side == "high"
    if high_side:
        pierce = [i for i in range(start, n) if m5[i].h > L + eps]
        outside = lambda c: c.c > L
    else:
        pierce = [i for i in range(start, n) if m5[i].l < L - eps]
        outside = lambda c: c.c < L
    if not pierce:
        return None
    out_idx = [i for i in range(pierce[0], n) if outside(m5[i])]
    if len(out_idx) > d["max_outside_bars"]:
        return None  # terlalu lama di luar = acceptance, bukan deviation
    r = (out_idx[-1] + 1) if out_idx else pierce[-1]
    if r > n - 1:
        return None  # belum ada reclaim
    margin = d["reclaim_margin_atr5"] * atr5
    rc = m5[r].c
    if high_side and not rc < L - margin:
        return None
    if not high_side and not rc > L + margin:
        return None
    last = m5[-1]
    if (n - 1 - r) > d["lookback_5m"]:
        return None
    # retest belum terjadi (jangan mengejar): bar setelah reclaim tidak menyentuh level lagi
    after = m5[r + 1:]
    if high_side and any(c.h >= L for c in after):
        return None
    if not high_side and any(c.l <= L for c in after):
        return None
    extreme = max(m5[i].h for i in pierce) if high_side else min(m5[i].l for i in pierce)
    depth = abs(extreme - L)
    if depth / atr_h1 > d["max_sweep_atr_h1"]:
        return None  # sweep terlalu dalam = breakout sungguhan
    buf = d["stop_buffer_atr5"] * atr5
    off = d["entry_offset_atr5"] * atr5
    if high_side:
        direction, entry, stop = SHORT, L - off, extreme + buf
    else:
        direction, entry, stop = LONG, L + off, extreme - buf
    # OBV & volume
    e_idx = max(pierce, key=lambda i: m5[i].h) if high_side else min(pierce, key=lambda i: m5[i].l)
    w = d["obv_window"]
    lo_i = max(0, e_idx - w)
    obv_div = False
    if e_idx - lo_i >= 6:
        if high_side:
            obv_div = m5[e_idx].h >= max(c.h for c in m5[lo_i:e_idx]) and obv5[e_idx] < max(obv5[lo_i:e_idx])
        else:
            obv_div = m5[e_idx].l <= min(c.l for c in m5[lo_i:e_idx]) and obv5[e_idx] > min(obv5[lo_i:e_idx])
    sgn = 1 if direction == LONG else -1
    obv_confirm = (obv5[-1] - obv5[max(0, n - 7)]) * sgn > 0
    vr = _vol_ratio(m5, pierce)
    txt = (f"Sweep {'di atas' if high_side else 'di bawah'} {level['kind']} {fmt(L)} (ekstrem {fmt(extreme)}), "
           f"lalu reclaim close kembali ke dalam; tunggu retest di {fmt(entry)}.")
    return {
        "setup": "DEVIATION", "direction": direction, "entry": entry, "stop": stop,
        "level": {"price": L, "kind": level["kind"], "touches": level["touches"], "monday": level["monday"]},
        "flags": {"vol_ratio": round(vr, 2), "vol_spike": vr >= d["vol_spike_ratio"], "obv_div": bool(obv_div),
                  "obv_confirm": bool(obv_confirm), "sweep_depth_atr_h1": round(depth / atr_h1, 2)},
        "reasons": [txt], "targets_extra": [],
    }


def detect_range_edge(m5, obv5, rng, side, atr5, atr_h1, cfg):
    rc = cfg["rules"]["range_edge"]
    L = rng["high"] if side == "high" else rng["low"]
    zone = rc["edge_zone_atr_h1"] * atr_h1
    eps = L * cfg["rules"]["deviation"]["pierce_min_pct"] / 100
    n = len(m5)
    best = None
    for i in range(max(0, n - rc["lookback_5m"]), n):
        c = m5[i]
        span = c.h - c.l
        if span <= 0:
            continue
        if side == "high":
            if c.h > L + eps or c.h < L - zone:
                continue
            if (c.h - max(c.o, c.c)) / span >= rc["wick_frac"] and c.c <= c.l + 0.4 * span:
                best = i
        else:
            if c.l < L - eps or c.l > L + zone:
                continue
            if (min(c.o, c.c) - c.l) / span >= rc["wick_frac"] and c.c >= c.h - 0.4 * span:
                best = i
    if best is None:
        return None
    rej, after, last = m5[best], m5[best + 1:], m5[-1]
    buf = rc["stop_buffer_atr_h1"] * atr_h1
    if side == "high":
        if any(c.h >= rej.h for c in after) or last.c >= rej.h:
            return None
        direction, entry, stop = SHORT, rej.h, max(L, rej.h) + buf
    else:
        if any(c.l <= rej.l for c in after) or last.c <= rej.l:
            return None
        direction, entry, stop = LONG, rej.l, min(L, rej.l) - buf
    sgn = 1 if direction == LONG else -1
    obv_confirm = (obv5[-1] - obv5[max(0, n - 7)]) * sgn > 0
    touches = rng["touches_high"] if side == "high" else rng["touches_low"]
    return {
        "setup": "RANGE_EDGE", "direction": direction, "entry": entry, "stop": stop,
        "level": {"price": L, "kind": "range_high" if side == "high" else "range_low", "touches": touches,
                  "monday": False},
        "flags": {"vol_ratio": 0.0, "vol_spike": False, "obv_div": False, "obv_confirm": bool(obv_confirm)},
        "reasons": [f"Penolakan (wick) di tepi range {fmt(L)} tanpa sweep; entry limit di {fmt(entry)}."],
        "targets_extra": [],
    }


def detect_breakout_retest(h1, m5, obv5, rng, side, atr5, atr_h1, cfg):
    bc = cfg["rules"]["breakout_retest"]
    n = len(h1)
    first = n - rng["end_off"]
    L = rng["high"] if side == "high" else rng["low"]
    buf = bc["break_buffer_atr_h1"] * atr_h1
    sgn = 1 if side == "high" else -1  # arah breakout
    b = None
    for i in range(first, n):
        if (h1[i].c - L) * sgn > buf:
            b = i
            break
    if b is None or n - 1 - b > bc["max_age_h1"]:
        return None
    if any((h1[i].c - L) * sgn <= -buf for i in range(b, n)):
        return None  # breakout gagal
    if sum(1 for i in range(b, n) if (h1[i].c - L) * sgn > 0) < bc["min_accept_h1"]:
        return None
    h = rng["height"]
    tp1 = L + sgn * h * bc["tp1_range_frac"]
    tp2 = L + sgn * h * bc["tp2_range_frac"]
    extreme_since = max(h1[i].h for i in range(b, n)) if sgn == 1 else min(h1[i].l for i in range(b, n))
    if (extreme_since - tp1) * sgn >= 0:
        return None  # target sudah tercapai tanpa kita
    off = bc["entry_offset_atr5"] * atr5
    entry = L + sgn * off
    t_after = h1[b].t + H1_MS
    if any((c.l <= entry) if sgn == 1 else (c.h >= entry) for c in m5 if c.t >= t_after):
        return None  # retest pertama sudah lewat
    last = m5[-1]
    if (last.c - entry) * sgn <= 0:
        return None
    stop = L - sgn * bc["stop_atr_h1"] * atr_h1
    direction = LONG if sgn == 1 else SHORT
    obv_confirm = (obv5[-1] - obv5[max(0, len(m5) - 13)]) * sgn > 0
    touches = rng["touches_high"] if side == "high" else rng["touches_low"]
    return {
        "setup": "BREAKOUT_RETEST", "direction": direction, "entry": entry, "stop": stop,
        "level": {"price": L, "kind": "range_high" if side == "high" else "range_low", "touches": touches,
                  "monday": False},
        "flags": {"vol_ratio": 0.0, "vol_spike": False, "obv_div": False, "obv_confirm": bool(obv_confirm)},
        "reasons": [f"Breakout H1 {'ke atas' if sgn == 1 else 'ke bawah'} dari range {fmt(L)} diterima di luar; "
                    f"tunggu retest pertama di {fmt(entry)}."],
        "targets_extra": [{"price": tp1, "label": "measured move 0.5x range"},
                          {"price": tp2, "label": "measured move 1.0x range"}],
    }


def score_candidate(c, sym_season, now_weekday, cfg):
    sc = cfg["rules"]["scoring"]
    s = sc["base"][c["setup"]]
    notes = [f"dasar {c['setup']} +{s}"]
    t = c["level"]["touches"]
    if t >= 3:
        s += sc["touches_ge3_bonus"]; notes.append(f"level diuji {t}x +{sc['touches_ge3_bonus']}")
    elif t >= 2:
        s += sc["touches_ge2_bonus"]; notes.append(f"level diuji {t}x +{sc['touches_ge2_bonus']}")
    f = c["flags"]
    if c["level"]["monday"]:
        s += sc["monday_bonus"]; notes.append("level Monday +%d" % sc["monday_bonus"])
    if f["vol_spike"]:
        s += sc["vol_spike_bonus"]; notes.append("lonjakan volume saat sweep +%d" % sc["vol_spike_bonus"])
    if f["obv_div"]:
        s += sc["obv_div_bonus"]; notes.append("divergensi OBV +%d" % sc["obv_div_bonus"])
    if f["obv_confirm"]:
        s += sc["obv_confirm_bonus"]; notes.append("OBV searah +%d" % sc["obv_confirm_bonus"])
    want = "long" if c["direction"] == LONG else "short"
    if sym_season == want:
        s += sc["season_align_bonus"]; notes.append("searah musim H4 +%d" % sc["season_align_bonus"])
    elif sym_season in ("long", "short"):
        s -= sc["counter_season_penalty"]; notes.append("melawan musim H4 -%d" % sc["counter_season_penalty"])
    if c["rr1"] >= 2:
        s += sc["rr_ge2_bonus"]; notes.append("R:R>=2 +%d" % sc["rr_ge2_bonus"])
    if now_weekday == 0:
        s -= sc["monday_choppy_penalty"]; notes.append("Senin (range Monday choppy) -%d" % sc["monday_choppy_penalty"])
    return max(0, min(10, s)), notes


def generate_candidates(symbol, data, cfg, now_ms, weekday):
    """Return (kandidat[], info_pasar{}, rejects{}). data = {'5m','1h','4h'}."""
    m5, h1, h4 = data["5m"], data["1h"], data.get("4h", [])
    rejects = {}
    if len(m5) < 120 or len(h1) < 100:
        return [], {"error": "data_kurang"}, rejects
    atr5 = atr(m5[-150:], 14)
    atr_h1 = atr(h1[-120:], 14)
    if atr5 <= 0 or atr_h1 <= 0:
        return [], {"error": "atr_nol"}, rejects
    ranges = find_ranges(h1, atr_h1, cfg)
    monday = monday_levels(h1, now_ms) if cfg["rules"]["monday"]["enabled"] else None
    levels = collect_levels(ranges, monday, atr_h1)
    targets = target_levels(ranges, monday, h1, cfg["rules"]["plan"]["swing_pivot_n"])
    sym_season = season(h4)
    obv5 = obv(m5)
    raw = []
    for lv in levels:
        d = detect_deviation(m5, obv5, lv, atr5, atr_h1, cfg)
        if d:
            raw.append(d)
    for r in ranges:
        for side in ("high", "low"):
            if cfg["rules"]["range_edge"]["enabled"]:
                e = detect_range_edge(m5, obv5, r, side, atr5, atr_h1, cfg)
                if e:
                    raw.append(e)
            if cfg["rules"]["breakout_retest"]["enabled"]:
                b = detect_breakout_retest(h1, m5, obv5, r, side, atr5, atr_h1, cfg)
                if b:
                    raw.append(b)
    price = m5[-1].c
    out = []
    for c in raw:
        dist = abs(c["entry"] - price) / price * 100
        if dist > cfg["rules"]["plan"]["max_entry_distance_pct"]:
            rejects["entry_terlalu_jauh"] = rejects.get("entry_terlalu_jauh", 0) + 1
            continue
        plan, why = plan_trade(c["direction"], c["entry"], c["stop"], c["targets_extra"] + targets, cfg)
        if not plan:
            rejects[why] = rejects.get(why, 0) + 1
            continue
        c.update(plan)
        c["symbol"] = symbol
        c["score"], c["score_notes"] = score_candidate(c, sym_season, weekday, cfg)
        c["price_at_signal"] = price
        c["season"] = sym_season
        out.append(c)
    out.sort(key=lambda x: -x["score"])
    info = {
        "price": price, "atr5": atr5, "atr_h1": atr_h1, "atr_h1_pct": round(atr_h1 / price * 100, 3),
        "season": sym_season, "ranges": ranges, "monday": monday, "levels": levels,
    }
    return out, info, rejects
