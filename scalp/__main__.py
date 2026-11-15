"""CLI:  python -m scalp run | verify | build-site"""
import argparse
import os
import shutil
import sys

from .config import load_config
from .engine import run
from .journal import Journal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scalp")
    ap.add_argument("cmd", choices=["run", "verify", "build-site"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--data-dir", default=os.path.join(ROOT, "data"))
    ap.add_argument("--site-src", default=os.path.join(ROOT, "site"))
    ap.add_argument("--site-out", default=os.path.join(ROOT, "_site"))
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
    if a.cmd == "build-site":
        if os.path.exists(a.site_out):
            shutil.rmtree(a.site_out)
        shutil.copytree(a.site_src, a.site_out)
        dst = os.path.join(a.site_out, "data")
        os.makedirs(dst, exist_ok=True)
        for fn in ("state.json", "market.json", "brief.json", "journal.jsonl"):
            p = os.path.join(a.data_dir, fn)
            if os.path.exists(p):
                shutil.copy(p, dst)
        open(os.path.join(a.site_out, ".nojekyll"), "w").close()
        print("site dibangun di", a.site_out)
        return 0


if __name__ == "__main__":
    sys.exit(main())
