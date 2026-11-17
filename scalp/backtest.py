"""Backtest replay: MESIN YANG SAMA dengan live (generate_candidates, choose_signals, simulate, batas risiko).

Prinsip anti-lookahead:
  * pada waktu t hanya candle dengan (open + interval) <= t yang terlihat (`asof`);
  * state risiko & seleksi dibangun dari event jurnal yang SUDAH terlihat pada t (event terlihat bila ts+5m <= t);
  * hasil trade disimulasikan dengan `journal.simulate` (fill pesimis, SL dulu bila ambigu) — sama dengan forward test.

Keterbatasan (jujur): universe tetap (bias survivorship), tanpa order book/depth, kualitas fill hanya model penetrasi,
satu sumber data. Backtest bukan bukti edge; ia hanya penyaring awal sebelum menunggu forward test berminggu-minggu.
"""
import bisect
import time
from datetime import datetime, timezone

from .context import build_btc_context
from .data import INTERVAL_MS, _get_json
from .dataq import check_candles
from .indicators import Candle
from .journal import M5_MS, replay, simulate
from .risk import gate_reasons, position_plan, risk_state
from .setups import generate_candidates
from .signals import build_record, choose_signals
from .stats import DAY_MS, boot_ci_by_day, summarize

BASES = ("https://data-api.binance.vision", "https://api.binance.com")


# ----------------------------------------------------------------- data historis
def fetch_history(symbol, interval, start_ms, end_ms, bases=BASES, log=print):
    """Unduh candle historis dengan paginasi (1000/request). Mengembalikan hanya candle TERTUTUP."""
    ms = INTERVAL_MS[interval]
    last_err = None
    for base in bases:
        out, cur = [], int(start_ms)
        try:
            while cur < end_ms:
                rows = _get_json(f"{base}/api/v3/klines?symbol={symbol}&interval={interval}"
                                 f"&startTime={cur}&endTime={int(end_ms)}&limit=1000")
                if not rows:
                    break
                out += [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]
                cur = int(rows[-1][0]) + ms
                if len(rows) < 1000:
                    break
                time.sleep(0.15)
            dedup = {c.t: c for c in out}
            res = [dedup[k] for k in sorted(dedup) if dedup[k].t + ms <= end_ms]
            if res:
                return res
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"[history] {base} gagal {symbol} {interval}: {e}")
    raise RuntimeError(f"history {symbol} {interval} gagal: {last_err}")


def fetch_all_history(symbols, start_ms, end_ms, rt, log=print):
    """Ambil 5m/1h/4h dengan warmup cukup untuk limit_* pada config."""
    need = {"5m": rt["data"]["limit_5m"], "1h": rt["data"]["limit_1h"], "4h": rt["data"]["limit_4h"]}
    hist = {}
    for s in symbols:
        hist[s] = {}
        for iv, n in need.items():
            warm = (n + 20) * INTERVAL_MS[iv]
            hist[s][iv] = fetch_history(s, iv, start_ms - warm, end_ms, log=log)
        log(f"[history] {s}: " + ", ".join(f"{iv}={len(v)}" for iv, v in hist[s].items()))
    return hist


# --------------------------------------------------------------------- as-of
class AsOf:
    """Potong data sehingga hanya candle tertutup pada waktu t yang terlihat."""

    def __init__(self, hist):
        self.hist = hist
        self.close_times = {(s, iv): [c.t + INTERVAL_MS[iv] for c in cs]
                            for s, d in hist.items() for iv, cs in d.items()}

    def get(self, sym, iv, t, limit):
        ct = self.close_times[(sym, iv)]
        i = bisect.bisect_right(ct, t)
        return self.hist[sym][iv][max(0, i - limit):i]


