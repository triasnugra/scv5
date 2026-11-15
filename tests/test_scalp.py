import json
import os
import tempfile
import unittest

from scalp import ai, engine
from scalp.data import closed_only
from scalp.indicators import atr, ema, obv, pivots
from scalp.journal import Journal, replay, simulate
from scalp.setups import generate_candidates, plan_trade
from scalp.stats import summarize, wilson
from scalp.structure import build_range, find_ranges, monday_levels
from tests.helpers import (H1, M5, NOW, candle, cfg, make_h1, make_h1_breakout, make_h4, make_m5_flat, mirror,
                           m5_with_acceptance_then_fail, m5_with_sweep_short)

C = cfg()


def gen(m5, h1=None, h4=None, weekday=2):
    h1 = h1 or make_h1()
    return generate_candidates("TESTUSDT", {"5m": m5, "1h": h1, "4h": h4 or []}, C, NOW, weekday)


class TestIndicators(unittest.TestCase):
    def test_basic(self):
        h1 = make_h1()
        self.assertGreater(atr(h1, 14), 0)
        self.assertAlmostEqual(ema([1, 1, 1], 3)[-1], 1)
        self.assertEqual(obv([candle(0, 1, 1, 1, 1, 5), candle(1, 1, 2, 1, 2, 5), candle(2, 2, 2, 1, 1, 3)]), [0, 5, 2])
        self.assertTrue(pivots(h1, 3))

    def test_closed_only(self):
        cs = [candle(0, 1, 1, 1, 1), candle(M5, 1, 1, 1, 1)]
        self.assertEqual(len(closed_only(cs, "5m", M5 + 1)), 1)


class TestStructure(unittest.TestCase):
    def test_range_found(self):
        h1 = make_h1()
        r = find_ranges(h1, atr(h1[-120:], 14), C)
        self.assertTrue(r)
        self.assertAlmostEqual(r[0]["high"], 104.0, places=1)
        self.assertAlmostEqual(r[0]["low"], 100.0, places=1)

    def test_trending_has_no_range(self):
        h1 = [candle(i * H1, 100 + i, 101 + i, 99.5 + i, 100.5 + i) for i in range(200)]
        self.assertEqual(find_ranges(h1, atr(h1[-120:], 14), C), [])

    def test_drift_filter(self):
        h1 = make_h1()
        a = atr(h1[-120:], 14)
        self.assertIsNotNone(build_range(h1, 3, 72, a, C))
        tight = json.loads(json.dumps(C))
        tight["rules"]["range"]["max_drift"] = 0.0001
        self.assertIsNone(build_range(h1, 3, 72, a, tight))

    def test_monday_levels(self):
        m = monday_levels(make_h1(), NOW)
        self.assertIsNotNone(m)
        self.assertFalse(m["prev_week"])


