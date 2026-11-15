"""Orkestrator: ambil data -> nilai sinyal lama -> cari setup baru -> review Claude -> catat jurnal -> tulis JSON dashboard."""
import json
import os
import time
from datetime import datetime, timezone

from . import ai as ai_mod
from .context import build_btc_context, position_label, range_position, season
from .data import Provider
from .journal import M5_MS, Journal, replay, simulate
from .setups import generate_candidates
from .stats import build_stats


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _market_note(info, has_setup):
    if has_setup:
        return "SETUP", "Ada kandidat setup; lihat daftar sinyal."
    rngs = info.get("ranges") or []
    if not rngs:
        return "TANPA_RANGE", "Tidak ada range H1 valid (trending / belum terbentuk). Tidak ada keunggulan: tunggu."
    pos = range_position(info["price"], rngs[0])
    lab = position_label(pos)
    if lab == "mid_no_mans_land":
        return "TENGAH_RANGE", "Di tengah range (no-man's-land). Tidak mengejar harga; tunggu sweep/trigger di tepi."
    if lab in ("range_top", "range_bottom"):
        return "DEKAT_TEPI", "Dekat tepi range. Tunggu sweep + reclaim atau penolakan jelas."
    return "MENUNGGU", "Di antara tepi dan tengah range; belum ada trigger."


