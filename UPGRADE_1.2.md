# SCV5 → 1.2-ElizStrict Upgrade

Tanggal: 2026-11-19  
Tujuan: mengurangi frekuensi Stop Loss dengan menaikkan kualitas filter sesuai metodologi @eliz883 (patience, clear trigger, protect capital).

## Ringkasan Perubahan

### 1. Config (`config/strategy.json`) — versi `1.2-ElizStrict`
- **min_score** dinaikkan: 6 → **7.5**
- **RANGE_EDGE** dimatikan default (`enabled: false`) — pola ini paling sering SL di market choppy
- **vol_spike_ratio** 1.8 → **2.2**
- **stop_buffer_atr5** 0.3 → **0.45** (lebih realistis vs noise)
- **max_stop_pct** 2.0 → **1.6**
- **min_rr** 1.5 → **1.7**
- **max_signals_per_hour** 2 → **1**
- **max_open_total** 4 → **2**
- **symbol_cooldown_min** 180 → **240**
- **max_signals_per_day** 6 → **3**
- **loss_streak_pause** 3 → **2**, cooldown 120 → **240** menit
- **max_daily_loss_r** 3.0 → **2.0**
- Universe disederhanakan (hapus PEPE & HBAR sementara)
- **ban_mid_range: true** (38%–62% range = no trade)
- **require_retest: true** pada DEVIATION
- Scoring: DEVIATION base 5, mid_range_penalty 4, season_align 2, counter_season -3

### 2. Code
- `scalp/setups.py` — rewrite deteksi DEVIATION (wajib retest), mid-range hard ban, scoring baru
- `scalp/signals.py` — prefer majors, parameter lebih ketat
- `scalp/engine.py` — pass `cfg` ke `choose_signals`

### 3. Yang TIDAK berubah
- Arsitektur journal hash-chain
- Deploy GitHub Actions / Pages
- Simulasi fill & risk layer (hanya angka yang lebih ketat)

## Cara Deploy (timpa file)

1. Backup dulu repo Anda.
2. Timpa file-file berikut dari paket upgrade ini:
   - `config/strategy.json`
   - `scalp/setups.py`
   - `scalp/signals.py`
   - `scalp/engine.py` (hanya 1 baris call)
3. Commit & push.
4. GitHub Actions akan jalan otomatis (atau trigger manual).
5. Karena `strategy_version` & hash berubah → **epoch statistik baru** dimulai (jurnal lama tetap aman).

## Ekspektasi setelah upgrade
- Jumlah sinyal jauh berkurang (hanya kualitas tinggi).
- Win rate diharapkan naik; expectancy harus menuju positif.
- Di market choppy seperti sekarang: sering "no signal" = sesuai filosofi @eliz883 ("patience is a position").

## Rollback
Cukup kembalikan `strategy.json` ke versi 1.1 dan file Python sebelumnya.