class TestSetups(unittest.TestCase):
    def test_deviation_short(self):
        cands, _, _ = gen(m5_with_sweep_short())
        dev = [c for c in cands if c["setup"] == "DEVIATION"]
        self.assertEqual(len(dev), 1)
        c = dev[0]
        self.assertEqual(c["direction"], "SHORT")
        self.assertGreater(c["stop"], 104.35)      # stop di atas ekstrem sweep
        self.assertAlmostEqual(c["entry"], 104.0, places=2)
        self.assertLess(c["tp1"], c["entry"])
        self.assertGreaterEqual(c["rr1"], 1.5)
        self.assertTrue(c["flags"]["vol_spike"])
        self.assertGreaterEqual(c["score"], C["rules"]["filters"]["min_score"])

    def test_deviation_long_mirror(self):
        m5, h1 = mirror(m5_with_sweep_short()), mirror(make_h1())
        cands, _, _ = gen(m5, h1)
        dev = [c for c in cands if c["setup"] == "DEVIATION"]
        self.assertEqual(len(dev), 1)
        self.assertEqual(dev[0]["direction"], "LONG")
        self.assertLess(dev[0]["stop"], 104.0 - 0.35)
        self.assertGreater(dev[0]["tp1"], dev[0]["entry"])

    def test_mid_range_no_setup(self):
        cands, info, _ = gen(make_m5_flat(n=300, price=102.0))
        self.assertEqual(cands, [])
        self.assertTrue(info["ranges"])

    def test_no_chasing_after_retest(self):
        m5 = m5_with_sweep_short()
        # candle terakhir sudah menyentuh level lagi => retest terjadi tanpa kita => tidak ada sinyal
        last = m5[-1]
        m5[-1] = candle(last.t, last.o, 104.02, last.l, last.c, last.v)
        cands, _, _ = gen(m5)
        self.assertEqual([c for c in cands if c["setup"] == "DEVIATION"], [])

    def test_sweep_too_deep_is_breakout(self):
        cands, _, _ = gen(m5_with_sweep_short(extreme=105.8))
        self.assertEqual([c for c in cands if c["setup"] == "DEVIATION"], [])

    def test_breakout_retest(self):
        h1 = make_h1_breakout()
        m5 = make_m5_flat(n=300, price=104.8)
        cands, _, _ = gen(m5, h1)
        bo = [c for c in cands if c["setup"] == "BREAKOUT_RETEST"]
        self.assertTrue(bo, "harus ada breakout-retest")
        self.assertEqual(bo[0]["direction"], "LONG")
        self.assertAlmostEqual(bo[0]["entry"], 104.0, delta=0.1)
        self.assertLess(bo[0]["stop"], bo[0]["entry"])

    def test_acceptance_outside_is_not_deviation(self):
        cands, _, _ = gen(m5_with_acceptance_then_fail())
        self.assertEqual([c for c in cands if c["setup"] == "DEVIATION"], [])

    def test_breakout_target_already_reached(self):
        h1 = make_h1_breakout(closes=(104.6, 105.2, 105.8, 106.1, 105.0))
        cands, _, _ = gen(make_m5_flat(n=300, price=104.8), h1)
        self.assertEqual([c for c in cands if c["setup"] == "BREAKOUT_RETEST"], [])

    def test_cost_too_high_rejected(self):
        self.assertEqual(plan_trade("SHORT", 100, 100.31, [{"price": 90.0, "label": "x"}], C)[1], "biaya_terlalu_besar")

    def test_plan_rejects(self):
        t = [{"price": 90.0, "label": "x"}]
        self.assertEqual(plan_trade("SHORT", 100, 100.05, t, C)[1], "stop_terlalu_sempit")
        self.assertEqual(plan_trade("SHORT", 100, 103.0, t, C)[1], "stop_terlalu_lebar")
        self.assertEqual(plan_trade("SHORT", 100, 100.5, [{"price": 99.8, "label": "x"}], C)[1], "rr_terlalu_rendah")
        self.assertEqual(plan_trade("SHORT", 100, 99.0, t, C)[1], "stop_salah_sisi")


def sig(direction="LONG", E=100.0, S=99.0, T1=101.5, T2=103.0, expiry=None):
    if direction == "SHORT":
        S, T1, T2 = 2 * E - S, 2 * E - T1, 2 * E - T2
    return {"id": "X", "direction": direction, "entry": E, "stop": S, "tp1": T1, "tp2": T2,
            "sim_from_ts": 0, "expiry_ts": expiry if expiry is not None else 10 * M5}


def run_sim(s, bars, now=None):
    cs = [candle(i * M5, *b) for i, b in enumerate(bars)]
    return simulate(s, cs, now if now is not None else (len(cs) + 1) * M5, C["sim"])


