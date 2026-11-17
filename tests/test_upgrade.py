"""Tes modul tambahan: dataq, risk, bootstrap per hari, slippage exit, backtest, integrasi engine."""
import json
import os
import tempfile
import unittest

from unittest import mock

from scalp import backtest as bt
from scalp import engine
from scalp.backtest import AsOf, run_backtest
from scalp.dataq import check_candles
from scalp.journal import replay, simulate
from scalp.risk import gate_reasons, position_plan, risk_state
from scalp.stats import DAY_MS, boot_ci_by_day, protocol_status, summarize
from tests.helpers import H1, M5, NOW, candle, cfg, make_h1, make_h4, make_m5_flat, m5_with_sweep_short
from tests.test_scalp import FakeProvider, fake_series

C = cfg()
DQ = C["rules"]["data_quality"]
LIM = C["rules"]["risk_limits"]
H = "h1"


def closed_sig(i, r, close_ts, h=H, created=None):
    return {"id": f"S{i}", "symbol": "XUSDT", "status": "CLOSED", "reason": "SL" if r < 0 else "TP2", "r_net": r,
            "close_ts": close_ts, "created_ts": created if created is not None else close_ts - 3_600_000,
            "strategy_hash": h}


class TestDataQ(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(check_candles(make_h1(), "1h", NOW, DQ), [])

    def test_disabled_when_none(self):
        self.assertEqual(check_candles([], "1h", NOW, None), [])

    def test_stale(self):
        self.assertTrue(any("BASI" in r for r in check_candles(make_h1(end=NOW - 10 * H1), "1h", NOW, DQ)))

    def test_too_few(self):
        self.assertTrue(any("DATA_KURANG" in r for r in check_candles(make_h1(n=50), "1h", NOW, DQ)))

    def test_gap(self):
        h1 = make_h1()
        h1 = h1[:-40] + h1[-30:]  # 10 bar hilang di jendela terakhir
        self.assertTrue(any("CELAH" in r for r in check_candles(h1, "1h", NOW, DQ)))

    def test_bad_ohlc_and_zero_volume(self):
        h1 = make_h1()
        h1[-5] = candle(h1[-5].t, 100, 99, 101, 100)  # high < low
        self.assertIn("OHLC_RUSAK", check_candles(h1, "1h", NOW, DQ))
        m5 = [candle(c.t, c.o, c.h, c.l, c.c, 0.0) for c in make_m5_flat()]
        self.assertIn("VOLUME_NOL_BANYAK", check_candles(m5, "5m", NOW, DQ))


class TestRisk(unittest.TestCase):
    def sigs(self, items):
        return {s["id"]: s for s in items}

    def test_no_limits_no_block(self):
        st = risk_state({}, H, NOW, None)
        self.assertEqual(gate_reasons(st, None, NOW), [])

    def test_daily_loss_blocks(self):
        d0 = NOW - NOW % DAY_MS
        s = self.sigs([closed_sig(1, -1.1, d0 + 1000), closed_sig(2, -1.1, d0 + 2000), closed_sig(3, -1.1, d0 + 3000)])
        why = gate_reasons(risk_state(s, H, NOW, LIM), LIM, NOW)
        self.assertTrue(any(w.startswith("BATAS_RUGI_HARIAN") for w in why))

    def test_loss_streak_cooldown_expires(self):
        s = self.sigs([closed_sig(i, -1.1, NOW - (4 - i) * 600_000) for i in range(3)])
        st = risk_state(s, H, NOW, LIM)
        self.assertEqual(st["loss_streak"], 3)
        self.assertTrue(any(w.startswith("JEDA_LOSS") for w in gate_reasons(st, LIM, NOW)))
        later = NOW + 3 * 3_600_000  # > 120 menit setelah loss terakhir
        self.assertFalse(any(w.startswith("JEDA_LOSS") for w in gate_reasons(risk_state(s, H, later, LIM), LIM, later)))

    def test_win_breaks_streak(self):
        s = self.sigs([closed_sig(1, -1.1, NOW - 3000), closed_sig(2, 1.5, NOW - 2000), closed_sig(3, -1.1, NOW - 1000)])
        self.assertEqual(risk_state(s, H, NOW, LIM)["loss_streak"], 1)

    def test_drawdown_pause(self):
        lim = dict(LIM, max_daily_loss_r=99, loss_streak_pause=99, dd_pause_r=4.0)
        s = self.sigs([closed_sig(i, -1.1, NOW - (10 - i) * 60_000) for i in range(5)])
        self.assertTrue(any(w.startswith("JEDA_DRAWDOWN") for w in gate_reasons(risk_state(s, H, NOW, lim), lim, NOW)))

    def test_other_strategy_hash_ignored(self):
        s = self.sigs([closed_sig(i, -1.1, NOW - i * 1000, h="OTHER") for i in range(5)])
        self.assertEqual(gate_reasons(risk_state(s, H, NOW, LIM), LIM, NOW), [])

    def test_data_gap_excluded(self):
        x = closed_sig(1, 0.0, NOW - 1000)
        x["reason"] = "DATA_GAP"
        self.assertEqual(risk_state({"S1": x}, H, NOW, LIM)["loss_streak"], 0)

    def test_signals_per_day_cap(self):
        d0 = NOW - NOW % DAY_MS
        s = self.sigs([dict(closed_sig(i, 1.0, d0 + 5000 + i), created_ts=d0 + 1000 + i) for i in range(6)])
        self.assertIn("BATAS_SINYAL_HARIAN", gate_reasons(risk_state(s, H, NOW, LIM), LIM, NOW))


class TestPositionPlan(unittest.TestCase):
    ACC = C["runtime"]["account"]

    def test_pre_protocol_risk(self):
        p = position_plan(100.0, 99.0, self.ACC, "BERJALAN")
        self.assertAlmostEqual(p["risk_usd"], 2.5)
        self.assertAlmostEqual(p["qty"], 2.5)
        self.assertFalse(p["leverage_capped"])

    def test_after_protocol_pass(self):
        p = position_plan(100.0, 99.0, self.ACC, "LULUS")
        self.assertAlmostEqual(p["risk_usd"], 5.0)

    def test_leverage_cap_reduces_risk(self):
        p = position_plan(100.0, 99.99, self.ACC, "BERJALAN")
        self.assertTrue(p["leverage_capped"])
        self.assertAlmostEqual(p["leverage"], self.ACC["max_leverage"])
        self.assertLess(p["risk_usd"], 2.5)

    def test_invalid_inputs(self):
        self.assertIsNone(position_plan(100.0, 100.0, self.ACC, "BERJALAN"))
        self.assertIsNone(position_plan(100.0, 99.0, None, "BERJALAN"))


class TestStatsDay(unittest.TestCase):
    def test_needs_min_days(self):
        self.assertEqual(boot_ci_by_day([(1, 1.0), (1, 1.0), (2, 1.0)]), [None, None])

    def test_deterministic_and_directional(self):
        pos = [(d, 0.8 + 0.1 * (i % 3)) for d in range(12) for i in range(3)]
        a, b = boot_ci_by_day(pos), boot_ci_by_day(pos)
        self.assertEqual(a, b)
        self.assertGreater(a[0], 0)
        neg = [(d, -0.5) for d in range(12)]
        self.assertLess(boot_ci_by_day(neg)[1], 0)

    def test_clustering_widens_ci(self):
        # satu hari ekstrem berisi banyak trade: CI per-hari harus lebih lebar dari CI per-trade
        pairs = [(0, 3.0)] * 30 + [(d, -0.2) for d in range(1, 11)]
        lo_d, hi_d = boot_ci_by_day(pairs)
        s = summarize([r for _, r in pairs])
        self.assertGreater(hi_d - lo_d, s["exp_ci"][1] - s["exp_ci"][0])

    def test_protocol_requires_day_ci(self):
        o = summarize([1.0] * 160)
        o["exp_ci_day"] = [None, None]
        p = protocol_status(o, NOW - 30 * 7 * DAY_MS, NOW, C["protocol"])
        self.assertEqual(p["verdict"], "GAGAL")
        self.assertFalse(p["checks"]["expectancy_ci_low"])


class TestExitSlippage(unittest.TestCase):
    def test_sl_includes_slippage(self):
        sig = {"id": "X", "direction": "LONG", "entry": 100.0, "stop": 99.0, "tp1": 101.5, "tp2": 103.0,
               "sim_from_ts": 0, "expiry_ts": 10 * M5}
        cs = [candle(0, 100.5, 100.6, 99.9, 100.2), candle(M5, 100.2, 100.3, 98.9, 99.0)]
        sim = dict(C["sim"], stop_slip_pct=0.1)
        ev = simulate(sig, cs, 5 * M5, sim)
        self.assertAlmostEqual(ev[-1][1]["r_gross"], -1.0 - 0.1 / 1.0, places=3)
        base = simulate(sig, cs, 5 * M5, C["sim"])
        self.assertAlmostEqual(base[-1][1]["r_gross"], -1.0, places=3)

    def _sig(self):
        return {"id": "X", "direction": "LONG", "entry": 100.0, "stop": 99.0, "tp1": 101.5, "tp2": 103.0,
                "sim_from_ts": 0, "expiry_ts": 10 * M5}

    def test_sl_on_fill_candle_includes_slippage(self):
        cs = [candle(0, 100.5, 100.6, 98.9, 99.5)]  # fill + SL di candle yang sama
        ev = simulate(self._sig(), cs, 3 * M5, dict(C["sim"], stop_slip_pct=0.1))
        self.assertEqual(ev[-1][1]["reason"], "SL")
        self.assertAlmostEqual(ev[-1][1]["r_gross"], -1.1, places=3)

    def test_breakeven_includes_slippage_on_remainder(self):
        cs = [candle(0, 100.5, 100.6, 99.9, 100.2), candle(M5, 100.2, 101.6, 100.2, 101.5),
              candle(2 * M5, 101.5, 101.6, 99.95, 100.0)]
        ev = simulate(self._sig(), cs, 5 * M5, dict(C["sim"], stop_slip_pct=0.1))
        f = C["sim"]["tp1_fraction"]
        self.assertEqual(ev[-1][1]["reason"], "BE")
        self.assertAlmostEqual(ev[-1][1]["r_gross"], f * 1.5 - (1 - f) * 0.1, places=3)

    def test_timeout_includes_slippage(self):
        bars = [(100.5, 100.6, 99.9, 100.2)] + [(100.2, 100.4, 100.1, 100.3)] * 6
        cs = [candle(i * M5, *b) for i, b in enumerate(bars)]
        sim = dict(C["sim"], max_hold_min=15)
        a = simulate(self._sig(), cs, 20 * M5, sim)[-1][1]
        b = simulate(self._sig(), cs, 20 * M5, dict(sim, stop_slip_pct=0.1))[-1][1]
        self.assertEqual(a["reason"], "TIMEOUT")
        self.assertAlmostEqual(a["r_gross"] - b["r_gross"], 0.1, places=3)

    def test_timeout_after_tp1_includes_slippage_on_remainder(self):
        bars = [(100.5, 100.6, 99.9, 100.2), (100.2, 101.6, 100.2, 101.5)] + [(101.5, 101.8, 100.9, 101.2)] * 6
        cs = [candle(i * M5, *b) for i, b in enumerate(bars)]
        sim = dict(C["sim"], max_hold_min=20)
        a = simulate(self._sig(), cs, 30 * M5, sim)[-1][1]
        b = simulate(self._sig(), cs, 30 * M5, dict(sim, stop_slip_pct=0.1))[-1][1]
        self.assertEqual(a["reason"], "TIMEOUT")
        f = C["sim"]["tp1_fraction"]
        self.assertAlmostEqual(a["r_gross"] - b["r_gross"], (1 - f) * 0.1, places=3)


def hist_with_signal(future_tail):
    h1, h4 = make_h1(), make_h4()
    alt = m5_with_sweep_short() + future_tail
    return {"BTCUSDT": {"5m": make_m5_flat(n=300), "1h": h1, "4h": h4},
            "ALTUSDT": {"5m": alt, "1h": h1, "4h": h4}}


FUT_BE = [candle(NOW, 103.4, 104.05, 103.4, 103.9),          # fill SHORT di 104.0
          candle(NOW + M5, 103.9, 103.9, 101.9, 102.0),      # TP1
          candle(NOW + 2 * M5, 102.0, 104.1, 102.0, 104.0)]  # kembali ke entry -> BE


class TestBacktest(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads(json.dumps(C))
        self.cfg["universe"]["symbols"] = ["BTCUSDT", "ALTUSDT"]
        self.cfg["_hash"] = C["_hash"]

    def test_asof_hides_unclosed_and_future(self):
        h = hist_with_signal(FUT_BE)
        a = AsOf(h)
        got = a.get("ALTUSDT", "5m", NOW, 1000)
        self.assertEqual(got[-1].t + M5, NOW)
        self.assertTrue(all(c.t + M5 <= NOW for c in got))
        self.assertTrue(all(c.t + H1 <= NOW for c in a.get("ALTUSDT", "1h", NOW, 1000)))

    def test_backtest_produces_trade_with_be(self):
        res = run_backtest(self.cfg, hist_with_signal(FUT_BE), NOW, NOW + 4 * M5 * 3, 900_000, log=lambda *_: None)
        self.assertEqual(res["signals_total"], 1)
        self.assertEqual(res["overall"]["n"], 1)
        self.assertEqual(res["trades"][0]["reason"], "BE")
        self.assertEqual(res["trades"][0]["symbol"], "ALTUSDT")

    def test_no_lookahead_signal_independent_of_future(self):
        a = run_backtest(self.cfg, hist_with_signal(FUT_BE), NOW, NOW, 900_000)
        crash = [candle(NOW, 103.4, 120.0, 90.0, 100.0), candle(NOW + M5, 100.0, 130.0, 70.0, 80.0)]
        b = run_backtest(self.cfg, hist_with_signal(crash), NOW, NOW, 900_000)
        self.assertEqual(a["signals_total"], b["signals_total"])  # sinyal sama; hanya hasilnya yang boleh beda
        self.assertEqual(a["signals_total"], 1)

    def test_allow_short_false_blocks(self):
        self.cfg["rules"]["filters"]["allow_short"] = False
        res = run_backtest(self.cfg, hist_with_signal(FUT_BE), NOW, NOW, 900_000)
        self.assertEqual(res["signals_total"], 0)

    def test_risk_limit_blocks_and_counts(self):
        self.cfg["rules"]["risk_limits"] = dict(LIM, max_signals_per_day=0)
        res = run_backtest(self.cfg, hist_with_signal(FUT_BE), NOW, NOW, 900_000)
        self.assertEqual(res["signals_total"], 0)
        self.assertGreaterEqual(res["risk_blocks"].get("BATAS_SINYAL_HARIAN", 0), 1)


def _rows(t0, n, step):
    return [[t0 + i * step, "1", "2", "0.5", "1.5", "10"] for i in range(n)]


class TestFetchHistory(unittest.TestCase):
    def test_pagination_dedup_and_closed_only(self):
        step = M5
        pages = [_rows(0, 1000, step), _rows(1000 * step, 10, step)]
        calls = []

        def fake(url, *a, **k):
            calls.append(url)
            return pages[len(calls) - 1]

        end = 1005 * step - 1  # candle terakhir (index 1004) belum tertutup
        with mock.patch.object(bt, "_get_json", side_effect=fake), mock.patch.object(bt.time, "sleep"):
            out = bt.fetch_history("XUSDT", "5m", 0, end, bases=("https://a",))
        self.assertEqual(len(calls), 2)
        self.assertIn("startTime=", calls[1])
        self.assertEqual(out[-1].t, 1003 * step)  # 1004 dibuang karena belum tertutup
        self.assertEqual(len({c.t for c in out}), len(out))
        self.assertEqual([c.t for c in out], sorted(c.t for c in out))

    def test_fallback_to_next_host(self):
        def fake(url, *a, **k):
            if "bad.example" in url:
                raise RuntimeError("HTTP 451")
            return _rows(0, 5, M5)

        with mock.patch.object(bt, "_get_json", side_effect=fake):
            out = bt.fetch_history("XUSDT", "5m", 0, 10 * M5, bases=("https://bad.example", "https://ok.example"),
                                   log=lambda *_: None)
        self.assertEqual(len(out), 5)

    def test_all_hosts_fail_raises(self):
        with mock.patch.object(bt, "_get_json", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                bt.fetch_history("XUSDT", "5m", 0, 10 * M5, bases=("https://a",), log=lambda *_: None)


class TestCLIBacktest(unittest.TestCase):
    def test_cli_writes_json(self):
        from scalp.__main__ import main
        h = hist_with_signal(FUT_BE)
        d = tempfile.mkdtemp()
        out = os.path.join(d, "bt.json")
        with mock.patch.object(bt, "fetch_all_history", return_value=h), \
                mock.patch("scalp.__main__.time.time", return_value=(NOW + 6 * M5) / 1000):
            rc = main(["backtest", "--days", "1", "--step-min", "15", "--symbols", "BTCUSDT,ALTUSDT",
                       "--data-dir", d, "--out", out])
        self.assertEqual(rc, 0)
        with open(out) as f:
            res = json.load(f)
        self.assertIn("overall", res)
        self.assertIn("assumptions", res)


class TestEngineIntegration(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads(json.dumps(C))
        self.cfg["universe"]["symbols"] = ["BTCUSDT", "ALTUSDT"]
        self.cfg["runtime"]["ai"]["enabled"] = False
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def run_once(self, cfg, series, now=NOW):
        d = tempfile.mkdtemp()
        res = engine.run(cfg, os.path.join(d, "j.jsonl"), d, FakeProvider(series), now_ms=now, log=lambda *_: None)
        with open(os.path.join(d, "state.json")) as f:
            return res, json.load(f), d

    def test_signal_has_sizing_and_state_has_risk(self):
        res, state, _ = self.run_once(self.cfg, fake_series(m5_with_sweep_short()))
        self.assertEqual(res["new_signals"], 1)
        sz = state["active"][0]["sizing"]
        self.assertAlmostEqual(sz["risk_usd"], 2.5, places=2)
        self.assertIn("state", state["risk"])
        self.assertEqual(state["risk"]["halted"], [])

    def test_allow_short_false(self):
        self.cfg["rules"]["filters"]["allow_short"] = False
        res, _, _ = self.run_once(self.cfg, fake_series(m5_with_sweep_short()))
        self.assertEqual(res["new_signals"], 0)

    def test_risk_halt_reported(self):
        self.cfg["rules"]["risk_limits"] = dict(LIM, max_signals_per_day=0)
        res, state, _ = self.run_once(self.cfg, fake_series(m5_with_sweep_short()))
        self.assertEqual(res["new_signals"], 0)
        self.assertIn("BATAS_SINYAL_HARIAN", state["risk"]["halted"])

    def test_stale_symbol_dropped_by_dataq(self):
        series = fake_series(m5_with_sweep_short())
        series["ALTUSDT"]["5m"] = make_m5_flat(n=300, end=NOW - 5 * 3_600_000)
        res, state, _ = self.run_once(self.cfg, series)
        self.assertEqual(res["new_signals"], 0)
        self.assertTrue(any("ALTUSDT" in e and "DATAQ" in e for e in state["errors"]))

    def test_spot_profile_loads_and_differs(self):
        from scalp.config import load_config
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
        spot = load_config(os.path.join(base, "strategy.spot.json"))
        self.assertNotEqual(spot["_hash"], C["_hash"])
        self.assertFalse(spot["rules"]["filters"]["allow_short"])
        self.assertEqual(spot["runtime"]["account"]["max_leverage"], 1)


if __name__ == "__main__":
    unittest.main()
