"""Kualitas data (diport dari scv2/g0_data, versi stdlib).

Candle yang cacat atau basi lebih berbahaya daripada tidak ada data: sinyal bisa lahir dari harga yang salah.
Fungsi ini mengembalikan daftar alasan penolakan; daftar kosong = data layak dipakai.
"""
from .data import INTERVAL_MS


def check_candles(candles, interval, now_ms, dq):
    """candles: list[Candle] TERTUTUP & urut. dq: cfg['rules']['data_quality'] (None = tidak dicek)."""
    if not dq:
        return []
    ms = INTERVAL_MS[interval]
    reasons = []
    min_bars = (dq.get("min_bars") or {}).get(interval, 0)
    if min_bars and len(candles) < min_bars:
        reasons.append(f"DATA_KURANG({len(candles)}<{min_bars})")
    if not candles:
        return reasons or ["DATA_KOSONG"]

    # 1) basi: candle tertutup terakhir terlalu lama
    age_bars = (now_ms - (candles[-1].t + ms)) / ms
    if age_bars > dq.get("max_stale_bars", 2):
        reasons.append(f"BASI({age_bars:.1f} bar)")

    # 2) urutan waktu + celah (pada jendela terakhir saja; pair ilikuid memang kadang tanpa candle)
    win = candles[-int(dq.get("gap_window", 150)):]
    missing, disorder = 0, False
    for i in range(1, len(win)):
        d = win[i].t - win[i - 1].t
        if d <= 0:
            disorder = True
        elif d > ms:
            missing += int(d // ms) - 1
    if disorder:
        reasons.append("URUTAN_WAKTU_RUSAK")
    if missing > dq.get("max_gap_bars", 6):
        reasons.append(f"CELAH({missing} bar hilang)")

    # 3) OHLC tidak masuk akal
    for c in win:
        if min(c.o, c.h, c.l, c.c) <= 0 or c.h < c.l or c.h < max(c.o, c.c) * (1 - 1e-9):
            reasons.append("OHLC_RUSAK")
            break

    # 4) volume nol terlalu sering (pasar mati / feed rusak)
    zw = candles[-int(dq.get("zero_volume_window", 60)):]
    if zw and sum(1 for c in zw if c.v <= 0) / len(zw) > dq.get("max_zero_volume_frac", 0.3):
        reasons.append("VOLUME_NOL_BANYAK")
    return reasons
