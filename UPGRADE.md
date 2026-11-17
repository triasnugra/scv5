# Upgrade scv5 → v1.1 (port modul scalpds, scv3, scv2, scgrok, scc)

Semua kode **Python stdlib murni** (tanpa pandas/numpy/ccxt), sama seperti scv5. Total 77 tes (36 lama + 41 baru) lulus;
17 dari 17 mutasi sengaja pada logika baru terdeteksi tes.

## Apa yang ditambahkan (dan dari mana)

| File | Sumber ide | Fungsi |
|---|---|---|
| `scalp/risk.py` | scc `risk.gate`, scgrok `riskPlan` | Batas rugi harian, jeda setelah loss beruntun, jeda drawdown 7 hari, batas sinyal/hari, **ukuran posisi** (risiko 0,25% sampai protokol LULUS, lalu 0,5%; leverage dibatasi). State diturunkan dari jurnal (replay), jadi ikut tamper-evident |
| `scalp/dataq.py` | scv2 `g0_data` | Tolak data basi, bercelah, OHLC rusak, volume nol |
| `scalp/signals.py` | scv2 (satu mesin untuk live+backtest) | Seleksi sinyal & pembuatan record, dipakai BERSAMA oleh engine dan backtest |
| `scalp/backtest.py` | scv3, scv2, scalpds | Replay historis dengan mesin yang sama, data dipotong as-of (anti-lookahead), fill pesimis |
| `scalp/stats.py` (diubah) | scalpds `bootCI` | Bootstrap CI **per hari**; protokol LULUS kini butuh CI per-trade DAN per-hari > 0 |
| `scalp/journal.py` (diubah) | scv5 fix #4 | `sim.stop_slip_pct`: slippage pada exit SL/BE/TIMEOUT (default 0 = perilaku lama) |
| `scalp/engine.py` (diubah) | — | Memakai semua modul di atas; `state.json` punya blok `risk`; sinyal punya `sizing` |
| `scalp/__main__.py` (diubah) | — | Perintah baru `backtest` |
| `config/strategy.json` (diubah) | — | Kunci baru: `risk_limits`, `data_quality`, `allow_short`, `max_open_total`, `stop_slip_pct`, `runtime.account` |
| `config/strategy.spot.json` (baru) | — | Profil spot: biaya 0,20%, slippage exit 0,03%, **tanpa short**, leverage 1 |
| `.github/workflows/deploy.yml` (diubah) | — | Lihat di bawah |
| `.github/workflows/backtest.yml` (baru) | — | Backtest manual di Actions + artifact |

## Cara upload (GitHub web)
1. Ekstrak zip. Di repo scv5: **Add file → Upload files**, seret folder `scalp`, `tests`, `config`, `.github`
   dan file `requirements.txt`, `UPGRADE.md`. Path yang sama akan **menimpa** file lama.
   (Folder `.github` tersembunyi di beberapa file manager; bila tidak terlihat, buat dua file workflow lewat *Create new file* lalu tempel isinya.)
2. Commit. Workflow `deploy.yml` otomatis jalan (push) dan menjalankan 77 tes sebagai gerbang.

**Penting:** file yang ditimpa penuh adalah `engine.py`, `stats.py`, `journal.py`, `__main__.py`, `strategy.json`, `deploy.yml`,
`requirements.txt`. Bila Anda mengubahnya di repo setelah mengirim zip ke saya, bandingkan dulu dengan `upgrade.patch`.

**Efek ke forward-test:** `strategy.json` berubah → hash strategi berubah → **epoch baru** (statistik mulai dari nol untuk hash baru;
jurnal lama tetap utuh dan chain tetap valid). Itu disengaja agar tidak ada data snooping.

## Yang berubah di workflow
- `ANTHROPIC_API_KEY` kini diteruskan ke engine (sebelumnya tidak, sehingga review Claude selalu mati).
- Persistensi: **git adalah satu-satunya sumber kebenaran** (cache Actions dihapus). Hanya `journal.jsonl` dan `brief.json` yang di-commit
  (bukan state/market tiap 15 menit). Push dicoba 3× dengan rebase; bila tetap gagal, **run gagal** (tidak lagi sekadar peringatan).
- Tes hanya menjadi gerbang pada push/manual, bukan pada run terjadwal.
- Profil strategi dipilih lewat Variable repo `STRATEGY_PROFILE` (`strategy` atau `strategy.spot`).

## Memakai backtest
Actions → **Backtest (manual)** → isi hari (mis. 30) → lihat ringkasan di *job summary*; detail ada di artifact `backtest-result`.
Lokal: `python -m scalp backtest --days 30 --out out/backtest.json`.
Lama jalan ±25 detik untuk 30 hari × 8 simbol (di luar waktu unduh data).

## Batas jujur
- Backtest **bukan bukti edge**: universe tetap (survivorship), tanpa order book, fill hanya model penetrasi, satu sumber data.
  Gunakan sebagai penyaring awal; keputusan akhir tetap pada forward-test 150 trade/20 minggu.
- Kondisi risiko memakai **pause berbatas waktu**, bukan kill switch permanen (kill switch permanen akan membekukan forward-test).
- Sinyal SHORT dari data spot tidak bisa dieksekusi di spot. Pakai profil `strategy.spot` bila Anda hanya trading spot.
- Konfigurasi default sengaja mempertahankan ekonomi lama (`cost_pct` 0,10, `stop_slip_pct` 0) agar tes lama tetap valid.

## Sengaja TIDAK diport
- **scc `LiveBroker`** dan **scgrok `execute.mjs`**: scv5 tidak punya eksekusi order, dan keduanya punya celah yang belum diperbaiki
  (SL tidak terpasang bila gagal setelah entry; `tp1_hit` ditandai sebelum SL baru terpasang). Jangan dicangkok sebelum diperbaiki.
- **scalpds resolver 1 menit** untuk candle ambigu: butuh panggilan API tambahan per sinyal dan mengubah semantik `simulate`.
  Kandidat langkah berikutnya.
- **scv3 fitur vektorisasi pandas**: bergantung pandas; harness backtest di sini memakai pendekatan replay stdlib yang sama dengan live.