def run(cfg, journal_path, out_dir, provider=None, now_ms=None, log=print):
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    rt = cfg["runtime"]
    shash = cfg["_hash"]
    j = Journal(journal_path)
    ok, msg = j.verify()
    if not ok:
        raise RuntimeError(f"INTEGRITAS JURNAL GAGAL: {msg}")
    provider = provider or Provider(rt["data"]["sources"])
    symbols = cfg["universe"]["symbols"]
    ctx_sym = cfg["universe"]["context_symbol"]
    if ctx_sym not in symbols:
        symbols = [ctx_sym] + symbols

    # 1) data
    data, errors = {}, []
    for s in symbols:
        try:
            data[s] = {
                "5m": provider.get(s, "5m", rt["data"]["limit_5m"], now_ms),
                "1h": provider.get(s, "1h", rt["data"]["limit_1h"], now_ms),
                "4h": provider.get(s, "4h", rt["data"]["limit_4h"], now_ms),
            }
        except Exception as e:  # noqa: BLE001
            errors.append(f"{s}: {e}")
            log(f"[data] {s} dilewati: {e}")

    # 2) nilai sinyal lama (simulasi ulang deterministik, tambahkan hanya event baru)
    sigs = replay(j.records)
    new_events = 0
    for sid, s in list(sigs.items()):
        if s["status"] in ("CLOSED", "EXPIRED") or s["symbol"] not in data:
            continue
        m5 = data[s["symbol"]]["5m"]
        if m5 and m5[0].t > s["sim_from_ts"] and s["status"] in ("PENDING", "OPEN", "OPEN_TP1"):
            # data tidak lagi mencakup awal sinyal: celah data
            j.append("EXPIRE" if s["status"] == "PENDING" else "CLOSE",
                     {"id": sid, "ts": now_ms, "reason": "DATA_GAP", "r_net": 0.0, "r_gross": 0.0}, now_ms)
            new_events += 1
            continue
        have = {"PENDING": 0, "OPEN": 1, "OPEN_TP1": 2}[s["status"]]
        for typ, d in simulate(s, m5, now_ms, cfg["sim"]):
            rank = {"FILL": 1, "TP1": 2}.get(typ, 3)
            if rank > have:
                j.append(typ, d, now_ms)
                new_events += 1
                have = rank
    sigs = replay(j.records)

    # 3) konteks BTC
    btc_ctx = build_btc_context(data.get(ctx_sym), cfg)

    # 4) cari kandidat baru
    weekday = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).weekday()
    flt = cfg["rules"]["filters"]
    active_syms = {s["symbol"] for s in sigs.values() if s["status"] in ("PENDING", "OPEN", "OPEN_TP1")}
    market, pool = {}, []
    for sym, d in data.items():
        cands, info, rejects = generate_candidates(sym, d, cfg, now_ms, weekday)
        info = dict(info)
        info["rejects"] = rejects
        market[sym] = (info, d)
        for c in cands:
            pool.append((c, info, d))
    pool = [p for p in pool if p[0]["score"] >= flt["min_score"]]
    if btc_ctx.get("shock"):
        log("[filter] BTC shock: tidak ada sinyal baru")
        pool = []
    last_sig_ts = {}
    for s in sigs.values():
        last_sig_ts[s["symbol"]] = max(last_sig_ts.get(s["symbol"], 0), s["created_ts"])
    hour_count = sum(1 for s in sigs.values() if now_ms - s["created_ts"] < 3_600_000)
    pool.sort(key=lambda p: -p[0]["score"])
    chosen, seen = [], set()
    for c, info, d in pool:
        sym = c["symbol"]
        if sym in active_syms or sym in seen:
            continue
        if now_ms - last_sig_ts.get(sym, 0) < flt["symbol_cooldown_min"] * 60_000:
            continue
        if hour_count + len(chosen) >= flt["max_signals_per_hour"]:
            break
        chosen.append((c, info, d))
        seen.add(sym)

    # 5) review Claude (opsional)
    key = ai_mod.get_key()
    ai_on = bool(rt["ai"]["enabled"] and key)
    reviews = 0
    for c, info, d in chosen:
        c["ai"] = {"verdict": "N/A"}
        if ai_on and reviews < rt["ai"]["max_reviews_per_run"]:
            try:
                c["ai"] = ai_mod.review(c, info, btc_ctx, d, key, os.environ.get("ANTHROPIC_MODEL") or rt["ai"]["model"])
                reviews += 1
            except Exception as e:  # noqa: BLE001
                log(f"[ai] review gagal {c['symbol']}: {e}")
                c["ai"] = {"verdict": "N/A", "error": str(e)[:120]}

    # 6) catat sinyal baru
    for c, info, d in chosen:
        last = d["5m"][-1]
        sid = f"{c['symbol']}-{iso(now_ms).replace(':', '').replace('-', '')}-{c['setup'][:3]}"
        rec = {
            "id": sid, "symbol": c["symbol"], "setup": c["setup"], "direction": c["direction"],
            "entry": c["entry"], "stop": c["stop"], "tp1": c["tp1"], "tp2": c["tp2"],
            "tp1_label": c["tp1_label"], "tp2_label": c["tp2_label"],
            "risk_pct": c["risk_pct"], "rr1": c["rr1"], "rr2": c["rr2"], "cost_r": c["cost_r"],
            "score": c["score"], "score_notes": c["score_notes"], "level": c["level"], "flags": c["flags"],
            "reasons": c["reasons"], "season": c["season"], "price_at_signal": c["price_at_signal"],
            "created_ts": now_ms, "created_iso": iso(now_ms),
            "sim_from_ts": last.t + M5_MS,
            "expiry_ts": now_ms + cfg["sim"]["expiry_min"] * 60_000,
            "strategy_hash": shash, "strategy_version": cfg["strategy_version"],
            "btc": {k: btc_ctx.get(k) for k in ("season", "pos", "label")},
            "ai": c["ai"],
        }
        j.append("SIGNAL", rec, now_ms)
        log(f"[sinyal] {sid} {c['direction']} entry={c['entry']:.6g} sl={c['stop']:.6g} "
            f"tp1={c['tp1']:.6g} skor={c['score']} AI={c['ai'].get('verdict')}")
    j.flush()
    sigs = replay(j.records)

    # 7) keluaran dashboard
    chosen_syms = {c["symbol"] for c, _, _ in chosen}
    mk = {}
    for sym, (info, d) in market.items():
        state, note = _market_note(info, sym in chosen_syms or sym in active_syms) if "error" not in info else ("DATA", info["error"])
        r0 = (info.get("ranges") or [None])[0]
        pos = range_position(info["price"], r0) if r0 and "price" in info else None
        mk[sym] = {"state": state, "note": note, "price": info.get("price"), "season": info.get("season"),
                   "atr_h1_pct": info.get("atr_h1_pct"), "range": r0,
                   "range_pos": None if pos is None else round(pos, 3), "monday": info.get("monday"),
                   "rejects": info.get("rejects", {})}
    stats = build_stats(sigs, shash, now_ms, cfg["protocol"])
    mine = [s for s in sigs.values() if s["strategy_hash"] == shash]
    active = sorted([s for s in mine if s["status"] in ("PENDING", "OPEN", "OPEN_TP1")], key=lambda s: -s["created_ts"])
    recent = sorted([s for s in mine if s["status"] in ("CLOSED", "EXPIRED")], key=lambda s: -(s["close_ts"] or 0))[:60]
    ok2, msg2 = j.verify()
    _write_json(os.path.join(out_dir, "state.json"), {
        "generated_at": iso(now_ms), "generated_ts": now_ms,
        "strategy": {"version": cfg["strategy_version"], "hash": shash, "protocol": cfg["protocol"]},
        "integrity": {"ok": ok2, "message": msg2, "records": len(j.records), "head": j.last_hash},
        "stats": stats, "active": active, "recent": recent,
        "ai": {"enabled": ai_on, "model": os.environ.get("ANTHROPIC_MODEL") or rt["ai"]["model"]},
        "data_sources": provider.used if hasattr(provider, "used") else {}, "errors": errors[:10],
    })
    brief_text = _maybe_brief(out_dir, mk, btc_ctx, ai_on, key, rt, now_ms, log)
    _write_json(os.path.join(out_dir, "market.json"), {
        "generated_at": iso(now_ms), "btc": {k: btc_ctx.get(k) for k in ("season", "pos", "label", "shock", "price")},
        "symbols": mk, "brief": brief_text,
    })
    return {"new_signals": len(chosen), "new_events": new_events, "errors": errors, "symbols": len(data)}


def _maybe_brief(out_dir, mk, btc_ctx, ai_on, key, rt, now_ms, log):
    path = os.path.join(out_dir, "brief.json")
    prev = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                prev = json.load(f)
        except ValueError:
            prev = None
    if not ai_on:
        return prev
    if prev and now_ms - prev.get("ts", 0) < rt["ai"]["brief_interval_min"] * 60_000:
        return prev
    summary = {"btc": {k: btc_ctx.get(k) for k in ("season", "pos", "label", "shock", "price")},
               "simbol": {s: {k: v[k] for k in ("state", "season", "range_pos", "atr_h1_pct")} for s, v in mk.items()}}
    try:
        text = ai_mod.brief(summary, key, os.environ.get("ANTHROPIC_MODEL") or rt["ai"]["model"])
        out = {"ts": now_ms, "iso": iso(now_ms), "text": text}
        _write_json(path, out)
        return out
    except Exception as e:  # noqa: BLE001
        log(f"[ai] brief gagal: {e}")
        return prev
