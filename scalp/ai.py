"""Lapisan kecerdasan Claude (opsional): review kualitas setup + ringkasan pasar per jam.

Prinsip: aturan (rule engine) yang MENEMUKAN setup; Claude hanya menilai konteks (TAKE / WAIT / SKIP) dan tidak
boleh mengubah harga. Semua sinyal tetap dicatat ke forward-test dengan label verdict AI, sehingga nilai tambah
Claude bisa diukur secara statistik (lihat 'by_ai' di dashboard) alih-alih dipercaya begitu saja.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM = """Kamu adalah analis scalping crypto yang berpikir seperti trader @eliz883 (CryptoEliz). Prinsipnya:
- Trading berbasis TRIGGER dan likuiditas, bukan prediksi. Jangan mengantisipasi breakout/breakdown; jangan mengejar harga.
- Entry hanya di level presisi (tepi range H1, level Monday high/low, retest setelah sweep/reclaim). Di tengah range = tidak ada keunggulan, tunggu.
- Pola utama: liquidity sweep / stop run lalu reclaim (deviation) dan retest; breakout lalu retest pertama.
- Scalp long di musim long, scalp short di musim short. Melawan musim hanya jika konfirmasinya sangat jelas.
- Invalidasi kecil dan jelas, R:R menguntungkan; stop scalp 2-4% maksimum, sebaiknya jauh lebih kecil. Biaya & slippage harus wajar.
- Heatmap/likuiditas/indikator tidak boleh berdiri sendiri; price action memandu, indikator (OBV, volume) hanya konfirmasi.
- Range Monday cenderung choppy; saat pasar sepi/tipis dan banyak manipulasi, lebih baik kualitas daripada kuantitas. Menunggu adalah posisi.
- Altcoin bisa memberi scalp bagus saat BTC di tengah range, tetapi lakukan scalp cepat dan selektif.

Tugasmu: menilai SATU kandidat setup yang sudah ditemukan mesin. Gunakan HANYA data yang diberikan; jangan mengarang harga,
berita, atau data lain. Jangan mengubah entry/stop/target. Jawab HANYA dengan satu objek JSON (tanpa markdown):
{"verdict":"TAKE|WAIT|SKIP","confidence":0-100,"reasons":["maks 3 alasan singkat, Bahasa Indonesia"],"risks":["maks 2 risiko utama"]}
TAKE = kualitas tinggi & konteks mendukung. WAIT = menarik tapi butuh konfirmasi tambahan. SKIP = tolak (konteks buruk, mengejar, level lemah, melawan musim, dst)."""

BRIEF_SYSTEM = """Kamu analis crypto yang menulis ringkasan pasar singkat dalam Bahasa Indonesia untuk scalper bergaya @eliz883
(trigger, range H1, sweep/reclaim, tidak memprediksi). Gunakan HANYA data JSON yang diberikan. Maksimum 6 kalimat:
kondisi BTC (musim, posisi di range, no-man's-land atau tidak), altcoin mana yang dekat level penting, dan apa yang harus ditunggu.
Jangan memberi nasihat finansial, jangan memprediksi arah."""


def _post(payload, key, timeout=60, retries=2):
    body = json.dumps(payload).encode("utf-8")
    last = None
    for i in range(retries + 1):
        req = urllib.request.Request(API_URL, data=body, method="POST", headers={
            "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 529):
                break
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            last = str(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"panggilan Claude gagal: {last}")


def _text(resp):
    return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")


def parse_verdict(text):
    """Ekstrak objek JSON verdict dengan toleran (code fence / teks tambahan). Return dict atau None."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        o = json.loads(m.group(0))
    except ValueError:
        return None
    v = str(o.get("verdict", "")).upper()
    if v not in ("TAKE", "WAIT", "SKIP"):
        return None
    try:
        conf = max(0, min(100, int(o.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0
    reasons = [str(x)[:240] for x in (o.get("reasons") or [])][:3]
    risks = [str(x)[:240] for x in (o.get("risks") or [])][:2]
    return {"verdict": v, "confidence": conf, "reasons": reasons, "risks": risks}


def _bars(candles, n):
    return [[c.t // 1000, round(c.o, 8), round(c.h, 8), round(c.l, 8), round(c.c, 8), round(c.v, 2)] for c in candles[-n:]]


def build_payload(cand, info, ctx, data):
    return {
        "kandidat": {k: cand[k] for k in ("symbol", "setup", "direction", "entry", "stop", "tp1", "tp1_label", "tp2",
                                           "tp2_label", "risk_pct", "rr1", "rr2", "cost_r", "score", "score_notes",
                                           "level", "flags", "reasons", "price_at_signal", "season")},
        "pasar": {"atr_h1_pct": info.get("atr_h1_pct"),
                  "range_terbaik": (info.get("ranges") or [None])[0],
                  "monday": info.get("monday"),
                  "btc": {k: ctx.get(k) for k in ("season", "pos", "label", "shock")}},
        "candle_5m_terakhir(t,o,h,l,c,v)": _bars(data["5m"], 24),
        "candle_1h_terakhir(t,o,h,l,c,v)": _bars(data["1h"], 12),
    }


def review(cand, info, ctx, data, key, model):
    payload = build_payload(cand, info, ctx, data)
    resp = _post({"model": model, "max_tokens": 600, "system": SYSTEM,
                  "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}, key)
    out = parse_verdict(_text(resp))
    if not out:
        raise RuntimeError("balasan Claude tidak bisa di-parse")
    out["model"] = model
    return out


def brief(summary, key, model):
    resp = _post({"model": model, "max_tokens": 500, "system": BRIEF_SYSTEM,
                  "messages": [{"role": "user", "content": json.dumps(summary, ensure_ascii=False)}]}, key)
    return _text(resp).strip()


def get_key():
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()
