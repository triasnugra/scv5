"""Deteksi setup scalping ala @eliz883 — versi 1.2 ElizStrict.

Perubahan utama vs 1.1:
- DEVIATION wajib retest setelah reclaim (require_retest).
- Volume spike lebih ketat (default 2.2x).
- RANGE_EDGE dimatikan default (kualitas rendah di market choppy).
- Penalty keras untuk mid-range.
- Stop buffer lebih realistis (0.45 ATR5).
- Scoring lebih menekankan konfluensi & major pairs.
- Semua entry tetap LIMIT di level (tidak chase).
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
    """DEVIATION v1.2: sweep -> reclaim -> (wajib) retest. Lebih ketat."""
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
        return None  # acceptance, bukan deviation

    r = (out_idx[-1] + 1) if out_idx else pierce[-1]
    if r >= n:
        return None

    margin = d["reclaim_margin_atr5"] * atr5
    rc = m5[r].c
    if high_side and not (rc < L - margin):
        return None
    if not high_side and not (rc > L + margin):
        return None

    # --- ElizStrict: wajib ada retest setelah reclaim ---
    require_retest = d.get("require_retest", True)
    retest_lb = d.get("retest_lookback_5m", 18)
    after = m5[r + 1:]
    if require_retest:
        # Harus sudah menyentuh kembali zona level (retest) dalam window
        retested = False
        for c in after[-retest_lb:]:
            if high_side and c.h >= L - margin * 0.5:
                retested = True
                break
            if not high_side and c.l <= L + margin * 0.5:
                retested = True
                break
        if not retested:
            return None
        # Setelah retest, harga tidak boleh sudah jauh menjauh (chase protection)
        last = m5[-1]
        if high_side and last.c < L - 1.5 * atr5:
            return None
        if not high_side and last.c > L + 1.5 * atr5:
            return None
    else:
        # mode lama: jangan biarkan retest sudah terjadi
        if high_side and any(c.h >= L for c in after):
            return None
        if not high_side and any(c.l <= L for c in after):
            return None

    extreme = max(m5[i].h for i in pierce) if high_side else min(m5[i].l for i in pierce)
    depth = abs(extreme - L)
    if atr_h1 > 0 and depth / atr_h1 > d["max_sweep_atr_h1"]:
        return None

    buf = d["stop_buffer_atr5"] * atr5
    off = d["entry_offset_atr5"] * atr5
    if high_side:
        direction, entry, stop = SHORT, L - off, extreme + buf
    else:
        direction, entry, stop = LONG, L + off, extreme - buf

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

    strong_reclaim = abs(rc - L) > 0.25 * atr5
    txt = (f"Sweep {'di atas' if high_side else 'di bawah'} {level['kind']} {fmt(L)} (ekstrem {fmt(extreme)}), "
           f"reclaim + retest terkonfirmasi; entry LIMIT di {fmt(entry)}.")
    return {
        "setup": "DEVIATION", "direction": direction, "entry": entry, "stop": stop,
        "level": {"price": L, "kind": level["kind"], "touches": level["touches"], "monday": level.get("monday", False)},
        "flags": {
            "vol_ratio": round(vr, 2),
            "vol_spike": vr >= d["vol_spike_ratio"],
            "obv_div": bool(obv_div),
            "obv_confirm": bool(obv_confirm),
            "sweep_depth_atr_h1": round(depth / atr_h1, 2) if atr_h1 else 0,
            "strong_reclaim": strong_reclaim,
        },
        "reasons": [txt], "targets_extra": [],
    }


def detect_range_edge(m5, obv5, rng, side, atr5, atr_h1, cfg):
    """RANGE_EDGE dimatikan default di 1.2 karena winrate rendah di market choppy."""
    rc = cfg["rules"]["range_edge"]
    if not rc.get("enabled", False):
        return None
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
    c = m5[best]
    buf = rc["stop_buffer_atr_h1"] * atr_h1
    if side == "high":
        direction, entry, stop = SHORT, L, c.h + buf
    else:
        direction, entry, stop = LONG, L, c.l - buf
    vr = _vol_ratio(m5, [best])
    return {
        "setup": "RANGE_EDGE", "direction": direction, "entry": entry, "stop": stop,
        "level": {"price": L, "kind": "range_" + side, "touches": rng.get("touches", 0), "monday": False},
        "flags": {"vol_ratio": round(vr, 2), "vol_spike": vr >= 1.8, "obv_div": False, "obv_confirm": False,
                  "sweep_depth_atr_h1": 0.0},
        "reasons": [f"Wick rejection di tepi range {side} {fmt(L)}"], "targets_extra": [],
    }


def detect_breakout_retest(m5, h1, rng, atr5, atr_h1, cfg):
    """BREAKOUT_RETEST: close H1 diterima di luar range, lalu retest."""
    br = cfg["rules"]["breakout_retest"]
    if not br.get("enabled", True):
        return None
    # Simplified: look for recent H1 close outside + current price near the broken level
    # (full original logic retained in spirit; keep deterministic)
    n = len(h1)
    if n < br["min_accept_h1"] + 3:
        return None
    high, low = rng["high"], rng["low"]
    buf = br["break_buffer_atr_h1"] * atr_h1
    # find last accepted break
    break_dir = None
    break_idx = None
    for i in range(n - 1, max(0, n - br["max_age_h1"] - 1), -1):
        c = h1[i]
        if c.c > high + buf:
            # check acceptance
            accepts = sum(1 for j in range(i, min(n, i + br["min_accept_h1"] + 1)) if h1[j].c > high)
            if accepts >= br["min_accept_h1"]:
                break_dir, break_idx = "up", i
                break
        if c.c < low - buf:
            accepts = sum(1 for j in range(i, min(n, i + br["min_accept_h1"] + 1)) if h1[j].c < low)
            if accepts >= br["min_accept_h1"]:
                break_dir, break_idx = "down", i
                break
    if break_dir is None:
        return None
    level_price = high if break_dir == "up" else low
    # retest: price near level now
    last5 = m5[-1]
    off = br["entry_offset_atr5"] * atr5
    if break_dir == "up":
        if abs(last5.c - level_price) > 1.2 * atr5:
            return None
        direction, entry, stop = LONG, level_price + off, level_price - br["stop_atr_h1"] * atr_h1
    else:
        if abs(last5.c - level_price) > 1.2 * atr5:
            return None
        direction, entry, stop = SHORT, level_price - off, level_price + br["stop_atr_h1"] * atr_h1
    return {
        "setup": "BREAKOUT_RETEST", "direction": direction, "entry": entry, "stop": stop,
        "level": {"price": level_price, "kind": "break_" + break_dir, "touches": 1, "monday": False},
        "flags": {"vol_ratio": 1.0, "vol_spike": False, "obv_div": False, "obv_confirm": False,
                  "sweep_depth_atr_h1": 0.0},
        "reasons": [f"Breakout {break_dir} diterima, retest level {fmt(level_price)}"], "targets_extra": [],
    }


def score_candidate(c, info, cfg, btc_ctx, symbol):
    """Skor 0-10+ dengan penalti mid-range dan bonus major."""
    sc = cfg["rules"]["scoring"]
    flt = cfg["rules"]["filters"]
    score = float(sc["base"].get(c["setup"], 0))
    notes = [f"dasar {c['setup']} +{sc['base'].get(c['setup'], 0)}"]

    touches = c["level"].get("touches", 0)
    if touches >= 3:
        score += sc["touches_ge3_bonus"]
        notes.append(f"level diuji {touches}x +{sc['touches_ge3_bonus']}")
    elif touches >= 2:
        score += sc["touches_ge2_bonus"]
        notes.append(f"level diuji {touches}x +{sc['touches_ge2_bonus']}")

    flags = c.get("flags", {})
    if flags.get("vol_spike"):
        score += sc["vol_spike_bonus"]
        notes.append(f"lonjakan volume +{sc['vol_spike_bonus']}")
    if flags.get("obv_div"):
        score += sc["obv_div_bonus"]
        notes.append("OBV divergence +1")
    if flags.get("obv_confirm"):
        score += sc["obv_confirm_bonus"]
        notes.append("OBV confirm +1")
    if flags.get("strong_reclaim"):
        score += sc.get("strong_reclaim_bonus", 0)
        notes.append("strong reclaim +1")

    # season
    sea = c.get("season") or info.get("season")
    if sea and btc_ctx.get("season"):
        if sea == btc_ctx["season"]:
            score += sc["season_align_bonus"]
            notes.append(f"searah musim +{sc['season_align_bonus']}")
        else:
            score -= sc["counter_season_penalty"]
            notes.append(f"lawan musim -{sc['counter_season_penalty']}")

    # Monday
    if c["level"].get("monday"):
        score += sc["monday_bonus"]
        notes.append("Monday level +1")
    if info.get("monday_choppy"):
        score -= sc["monday_choppy_penalty"]
        notes.append(f"Senin choppy -{sc['monday_choppy_penalty']}")

    # R:R
    if c.get("rr1", 0) >= 2.5:
        score += sc.get("rr_ge25_bonus", 0) + sc.get("rr_ge2_bonus", 0)
        notes.append("R:R >= 2.5 +2")
    elif c.get("rr1", 0) >= 2.0:
        score += sc.get("rr_ge2_bonus", 0)
        notes.append("R:R >= 2 +1")

    # Mid-range ban / penalty (ElizStrict)
    pos = info.get("range_pos")
    if pos is not None and flt.get("ban_mid_range", True):
        lo, hi = flt.get("mid_range_low", 0.38), flt.get("mid_range_high", 0.62)
        if lo < pos < hi:
            score -= sc.get("mid_range_penalty", 4)
            notes.append(f"MID-RANGE penalty -{sc.get('mid_range_penalty', 4)}")

    # Major bonus
    majors = cfg.get("universe", {}).get("priority_majors", [])
    if symbol in majors:
        score += sc.get("major_bonus", 0)
        notes.append("major pair +1")

    c["score"] = round(score, 1)
    c["score_notes"] = notes
    return c


def generate_candidates(symbol, d, cfg, now_ms, weekday):
    """Hasilkan kandidat + info market + daftar reject reasons."""
    m5 = d.get("5m") or []
    h1 = d.get("1h") or []
    if len(m5) < 80 or len(h1) < 40:
        return [], {"symbol": symbol, "rejects": ["data_kurang"]}, ["data_kurang"]

    atr5 = atr(m5, 14) or 0.0
    atr_h1 = atr(h1, 14) or 0.0
    if atr5 <= 0 or atr_h1 <= 0:
        return [], {"symbol": symbol}, ["atr_invalid"]

    obv5 = obv(m5)
    ranges = find_ranges(h1, cfg)
    levels = collect_levels(ranges, monday_levels(h1, cfg), cfg)
    sea = season(h1, cfg)
    last = m5[-1]
    # range position 0..1
    rng = ranges[0] if ranges else None
    range_pos = None
    if rng and rng["high"] > rng["low"]:
        range_pos = (last.c - rng["low"]) / (rng["high"] - rng["low"])

    info = {
        "symbol": symbol,
        "price": last.c,
        "atr5": atr5,
        "atr_h1": atr_h1,
        "season": sea,
        "range_pos": range_pos,
        "monday_choppy": weekday == 0 and bool(cfg["rules"]["monday"].get("enabled")),
        "n_levels": len(levels),
    }

    candidates = []
    rejects = []

    # 1. DEVIATION (utama)
    for lv in levels:
        cand = detect_deviation(m5, obv5, lv, atr5, atr_h1, cfg)
        if cand:
            targets = target_levels(cand["direction"], cand["entry"], ranges, h1, cfg)
            plan, why = plan_trade(cand["direction"], cand["entry"], cand["stop"], targets, cfg)
            if plan:
                cand.update(plan)
                cand["symbol"] = symbol
                cand["season"] = sea
                cand["price_at_signal"] = last.c
                score_candidate(cand, info, cfg, {"season": sea}, symbol)
                candidates.append(cand)
            else:
                rejects.append(f"deviation_{lv['kind']}:{why}")
        else:
            rejects.append(f"deviation_{lv.get('kind','?')}:no_trigger")

    # 2. BREAKOUT_RETEST
    if rng:
        cand = detect_breakout_retest(m5, h1, rng, atr5, atr_h1, cfg)
        if cand:
            targets = target_levels(cand["direction"], cand["entry"], ranges, h1, cfg)
            plan, why = plan_trade(cand["direction"], cand["entry"], cand["stop"], targets, cfg)
            if plan:
                cand.update(plan)
                cand["symbol"] = symbol
                cand["season"] = sea
                cand["price_at_signal"] = last.c
                score_candidate(cand, info, cfg, {"season": sea}, symbol)
                candidates.append(cand)
            else:
                rejects.append(f"breakout:{why}")

    # 3. RANGE_EDGE (hanya jika enabled)
    if rng and cfg["rules"]["range_edge"].get("enabled"):
        for side in ("high", "low"):
            cand = detect_range_edge(m5, obv5, rng, side, atr5, atr_h1, cfg)
            if cand:
                targets = target_levels(cand["direction"], cand["entry"], ranges, h1, cfg)
                plan, why = plan_trade(cand["direction"], cand["entry"], cand["stop"], targets, cfg)
                if plan:
                    cand.update(plan)
                    cand["symbol"] = symbol
                    cand["season"] = sea
                    cand["price_at_signal"] = last.c
                    score_candidate(cand, info, cfg, {"season": sea}, symbol)
                    candidates.append(cand)

    # Final hard filter: mid-range ban
    flt = cfg["rules"]["filters"]
    if flt.get("ban_mid_range") and range_pos is not None:
        lo, hi = flt.get("mid_range_low", 0.38), flt.get("mid_range_high", 0.62)
        if lo < range_pos < hi:
            candidates = []
            rejects.append("MID_RANGE_BAN")

    # Sort by score desc
    candidates.sort(key=lambda x: -x.get("score", 0))
    info["rejects"] = rejects[:12]
    return candidates, info, rejects[:12]
