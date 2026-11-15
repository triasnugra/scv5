"""Ambil candle publik (tanpa API key) dengan fallback antar-bursa.

GitHub Actions berjalan di IP Amerika; api.binance.com sering diblokir (HTTP 451), jadi urutan default
memakai data-api.binance.vision (endpoint data publik Binance) lalu fallback ke Bybit / OKX.
"""
import json
import time
import urllib.error
import urllib.request

from .indicators import Candle

INTERVAL_MS = {"5m": 300_000, "1h": 3_600_000, "4h": 14_400_000}
UA = {"User-Agent": "scalp-engine/1.0 (+github-actions)"}


def _get_json(url, timeout=20, retries=1):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"gagal GET {url.split('?')[0]}: {last}")


def _binance(base, symbol, interval, limit):
    rows = _get_json(f"{base}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}")
    return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]


def _bybit(symbol, interval, limit):
    iv = {"5m": "5", "1h": "60", "4h": "240"}[interval]
    j = _get_json(f"https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}&interval={iv}&limit={min(limit, 1000)}")
    rows = j["result"]["list"]
    rows.reverse()  # Bybit: terbaru dulu
    return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]


def _okx(symbol, interval, limit):
    inst = symbol[:-4] + "-" + symbol[-4:]
    j = _get_json(f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar={interval.replace('h', 'H')}&limit={min(limit, 300)}")
    rows = j["data"]
    rows.reverse()
    return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]


SOURCES = {
    "binance_vision": lambda s, i, l: _binance("https://data-api.binance.vision", s, i, l),
    "binance": lambda s, i, l: _binance("https://api.binance.com", s, i, l),
    "bybit": _bybit,
    "okx": _okx,
}


def closed_only(candles, interval, now_ms):
    """Buang candle yang belum tertutup."""
    ms = INTERVAL_MS[interval]
    return [c for c in candles if c.t + ms <= now_ms]


class Provider:
    """Antarmuka data: get(symbol, interval, limit, now_ms) -> list[Candle] (hanya yang tertutup)."""

    def __init__(self, sources):
        self.sources = sources
        self.used = {}
        self.errors = []
        self.fails = {}   # sumber -> jumlah gagal beruntun
        self.ok = set()   # sumber yang pernah berhasil

    def get(self, symbol, interval, limit, now_ms):
        for name in self.sources:
            fn = SOURCES.get(name)
            # sumber yang gagal >=2x beruntun dan belum pernah berhasil dianggap diblokir: lewati (hemat waktu run)
            if not fn or (self.fails.get(name, 0) >= 2 and name not in self.ok):
                continue
            try:
                c = closed_only(fn(symbol, interval, limit), interval, now_ms)
                if len(c) >= 30:
                    self.used[symbol] = name
                    self.ok.add(name)
                    self.fails[name] = 0
                    return c
            except Exception as e:  # noqa: BLE001 - lanjut ke sumber berikutnya
                self.fails[name] = self.fails.get(name, 0) + 1
                self.errors.append(f"{name}:{symbol}:{interval}:{e}")
        raise RuntimeError(f"semua sumber gagal untuk {symbol} {interval}")