# ------------------------------------------------------------------ replay inti
def run_backtest(cfg, hist, start_ms, end_ms, step_ms=900_000, log=print):
    """hist: {sym: {'5m':[...],'1h':[...],'4h':[...]}} (urut waktu). Return dict hasil."""
    rt, rules = cfg["runtime"], cfg["rules"]
    shash, flt, limits = cfg["_hash"], rules["filters"], rules.get("risk_limits")
    ctx_sym = cfg["universe"]["context_symbol"]
    lim = {"5m": rt["data"]["limit_5m"], "1h": rt["data"]["limit_1h"], "4h": rt["data"]["limit_4h"]}
    asof = AsOf(hist)
    syms = [s for s in hist]

    sig_recs = []   # [(created_ts, record, events)]
    blocks = {}
    steps = 0
    t = int(start_ms)
    while t <= end_ms:
        steps += 1
        # 1) state jurnal yang terlihat pada t
        visible = []
        for created, rec, events in sig_recs:
            visible.append((created, 0, {"type": "SIGNAL", "data": rec}))
            for typ, d in events:
                if d["ts"] + M5_MS <= t:
                    visible.append((d["ts"], 1, {"type": typ, "data": d}))
        visible.sort(key=lambda x: (x[0], x[1]))
        sigs = replay([v[2] for v in visible])
        rstate = risk_state(sigs, shash, t, limits)
        blocked = gate_reasons(rstate, limits, t)
        for b in blocked:
            k = b.split("(")[0]
            blocks[k] = blocks.get(k, 0) + 1

        # 2) data as-of + kualitas
        data = {}
        for s in syms:
            d = {iv: asof.get(s, iv, t, lim[iv]) for iv in ("5m", "1h", "4h")}
            if any(check_candles(cs, iv, t, rules.get("data_quality")) for iv, cs in d.items()):
                continue
            data[s] = d
        if not data:
            t += step_ms
            continue

        # 3) kandidat -> seleksi (fungsi yang sama dengan live)
        btc_ctx = build_btc_context(data.get(ctx_sym), cfg)
        weekday = datetime.fromtimestamp(t / 1000, tz=timezone.utc).weekday()
        pool = []
        for s, d in data.items():
            cands, info, _ = generate_candidates(s, d, cfg, t, weekday)
            for c in cands:
                pool.append((c, dict(info), d))
        chosen = choose_signals(pool, sigs, flt, t, shock=bool(btc_ctx.get("shock")), blocked=blocked)

        # 4) catat + simulasikan hingga akhir data (event baru terlihat bertahap lewat aturan ts+5m <= t)
        for c, info, d in chosen:
            c["ai"] = {"verdict": "N/A"}
            sizing = position_plan(c["entry"], c["stop"], rt.get("account"), "BERJALAN")
            rec = build_record(c, d, t, cfg, btc_ctx, sizing)
            events = simulate(rec, hist[c["symbol"]]["5m"], end_ms, cfg["sim"])
            sig_recs.append((t, rec, events))
        t += step_ms

    return summarize_backtest(cfg, sig_recs, start_ms, end_ms, steps, blocks)


def summarize_backtest(cfg, sig_recs, start_ms, end_ms, steps, blocks):
    records = []
    for created, rec, events in sig_recs:
        records.append({"type": "SIGNAL", "data": rec})
        records += [{"type": typ, "data": d} for typ, d in events]
    sigs = replay(records)
    closed = sorted([s for s in sigs.values() if s["status"] == "CLOSED" and s["reason"] != "DATA_GAP"],
                    key=lambda s: s["close_ts"])
    expired_nf = [s for s in sigs.values() if s["status"] == "EXPIRED" and s["reason"] in ("NOT_FILLED", "TARGET_FIRST")]
    still_open = [s for s in sigs.values() if s["status"] in ("OPEN", "OPEN_TP1")]
    rs = [s["r_net"] for s in closed]
    overall = summarize(rs)
    overall["exp_ci_day"] = boot_ci_by_day([(s["close_ts"] // DAY_MS, s["r_net"]) for s in closed])

    def group(key):
        out = {}
        for s in closed:
            out.setdefault(key(s), []).append(s["r_net"])
        return {k: summarize(v) for k, v in sorted(out.items())}

    filled = len(closed) + len(still_open)
    # separuh pertama vs kedua: stabilitas sederhana (bukan walk-forward penuh)
    half = len(rs) // 2
    return {
        "period": {"from": start_ms, "to": end_ms, "steps": steps},
        "strategy_hash": cfg["_hash"], "strategy_version": cfg["strategy_version"],
        "signals_total": len(sigs), "filled": filled, "not_filled": len(expired_nf),
        "fill_rate": round(filled / (filled + len(expired_nf)), 3) if (filled + len(expired_nf)) else None,
        "overall": overall,
        "first_half": summarize(rs[:half]) if half else None,
        "second_half": summarize(rs[half:]) if half else None,
        "by_setup": group(lambda s: s["setup"]),
        "by_symbol": group(lambda s: s["symbol"]),
        "by_direction": group(lambda s: s["direction"]),
        "risk_blocks": blocks,
        "trades": [{"id": s["id"], "symbol": s["symbol"], "setup": s["setup"], "direction": s["direction"],
                    "created_ts": s["created_ts"], "close_ts": s["close_ts"], "reason": s["reason"],
                    "r_net": s["r_net"]} for s in closed],
        "assumptions": [
            f"biaya round-trip {cfg['sim']['cost_pct']}% + slippage exit {cfg['sim'].get('stop_slip_pct', 0)}%",
            "fill limit butuh penetrasi harga; di candle fill hanya SL dicek; SL didahulukan bila ambigu",
            "universe tetap (bias survivorship), tanpa order book; bukan bukti edge",
        ],
    }
