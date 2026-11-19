"""Orkestrator: ambil data -> cek kualitas data -> nilai sinyal lama -> cek batas risiko -> cari setup baru
-> review Claude -> catat jurnal (+ukuran posisi) -> tulis JSON dashboard."""
import json
import os
import time
from datetime import datetime, timezone

from . import ai as ai_mod
from .context import build_btc_context, position_label, range_position, season
from .data import Provider
from .dataq import check_candles
from .journal import M5_MS, Journal, replay, simulate
from .risk import OPEN_STATUSES, gate_reasons, position_plan, risk_state
from .setups import generate_candidates
from .signals import build_record, choose_signals, iso
from .stats import build_stats


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


def load_market_data(provider, symbols, rt, rules, now_ms, log):
    """Ambil 5m/1h/4h per simbol + saring dengan cek kualitas data. Return (data, errors)."""
    dq = rules.get("data_quality")
    data, errors = {}, []
    for s in symbols:
        try:
            d = {
                "5m": provider.get(s, "5m", rt["data"]["limit_5m"], now_ms),
                "1h": provider.get(s, "1h", rt["data"]["limit_1h"], now_ms),
                "4h": provider.get(s, "4h", rt["data"]["limit_4h"], now_ms),
            }
        except Exception as e:  # noqa: BLE001
            errors.append(f"{s}: {e}")
            log(f"[data] {s} dilewati: {e}")
            continue
        bad = []
        for iv, cs in d.items():
            bad += [f"{iv}:{r}" for r in check_candles(cs, iv, now_ms, dq)]
        if bad:
            errors.append(f"{s}: DATAQ {', '.join(bad)}")
            log(f"[dataq] {s} dilewati: {', '.join(bad)}")
            continue
        data[s] = d
    return data, errors


def run(cfg, journal_path, out_dir, provider=None, now_ms=None, log=print):
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    rt, rules = cfg["runtime"], cfg["rules"]
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

    # 1) data (+ kualitas)
    data, errors = load_market_data(provider, symbols, rt, rules, now_ms, log)

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

    # 3) status protokol + batas risiko (diturunkan dari jurnal)
    stats_now = build_stats(sigs, shash, now_ms, cfg["protocol"])
    verdict = stats_now["protocol"]["verdict"]
    limits = rules.get("risk_limits")
    rstate = risk_state(sigs, shash, now_ms, limits)
    blocked = gate_reasons(rstate, limits, now_ms)
    if blocked:
        log(f"[risiko] sinyal baru ditahan: {', '.join(blocked)}")

    # 4) konteks BTC
    btc_ctx = build_btc_context(data.get(ctx_sym), cfg)

    # 5) cari kandidat baru
    weekday = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).weekday()
    flt = rules["filters"]
    market, pool = {}, []
    for sym, d in data.items():
        cands, info, rejects = generate_candidates(sym, d, cfg, now_ms, weekday)
        info = dict(info)
        info["rejects"] = rejects
        market[sym] = (info, d)
        for c in cands:
            pool.append((c, info, d))
    if btc_ctx.get("shock"):
        log("[filter] BTC shock: tidak ada sinyal baru")
    chosen = choose_signals(pool, sigs, flt, now_ms, shock=bool(btc_ctx.get("shock")), blocked=blocked, cfg=cfg)
    active_syms = {s["symbol"] for s in sigs.values() if s["status"] in OPEN_STATUSES}

    # 6) review Claude (opsional)
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

    # 7) catat sinyal baru (+ ukuran posisi)
    account = rt.get("account")
    for c, info, d in chosen:
        sizing = position_plan(c["entry"], c["stop"], account, verdict)
        rec = build_record(c, d, now_ms, cfg, btc_ctx, sizing)
        j.append("SIGNAL", rec, now_ms)
        log(f"[sinyal] {rec['id']} {c['direction']} entry={c['entry']:.6g} sl={c['stop']:.6g} "
            f"tp1={c['tp1']:.6g} skor={c['score']} AI={c['ai'].get('verdict')}")
    j.flush()
    sigs = replay(j.records)

    # 8) keluaran dashboard
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
    active = sorted([s for s in mine if s["status"] in OPEN_STATUSES], key=lambda s: -s["created_ts"])
    recent = sorted([s for s in mine if s["status"] in ("CLOSED", "EXPIRED")], key=lambda s: -(s["close_ts"] or 0))[:60]
    ok2, msg2 = j.verify()
    _write_json(os.path.join(out_dir, "state.json"), {
        "generated_at": iso(now_ms), "generated_ts": now_ms,
        "strategy": {"version": cfg["strategy_version"], "hash": shash, "protocol": cfg["protocol"]},
        "integrity": {"ok": ok2, "message": msg2, "records": len(j.records), "head": j.last_hash},
        "risk": {"state": risk_state(sigs, shash, now_ms, limits), "halted": blocked, "limits": limits},
        "stats": stats, "active": active, "recent": recent,
        "ai": {"enabled": ai_on, "model": os.environ.get("ANTHROPIC_MODEL") or rt["ai"]["model"]},
        "data_sources": provider.used if hasattr(provider, "used") else {}, "errors": errors[:10],
    })
    brief_text = _maybe_brief(out_dir, mk, btc_ctx, ai_on, key, rt, now_ms, log)
    _write_json(os.path.join(out_dir, "market.json"), {
        "generated_at": iso(now_ms), "btc": {k: btc_ctx.get(k) for k in ("season", "pos", "label", "shock", "price")},
        "symbols": mk, "brief": brief_text,
    })
    return {"new_signals": len(chosen), "new_events": new_events, "errors": errors,
            "symbols": len(data), "risk_halt": blocked}


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
