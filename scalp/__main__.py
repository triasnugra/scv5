"""CLI:  python -m scalp run | verify | build-site | backtest"""
import argparse
import json
import time
import os
import shutil
import sys

from .config import load_config
from .engine import run
from .journal import Journal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scalp")
    ap.add_argument("cmd", choices=["run", "verify", "build-site", "backtest"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--data-dir", default=os.path.join(ROOT, "data"))
    ap.add_argument("--site-src", default=os.path.join(ROOT, "site"))
    ap.add_argument("--site-out", default=os.path.join(ROOT, "_site"))
    ap.add_argument("--days", type=int, default=30, help="backtest: jumlah hari ke belakang")
    ap.add_argument("--step-min", type=int, default=15, help="backtest: langkah scan (menit), live = 15")
    ap.add_argument("--symbols", default=None, help="backtest: daftar simbol dipisah koma (default: universe config)")
    ap.add_argument("--out", default=None, help="backtest: file JSON keluaran")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    jpath = os.path.join(a.data_dir, "journal.jsonl")

    if a.cmd == "verify":
        ok, msg = Journal(jpath).verify()
        print(msg)
        return 0 if ok else 1
    if a.cmd == "run":
        res = run(cfg, jpath, a.data_dir)
        print(res)
        # gagal keras hanya bila TIDAK ada data sama sekali (mis. semua sumber diblokir)
        return 0 if res["symbols"] > 0 else 2
    if a.cmd == "backtest":
        from .backtest import fetch_all_history, run_backtest
        syms = [x.strip() for x in a.symbols.split(",")] if a.symbols else list(cfg["universe"]["symbols"])
        ctx = cfg["universe"]["context_symbol"]
        if ctx not in syms:
            syms = [ctx] + syms
        end_ms = int(time.time() * 1000)
        end_ms -= end_ms % 3_600_000
        start_ms = end_ms - a.days * 86_400_000
        hist = fetch_all_history(syms, start_ms, end_ms, cfg["runtime"])
        res = run_backtest(cfg, hist, start_ms, end_ms, a.step_min * 60_000)
        o = res["overall"]
        print(f"signals={res['signals_total']} filled={res['filled']} fill_rate={res['fill_rate']}")
        print(f"n={o['n']} win={o['win_rate']} exp={o['expectancy']} ci={o['exp_ci']} ci_day={o['exp_ci_day']} "
              f"pf={o['profit_factor']} sqn={o['sqn']} maxdd={o['max_dd_r']}")
        out = a.out or os.path.join(a.data_dir, "backtest.json")
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1, sort_keys=True)
        print("ditulis:", out)
        return 0
    if a.cmd == "build-site":
        if os.path.exists(a.site_out):
            shutil.rmtree(a.site_out)
        shutil.copytree(a.site_src, a.site_out)
        dst = os.path.join(a.site_out, "data")
        os.makedirs(dst, exist_ok=True)
        for fn in ("state.json", "market.json", "brief.json", "journal.jsonl", "backtest.json"):
            p = os.path.join(a.data_dir, fn)
            if os.path.exists(p):
                shutil.copy(p, dst)
        open(os.path.join(a.site_out, ".nojekyll"), "w").close()
        print("site dibangun di", a.site_out)
        return 0


if __name__ == "__main__":
    sys.exit(main())
