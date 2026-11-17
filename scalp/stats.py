"""Statistik forward-test: win rate (Wilson), profit factor, expectancy (R) + CI, SQN (Van Tharp), drawdown."""
import math
import random


def wilson(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def summarize(rs):
    n = len(rs)
    if n == 0:
        return {"n": 0, "wins": 0, "win_rate": None, "wr_ci": [None, None], "expectancy": None, "exp_ci": [None, None],
                "profit_factor": None, "sqn": None, "sum_r": 0.0, "max_dd_r": 0.0, "avg_win": None, "avg_loss": None}
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else 0.0
    gl = abs(sum(losses))
    pf = (sum(wins) / gl) if gl > 0 else (None if not wins else float("inf"))
    sqn = (math.sqrt(min(n, 100)) * mean / sd) if sd > 0 else None
    ci = [mean - 1.96 * sd / math.sqrt(n), mean + 1.96 * sd / math.sqrt(n)] if n > 1 else [None, None]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in rs:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    lo, hi = wilson(len(wins), n)
    return {
        "n": n, "wins": len(wins), "win_rate": round(len(wins) / n, 4), "wr_ci": [round(lo, 4), round(hi, 4)],
        "expectancy": round(mean, 4), "exp_ci": [None if v is None else round(v, 4) for v in ci],
        "profit_factor": None if pf is None else (round(pf, 3) if pf != float("inf") else 999.0),
        "sqn": None if sqn is None else round(sqn, 3), "sum_r": round(sum(rs), 3), "max_dd_r": round(dd, 3),
        "avg_win": round(sum(wins) / len(wins), 3) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 3) if losses else None,
    }


DAY_MS = 86_400_000


def boot_ci_by_day(pairs, iters=2000, seed=7, alpha=0.05, min_days=5):
    """Bootstrap CI expectancy dengan resampling PER HARI (cluster).

    Sinyal antar-koin pada hari yang sama saling berkorelasi, jadi CI per-trade terlalu optimistis.
    pairs: list[(day_id, r)]. Deterministik (seed tetap). Return [lo, hi] atau [None, None] bila hari < min_days.
    """
    groups = {}
    for day, r in pairs:
        groups.setdefault(day, []).append(r)
    days = list(groups.values())
    if len(days) < min_days:
        return [None, None]
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        samp = [days[rng.randrange(len(days))] for _ in days]
        cnt = sum(len(g) for g in samp)
        if cnt:
            means.append(sum(sum(g) for g in samp) / cnt)
    means.sort()
    lo = means[int(alpha / 2 * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return [round(lo, 4), round(hi, 4)]


def protocol_status(overall, first_ts, now_ms, proto):
    weeks = 0.0 if not first_ts else (now_ms - first_ts) / (7 * 86_400_000)
    ready = overall["n"] >= proto["min_trades"] and weeks >= proto["min_weeks"]
    checks = {
        "expectancy_ci_low": overall["exp_ci"][0] is not None and overall["exp_ci"][0] > proto["min_expectancy_ci_low"]
        and (overall.get("exp_ci_day") or [None])[0] is not None
        and overall["exp_ci_day"][0] > proto["min_expectancy_ci_low"],
        "profit_factor": overall["profit_factor"] is not None and overall["profit_factor"] >= proto["min_profit_factor"],
        "sqn": overall["sqn"] is not None and overall["sqn"] >= proto["min_sqn"],
    }
    if not ready:
        verdict = "BERJALAN"
    else:
        verdict = "LULUS" if all(checks.values()) else "GAGAL"
    return {"verdict": verdict, "trades": overall["n"], "min_trades": proto["min_trades"], "weeks": round(weeks, 1),
            "min_weeks": proto["min_weeks"], "checks": checks}


def build_stats(sigs, strategy_hash, now_ms, proto):
    """sigs: dict hasil replay. Hanya sinyal dengan hash strategi aktif; DATA_GAP dikecualikan."""
    mine = [s for s in sigs.values() if s.get("strategy_hash") == strategy_hash]
    closed = sorted([s for s in mine if s["status"] == "CLOSED" and s["reason"] != "DATA_GAP"], key=lambda s: s["close_ts"])
    expired_nf = [s for s in mine if s["status"] == "EXPIRED" and s["reason"] in ("NOT_FILLED", "TARGET_FIRST")]
    filled = len(closed) + len([s for s in mine if s["status"] in ("OPEN", "OPEN_TP1")])
    rs = [s["r_net"] for s in closed]
    overall = summarize(rs)
    overall["exp_ci_day"] = boot_ci_by_day([(s["close_ts"] // DAY_MS, s["r_net"]) for s in closed])

    def group(key):
        out = {}
        for s in closed:
            k = key(s)
            out.setdefault(k, []).append(s["r_net"])
        return {k: summarize(v) for k, v in sorted(out.items())}

    eq, curve = 0.0, []
    for s in closed:
        eq += s["r_net"]
        curve.append({"t": s["close_ts"], "r": round(eq, 3)})
    first_ts = min((s["created_ts"] for s in mine), default=None)
    return {
        "overall": overall,
        "by_setup": group(lambda s: s["setup"]),
        "by_symbol": group(lambda s: s["symbol"]),
        "by_ai": group(lambda s: (s.get("ai") or {}).get("verdict", "N/A")),
        "by_direction": group(lambda s: s["direction"]),
        "fill_rate": round(filled / (filled + len(expired_nf)), 3) if (filled + len(expired_nf)) else None,
        "signals_total": len(mine), "filled": filled, "not_filled": len(expired_nf),
        "equity_curve": curve,
        "protocol": protocol_status(overall, first_ts, now_ms, proto),
    }