class TestSimulate(unittest.TestCase):
    # bar = (o, h, l, c)
    def test_tp1_then_tp2(self):
        ev = run_sim(sig(), [(100.5, 100.6, 99.9, 100.2), (100.2, 101.6, 100.2, 101.5), (101.5, 103.1, 101.0, 103.0)])
        self.assertEqual([e[0] for e in ev], ["FILL", "TP1", "CLOSE"])
        d = ev[-1][1]
        self.assertEqual(d["reason"], "TP2")
        self.assertAlmostEqual(d["r_gross"], 0.6 * 1.5 + 0.4 * 3.0, places=3)
        self.assertAlmostEqual(d["r_net"], d["r_gross"] - 0.1, places=3)

    def test_sl_after_fill(self):
        ev = run_sim(sig(), [(100.5, 100.6, 99.9, 100.2), (100.2, 100.3, 98.9, 99.0)])
        self.assertEqual(ev[-1][1]["reason"], "SL")
        self.assertAlmostEqual(ev[-1][1]["r_net"], -1.1, places=3)

    def test_fill_and_sl_same_candle(self):
        ev = run_sim(sig(), [(100.5, 100.6, 98.9, 99.5)])
        self.assertEqual([e[0] for e in ev], ["FILL", "CLOSE"])
        self.assertEqual(ev[-1][1]["reason"], "SL")

    def test_tp1_and_sl_same_candle_sl_first(self):
        ev = run_sim(sig(), [(100.5, 100.6, 99.9, 100.2), (100.2, 101.8, 98.8, 100.0)])
        self.assertEqual(ev[-1][1]["reason"], "SL")

    def test_tp1_then_breakeven(self):
        ev = run_sim(sig(), [(100.5, 100.6, 99.9, 100.2), (100.2, 101.6, 100.2, 101.5), (101.5, 101.6, 99.95, 100.0)])
        self.assertEqual(ev[-1][1]["reason"], "BE")
        self.assertAlmostEqual(ev[-1][1]["r_gross"], 0.9, places=3)

    def test_no_fill_without_penetration(self):
        ev = run_sim(sig(), [(100.5, 100.6, 100.0, 100.2)] * 3)  # hanya menyentuh, tidak menembus
        self.assertEqual(ev, [])

    def test_expiry_not_filled(self):
        bars = [(100.8, 101.0, 100.6, 100.8)] * 12
        ev = run_sim(sig(expiry=5 * M5), bars)
        self.assertEqual(ev[-1], ("EXPIRE", {"id": "X", "ts": 6 * M5, "reason": "NOT_FILLED"}))

    def test_target_first(self):
        ev = run_sim(sig(), [(100.8, 101.0, 100.6, 100.8), (100.8, 101.6, 100.4, 101.5)])
        self.assertEqual(ev[-1][1]["reason"], "TARGET_FIRST")

    def test_short_mirror(self):
        s = sig("SHORT")
        ev = run_sim(s, [(99.5, 100.1, 99.4, 99.8), (99.8, 99.8, 98.4, 98.5), (98.5, 98.6, 96.9, 97.0)])
        self.assertEqual(ev[-1][1]["reason"], "TP2")

    def test_timeout(self):
        sim = dict(C["sim"], max_hold_min=15)
        bars = [(100.5, 100.6, 99.9, 100.2)] + [(100.2, 100.4, 100.1, 100.3)] * 6
        cs = [candle(i * M5, *b) for i, b in enumerate(bars)]
        ev = simulate(sig(), cs, 20 * M5, sim)
        self.assertEqual(ev[-1][1]["reason"], "TIMEOUT")

    def test_open_candle_not_used(self):
        cs = [candle(0, 100.5, 100.6, 99.9, 100.2)]
        self.assertEqual(simulate(sig(), cs, M5 - 1, C["sim"]), [])  # candle belum tertutup


class TestJournal(unittest.TestCase):
    def test_chain_and_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "j.jsonl")
            j = Journal(p)
            j.append("SIGNAL", {"id": "A", "symbol": "X"}, 1)
            j.append("FILL", {"id": "A", "ts": 2}, 2)
            j.flush()
            j2 = Journal(p)
            self.assertTrue(j2.verify()[0])
            self.assertEqual(replay(j2.records)["A"]["status"], "OPEN")
            with open(p) as f:
                lines = f.read().splitlines()
            lines[0] = lines[0].replace('"X"', '"Y"')
            with open(p, "w") as f:
                f.write("\n".join(lines) + "\n")
            ok, msg = Journal(p).verify()
            self.assertFalse(ok)
            self.assertIn("diubah", msg)

    def test_deleted_record_detected(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "j.jsonl")
            j = Journal(p)
            for i in range(3):
                j.append("SIGNAL", {"id": str(i)}, i)
            j.flush()
            with open(p) as f:
                lines = f.read().splitlines()
            with open(p, "w") as f:
                f.write("\n".join([lines[0], lines[2]]) + "\n")
            self.assertFalse(Journal(p).verify()[0])


class TestStats(unittest.TestCase):
    def test_summary(self):
        s = summarize([2.0, -1.0, 2.0, -1.0, 1.0])
        self.assertEqual(s["n"], 5)
        self.assertAlmostEqual(s["win_rate"], 0.6)
        self.assertAlmostEqual(s["profit_factor"], 5.0 / 2.0, places=2)
        self.assertAlmostEqual(s["expectancy"], 0.6)
        self.assertAlmostEqual(s["max_dd_r"], 1.0)
        self.assertEqual(summarize([])["n"], 0)

    def test_wilson(self):
        lo, hi = wilson(50, 100)
        self.assertTrue(0.40 < lo < 0.41 and 0.59 < hi < 0.60)


class TestAI(unittest.TestCase):
    def test_parse(self):
        t = '```json\n{"verdict":"take","confidence":"78","reasons":["a","b","c","d"],"risks":["r"]}\n```'
        o = ai.parse_verdict(t)
        self.assertEqual(o["verdict"], "TAKE")
        self.assertEqual(o["confidence"], 78)
        self.assertEqual(len(o["reasons"]), 3)
        self.assertIsNone(ai.parse_verdict("tidak ada json"))
        self.assertIsNone(ai.parse_verdict('{"verdict":"BUY"}'))


