"""Muat konfigurasi strategi + hitung hash strategi (untuk segel forward-test)."""
import hashlib
import json
import os

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "strategy.json")


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sealed_part(cfg):
    """Bagian yang mempengaruhi sinyal/penilaian. 'runtime' (sumber data, AI) tidak ikut di-hash."""
    return {k: cfg[k] for k in ("strategy_version", "universe", "rules", "sim", "protocol")}


def strategy_hash(cfg):
    return hashlib.sha256(canon(sealed_part(cfg)).encode("utf-8")).hexdigest()


def load_config(path=None):
    with open(path or DEFAULT_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_hash"] = strategy_hash(cfg)
    return cfg
