"""Seleksi sinyal & pembuatan record SIGNAL.

Dipakai BERSAMA oleh engine (live) dan backtest, sehingga aturan seleksi tidak bisa menyimpang antara keduanya
(masalah klasik: backtest memakai logika berbeda dari live).
"""
from datetime import datetime, timezone

from .journal import M5_MS
from .risk import OPEN_STATUSES


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def choose_signals(pool, sigs, flt, now_ms, shock=False, blocked=None):
    """pool: list[(kandidat, info, data)]. sigs: hasil replay. Return list terpilih.

    blocked: daftar alasan global (batas risiko). Tidak kosong -> tidak ada sinyal baru.
    """
    if shock or blocked:
        return []
    allow_short = flt.get("allow_short", True)
    pool = [p for p in pool if p[0]["score"] >= flt["min_score"]]
    if not allow_short:
        pool = [p for p in pool if p[0]["direction"] == "LONG"]
    active_syms = {s["symbol"] for s in sigs.values() if s["status"] in OPEN_STATUSES}
    last_sig_ts = {}
    for s in sigs.values():
        last_sig_ts[s["symbol"]] = max(last_sig_ts.get(s["symbol"], 0), s["created_ts"])
    hour_count = sum(1 for s in sigs.values() if now_ms - s["created_ts"] < 3_600_000)
    open_total = len(active_syms)
    max_open = flt.get("max_open_total")
    pool = sorted(pool, key=lambda p: -p[0]["score"])
    chosen, seen = [], set()
    for c, info, d in pool:
        sym = c["symbol"]
        if sym in active_syms or sym in seen:
            continue
        if now_ms - last_sig_ts.get(sym, 0) < flt["symbol_cooldown_min"] * 60_000:
            continue
        if hour_count + len(chosen) >= flt["max_signals_per_hour"]:
            break
        if max_open is not None and open_total + len(chosen) >= max_open:
            break
        chosen.append((c, info, d))
        seen.add(sym)
    return chosen


def build_record(c, d, now_ms, cfg, btc_ctx, sizing=None):
    """Record SIGNAL yang disegel ke jurnal. `c['ai']` harus sudah diisi pemanggil."""
    last = d["5m"][-1]
    sid = f"{c['symbol']}-{iso(now_ms).replace(':', '').replace('-', '')}-{c['setup'][:3]}"
    return {
        "id": sid, "symbol": c["symbol"], "setup": c["setup"], "direction": c["direction"],
        "entry": c["entry"], "stop": c["stop"], "tp1": c["tp1"], "tp2": c["tp2"],
        "tp1_label": c["tp1_label"], "tp2_label": c["tp2_label"],
        "risk_pct": c["risk_pct"], "rr1": c["rr1"], "rr2": c["rr2"], "cost_r": c["cost_r"],
        "score": c["score"], "score_notes": c["score_notes"], "level": c["level"], "flags": c["flags"],
        "reasons": c["reasons"], "season": c["season"], "price_at_signal": c["price_at_signal"],
        "created_ts": now_ms, "created_iso": iso(now_ms),
        "sim_from_ts": last.t + M5_MS,
        "expiry_ts": now_ms + cfg["sim"]["expiry_min"] * 60_000,
        "strategy_hash": cfg["_hash"], "strategy_version": cfg["strategy_version"],
        "btc": {k: btc_ctx.get(k) for k in ("season", "pos", "label")},
        "ai": c.get("ai", {"verdict": "N/A"}),
        "sizing": sizing,
    }
