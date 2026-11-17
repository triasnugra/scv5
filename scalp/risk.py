"""Lapisan risiko: batas harian, jeda setelah loss beruntun, jeda drawdown, ukuran posisi.

Diport dari konsep scc/risk.gate + scgrok/riskPlan, versi stdlib dan DETERMINISTIK: seluruh state diturunkan
dari jurnal (replay), bukan dari file terpisah, sehingga ikut tamper-evident dan identik di live maupun backtest.

Batas (rules.risk_limits) ikut di-hash strategi karena mengubah sinyal mana yang tercatat.
Ukuran posisi (runtime.account) TIDAK di-hash: tidak mengubah R, hanya konversi ke uang.
"""

DAY_MS = 86_400_000
OPEN_STATUSES = ("PENDING", "OPEN", "OPEN_TP1")


def _closed(sigs, shash):
    out = [s for s in sigs.values()
           if s.get("strategy_hash") == shash and s.get("status") == "CLOSED"
           and s.get("reason") != "DATA_GAP" and s.get("r_net") is not None and s.get("close_ts")]
    out.sort(key=lambda s: s["close_ts"])
    return out


def risk_state(sigs, shash, now_ms, limits):
    """Ringkasan kondisi risiko saat ini (dict). `limits` None = hanya statistik, tanpa batas."""
    closed = _closed(sigs, shash)
    day0 = now_ms - (now_ms % DAY_MS)
    r_today = sum(s["r_net"] for s in closed if s["close_ts"] >= day0)

    streak, last_loss_ts = 0, None
    for s in reversed(closed):
        if s["r_net"] < 0:
            streak += 1
            last_loss_ts = last_loss_ts or s["close_ts"]
        else:
            break

    win_days = (limits or {}).get("dd_window_days", 7)
    since = now_ms - win_days * DAY_MS
    eq, peak, dd = 0.0, 0.0, 0.0
    for s in closed:
        if s["close_ts"] < since:
            continue
        eq += s["r_net"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    mine = [s for s in sigs.values() if s.get("strategy_hash") == shash]
    return {
        "r_today": round(r_today, 3),
        "loss_streak": streak,
        "last_loss_ts": last_loss_ts,
        "last_close_ts": closed[-1]["close_ts"] if closed else None,
        "drawdown_r_window": round(dd, 3),
        "open_signals": sum(1 for s in mine if s["status"] in OPEN_STATUSES),
        "signals_today": sum(1 for s in mine if s["created_ts"] >= day0),
    }


def gate_reasons(state, limits, now_ms):
    """Alasan global untuk TIDAK membuka sinyal baru. Kosong = boleh."""
    if not limits:
        return []
    why = []
    if state["r_today"] <= -limits.get("max_daily_loss_r", 3.0):
        why.append(f"BATAS_RUGI_HARIAN({state['r_today']}R)")
    n = limits.get("loss_streak_pause", 3)
    cool = limits.get("loss_streak_cooldown_min", 120) * 60_000
    if state["loss_streak"] >= n and state["last_loss_ts"] and now_ms - state["last_loss_ts"] < cool:
        why.append(f"JEDA_LOSS_BERUNTUN({state['loss_streak']}x)")
    pause = limits.get("dd_pause_hours", 24) * 3_600_000
    if (state["drawdown_r_window"] >= limits.get("dd_pause_r", 8.0) and state["last_close_ts"]
            and now_ms - state["last_close_ts"] < pause):
        why.append(f"JEDA_DRAWDOWN({state['drawdown_r_window']}R)")
    if state["signals_today"] >= limits.get("max_signals_per_day", 6):
        why.append("BATAS_SINYAL_HARIAN")
    return why


def position_plan(entry, stop, account, protocol_verdict):
    """Ukuran posisi dari risiko. Risiko dibatasi kecil sampai protokol forward-test LULUS.

    Return dict atau None bila input tidak valid. Bila leverage melebihi batas, qty DIKECILKAN (risiko turun).
    """
    if not account or entry <= 0:
        return None
    dist = abs(entry - stop)
    if dist <= 0:
        return None
    equity = float(account["equity_usd"])
    passed = protocol_verdict == "LULUS"
    risk_pct = float(account["risk_pct_post_protocol"] if passed else account["risk_pct_pre_protocol"])
    risk_usd = equity * risk_pct / 100.0
    qty = risk_usd / dist
    lev = qty * entry / equity if equity > 0 else 0.0
    capped = False
    max_lev = float(account.get("max_leverage", 1))
    if lev > max_lev:
        f = max_lev / lev
        qty, risk_usd, lev, capped = qty * f, risk_usd * f, max_lev, True
    return {
        "risk_pct": round(risk_pct * (risk_usd / (equity * risk_pct / 100.0)), 4),
        "risk_usd": round(risk_usd, 4),
        "qty": float(f"{qty:.8g}"),
        "notional_usd": round(qty * entry, 2),
        "leverage": round(lev, 3),
        "leverage_capped": capped,
        "protocol_passed": passed,
    }