class FakeProvider:
    def __init__(self, series):
        self.series, self.used = series, {}

    def get(self, symbol, interval, limit, now_ms):
        return closed_only(self.series[symbol][interval], interval, now_ms)


def fake_series(m5_alt):
    h1, h4 = make_h1(), make_h4()
    return {"BTCUSDT": {"5m": make_m5_flat(n=300), "1h": h1, "4h": h4},
            "ALTUSDT": {"5m": m5_alt, "1h": h1, "4h": h4}}


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads(json.dumps(C))
        self.cfg["universe"]["symbols"] = ["BTCUSDT", "ALTUSDT"]
        self.cfg["runtime"]["ai"]["enabled"] = False
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            jp = os.path.join(d, "journal.jsonl")
            alt = m5_with_sweep_short()
            res = engine.run(self.cfg, jp, d, FakeProvider(fake_series(alt)), now_ms=NOW, log=lambda *_: None)
            self.assertEqual(res["new_signals"], 1)
            with open(os.path.join(d, "state.json")) as f:
                state = json.load(f)
            self.assertTrue(state["integrity"]["ok"])
            self.assertEqual(len(state["active"]), 1)
            self.assertEqual(state["active"][0]["ai"]["verdict"], "N/A")
            with open(os.path.join(d, "market.json")) as f:
                mk = json.load(f)
            self.assertIn("BTCUSDT", mk["symbols"])
            self.assertEqual(mk["symbols"]["BTCUSDT"]["state"], "TENGAH_RANGE")

            # run ulang pada waktu sama: tidak boleh ada duplikat
            res2 = engine.run(self.cfg, jp, d, FakeProvider(fake_series(alt)), now_ms=NOW, log=lambda *_: None)
            self.assertEqual(res2["new_signals"], 0)

            # harga kemudian retest -> fill -> TP1
            ext = list(alt)
            t = NOW
            ext.append(candle(t, 103.4, 104.05, 103.4, 103.9))            # fill SHORT di 104.0
            ext.append(candle(t + M5, 103.9, 103.9, 101.9, 102.0))        # TP1 di 102.0
            res3 = engine.run(self.cfg, jp, d, FakeProvider(fake_series(ext)), now_ms=NOW + 3 * M5, log=lambda *_: None)
            sigs = replay(Journal(jp).records)
            s = list(sigs.values())[0]
            self.assertIn(s["status"], ("OPEN_TP1", "CLOSED"))
            self.assertTrue(Journal(jp).verify()[0])
            self.assertGreaterEqual(res3["new_events"], 2)

    def test_one_active_signal_per_symbol(self):
        cfg2 = json.loads(json.dumps(self.cfg))
        cfg2["rules"]["filters"]["symbol_cooldown_min"] = 0
        cfg2["rules"]["filters"]["max_signals_per_hour"] = 5
        with tempfile.TemporaryDirectory() as d:
            jp = os.path.join(d, "journal.jsonl")
            alt = m5_with_sweep_short()
            r1 = engine.run(cfg2, jp, d, FakeProvider(fake_series(alt)), now_ms=NOW, log=lambda *_: None)
            r2 = engine.run(cfg2, jp, d, FakeProvider(fake_series(alt)), now_ms=NOW + M5, log=lambda *_: None)
            self.assertEqual((r1["new_signals"], r2["new_signals"]), (1, 0))

    def test_tamper_blocks_run(self):
        with tempfile.TemporaryDirectory() as d:
            jp = os.path.join(d, "journal.jsonl")
            engine.run(self.cfg, jp, d, FakeProvider(fake_series(m5_with_sweep_short())), now_ms=NOW, log=lambda *_: None)
            with open(jp) as f:
                txt = f.read().replace('"SHORT"', '"LONG"')
            with open(jp, "w") as f:
                f.write(txt)
            with self.assertRaises(RuntimeError):
                engine.run(self.cfg, jp, d, FakeProvider(fake_series(m5_with_sweep_short())), now_ms=NOW + M5, log=lambda *_: None)

    def test_btc_shock_blocks(self):
        series = fake_series(m5_with_sweep_short())
        h1 = list(series["BTCUSDT"]["1h"])
        last = h1[-1]
        h1[-1] = candle(last.t, last.o, last.h + 6, last.l - 6, last.c, last.v)
        series["BTCUSDT"]["1h"] = h1
        with tempfile.TemporaryDirectory() as d:
            res = engine.run(self.cfg, os.path.join(d, "j.jsonl"), d, FakeProvider(series), now_ms=NOW, log=lambda *_: None)
            self.assertEqual(res["new_signals"], 0)


if __name__ == "__main__":
    unittest.main()
