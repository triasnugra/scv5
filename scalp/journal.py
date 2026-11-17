"""Jurnal forward-test: append-only, berantai hash (tamper-evident), plus simulasi eksekusi konservatif.

Event: SIGNAL, FILL, TP1, CLOSE, EXPIRE. State sinyal selalu diturunkan (replay) dari jurnal.
"""
import hashlib
import json
import os

from .config import canon

GENESIS = "0" * 64
M5_MS = 300_000


def compute_hash(rec):
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256(canon(body).encode("utf-8")).hexdigest()


class Journal:
    def __init__(self, path):
        self.path = path
        self.records = []
        self._pending = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.records.append(json.loads(line))

    @property
    def last_hash(self):
        return self.records[-1]["hash"] if self.records else GENESIS

    def verify(self):
        prev = GENESIS
        for i, r in enumerate(self.records):
            if r.get("prev") != prev:
                return False, f"rantai putus di seq {i}: prev tidak cocok"
            if compute_hash(r) != r.get("hash"):
                return False, f"hash tidak valid di seq {i}: catatan diubah"
            prev = r["hash"]
        return True, f"OK ({len(self.records)} catatan)"

    def append(self, typ, data, ts):
        rec = {"seq": len(self.records), "ts": int(ts), "type": typ, "prev": self.last_hash, "data": data}
        rec["hash"] = compute_hash(rec)
        self.records.append(rec)
        self._pending.append(rec)
        return rec

    def flush(self):
        if not self._pending:
            return 0
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            for r in self._pending:
                f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
        n = len(self._pending)
        self._pending = []
        return n


def replay(records):
    """Turunkan state sinyal dari event. Return dict id -> state."""
    sigs = {}
    for r in records:
        d, t = r["data"], r["type"]
        if t == "SIGNAL":
            s = dict(d)
            s.update(status="PENDING", fill_ts=None, tp1_ts=None, close_ts=None, reason=None, r_net=None, r_gross=None)
            sigs[d["id"]] = s
            continue
        s = sigs.get(d.get("id"))
        if not s:
            continue
        if t == "FILL":
            s.update(status="OPEN", fill_ts=d["ts"])
        elif t == "TP1":
            s.update(status="OPEN_TP1", tp1_ts=d["ts"])
        elif t == "CLOSE":
            s.update(status="CLOSED", close_ts=d["ts"], reason=d["reason"], r_net=d["r_net"], r_gross=d["r_gross"])
        elif t == "EXPIRE":
            s.update(status="EXPIRED", close_ts=d["ts"], reason=d["reason"])
    return sigs


def simulate(sig, candles, now_ms, sim):
    """Simulasikan ulang eksekusi sinyal dari awal (deterministik). Return daftar event (type, data).

    Aturan KONSERVATIF (melawan kita bila ambigu):
      - fill limit butuh penetrasi `fill_penetration_pct` melewati harga entry (adverse selection);
      - di candle fill, hanya SL yang dicek (TP baru dicek candle berikutnya);
      - candle yang menyentuh SL dan TP sekaligus dianggap kena SL dulu;
      - setelah TP1 stop pindah ke breakeven, efektif mulai candle berikutnya; BE dicek sebelum TP2.
    """
    ev = []
    long_ = sig["direction"] == "LONG"
    sgn = 1 if long_ else -1
    E, S, T1, T2 = sig["entry"], sig["stop"], sig["tp1"], sig["tp2"]
    risk = abs(E - S)
    risk_pct = risk / E * 100
    cost_r = sim["cost_pct"] / risk_pct
    # slippage exit (stop = order market): memperburuk SL, BE, dan TIMEOUT. Default 0 = perilaku lama.
    slip_r = sim.get("stop_slip_pct", 0.0) / risk_pct
    f = sim["tp1_fraction"]
    r1 = (T1 - E) * sgn / risk
    r2 = (T2 - E) * sgn / risk
    pen = E * sim["fill_penetration_pct"] / 100
    max_hold = sim["max_hold_min"] * 60_000
    status, fill_t = "PENDING", None

    def close(t, reason, r_gross):
        ev.append(("CLOSE", {"id": sig["id"], "ts": t, "reason": reason, "r_gross": round(r_gross, 4),
                             "r_net": round(r_gross - cost_r, 4)}))

    seq = [c for c in candles if c.t >= sig["sim_from_ts"] and c.t + M5_MS <= now_ms]
    if not seq and now_ms - sig["sim_from_ts"] > 0:
        return ev
    for c in seq:
        hit_sl = (c.l <= S) if long_ else (c.h >= S)
        if status == "PENDING":
            if c.t > sig["expiry_ts"]:
                ev.append(("EXPIRE", {"id": sig["id"], "ts": c.t, "reason": "NOT_FILLED"}))
                return ev
            if ((c.h >= T1) if long_ else (c.l <= T1)) and not (((c.l <= E - pen) if long_ else (c.h >= E + pen))):
                ev.append(("EXPIRE", {"id": sig["id"], "ts": c.t, "reason": "TARGET_FIRST"}))
                return ev
            filled = (c.l <= E - pen) if long_ else (c.h >= E + pen)
            if filled:
                ev.append(("FILL", {"id": sig["id"], "ts": c.t, "price": E}))
                status, fill_t = "OPEN", c.t
                if hit_sl:
                    close(c.t, "SL", -1.0 - slip_r)
                    return ev
            continue
        # posisi terbuka
        if status == "OPEN":
            if hit_sl:
                close(c.t, "SL", -1.0 - slip_r)
                return ev
            if (c.h >= T1) if long_ else (c.l <= T1):
                ev.append(("TP1", {"id": sig["id"], "ts": c.t, "price": T1}))
                status = "OPEN_TP1"
                continue
            if c.t - fill_t >= max_hold:
                close(c.t, "TIMEOUT", (c.c - E) * sgn / risk - slip_r)
                return ev
        elif status == "OPEN_TP1":
            hit_be = (c.l <= E) if long_ else (c.h >= E)
            if hit_be:
                close(c.t, "BE", f * r1 - (1 - f) * slip_r)
                return ev
            if (c.h >= T2) if long_ else (c.l <= T2):
                close(c.t, "TP2", f * r1 + (1 - f) * r2)
                return ev
            if c.t - fill_t >= max_hold:
                close(c.t, "TIMEOUT", f * r1 + (1 - f) * ((c.c - E) * sgn / risk - slip_r))
                return ev
    return ev
