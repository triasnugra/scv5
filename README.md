# Scalp Engine — metodologi @eliz883 + review Claude

Mesin analisis **scalping crypto** yang berjalan sepenuhnya di **GitHub Actions** dan tampil di **GitHub Pages**.
Tanpa instalasi lokal, tanpa dependensi pip (hanya Python standar).

> **Mode: PAPER / forward-test.** Mesin tidak menyentuh uang sungguhan. Setiap sinyal dicatat di jurnal berantai-hash
> dan dinilai otomatis. Jangan pakai modal nyata sebelum kriteria protokol (di bawah) terpenuhi.

## Cara kerja (ringkas)

1. **Data**: candle 5m/1h/4h dari `data-api.binance.vision` (fallback Binance → Bybit → OKX).
2. **Struktur** (inti pemikiran @eliz883): range H1 valid, level likuiditas (tepi range + **Monday high/low**).
3. **Tiga pola, semua berbasis trigger, entry LIMIT di level (tidak mengejar harga)**:
   - `DEVIATION` — sweep/stop-run di atas/bawah level → reclaim → tunggu retest.
   - `BREAKOUT_RETEST` — close H1 di luar range, diterima, entry di retest pertama.
   - `RANGE_EDGE` — penolakan (wick) di tepi range; kualitas lebih rendah, butuh konfluensi.
4. **Filter ala @eliz883**: tengah range = tidak ada keunggulan; "musim" long/short dari H4; BTC *shock* = diam;
   Senin (range Monday choppy) dikenai penalti; maks. sinyal/jam; cooldown per simbol; stop min/maks; R:R minimum; biaya wajar.
5. **Skor 0–10** (OBV, lonjakan volume, divergensi OBV, jumlah uji level, musim, R:R). Sinyal dicatat bila skor ≥ `min_score`.
6. **Claude (opsional)** menilai tiap sinyal: `TAKE / WAIT / SKIP` + alasan + risiko (Bahasa Indonesia), dan menulis ringkasan pasar per jam.
   Claude **tidak boleh mengubah harga**. Semua sinyal tetap di-paper-trade dan diberi label verdict-nya, sehingga
   dashboard menampilkan statistik **"Aturan saja" vs "Claude TAKE"**. Nilai tambah Claude diukur, bukan diasumsikan.
7. **Penilaian konservatif**: fill butuh penetrasi harga melewati limit; di candle fill hanya SL yang dicek;
   candle yang menyentuh SL & TP sekaligus = SL; TP1 parsial 60% lalu stop ke breakeven; biaya & slippage dikurangkan dari R.
8. **Jurnal tamper-evident**: `data/journal.jsonl` berantai SHA-256. Bila ada catatan diubah/dihapus, workflow berhenti dengan pesan jelas.

## Pasang (sekali saja, semuanya lewat web GitHub)

1. Buat repo baru (mis. `scalp`), **upload seluruh isi folder ini** (termasuk folder `.github`).
   *Tip: upload lewat "Add file → Upload files" — pastikan folder `.github/workflows/scalp-engine.yml` ikut terunggah.*
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. **Settings → Actions → General → Workflow permissions: Read and write permissions** → Save.
4. *(Opsional, untuk kecerdasan Claude)* **Settings → Secrets and variables → Actions → New repository secret**:
   nama `ANTHROPIC_API_KEY`, isi API key Anda. Tanpa ini mesin tetap jalan (verdict AI = `N/A`).
   Model bisa diganti lewat **Variables** → `ANTHROPIC_MODEL` (default `claude-sonnet-5-5`).
5. **Actions → scalp-engine → Run workflow** sekali. Setelah selesai, buka `https://<username>.github.io/<repo>/`.
6. Selanjutnya berjalan otomatis tiap ~10 menit.

Catatan: repo **public** → menit Actions gratis. Pada repo public, GitHub menonaktifkan jadwal otomatis setelah 60 hari tanpa
aktivitas repo; bila itu terjadi cukup klik *Enable workflow*.

## Biaya panggilan Claude

Maksimal `max_reviews_per_run` (default 6) review per run, hanya untuk sinyal baru (jarang), ditambah 1 ringkasan per jam.
Biasanya hanya beberapa panggilan per hari. Matikan dengan `"runtime.ai.enabled": false`.

## Protokol forward-test (disegel)

`config/strategy.json` bagian `protocol`: **150 trade**, **20 minggu**, CI95 expectancy > 0, PF ≥ 1.3, SQN ≥ 1.6.
Status di dashboard: `BERJALAN → LULUS / GAGAL`.

**Mengubah parameter aturan (`universe`, `rules`, `sim`, `protocol`) = hash strategi berubah = epoch forward-test BARU**
(statistik dihitung ulang dari nol untuk hash baru; jurnal lama tetap utuh). Ini disengaja agar tidak ada
*data snooping*. Bagian `runtime` (sumber data, pengaturan AI) tidak ikut di-hash.

## Struktur

```
config/strategy.json        semua parameter (dihash)
scalp/                      data, structure, setups, context, journal(+simulasi), stats, ai, engine
tests/                      36 unit test (jadi gerbang di workflow)
site/index.html             dashboard statis
data/                       journal.jsonl, state.json, market.json, brief.json (di-commit oleh workflow)
.github/workflows/          scalp-engine.yml
```

## Batasan yang jujur

- Belum ada bukti edge. Backtest awal strategi rule-based pada proyek terkait menunjukkan edge negatif setelah biaya
  akibat *adverse selection* limit order yang tak terisi; karena itu simulasi di sini memakai aturan fill yang pesimis dan
  statistik hanya dipercaya setelah protokol terpenuhi.
- @eliz883 sendiri menyebut scalping BTC sering ber-R:R buruk; filter biaya/stop minimum membuat sinyal lebih sering
  muncul di altcoin daripada BTC.
- Tidak membaca heatmap likuidasi (data berbayar/tidak publik). Likuiditas didekati dari struktur harga (tepi range,
  Monday, swing). Heatmap, bila nanti ditambah, hanya boleh sebagai konfirmasi, bukan pemicu.
- Data dari endpoint publik; bila semua sumber gagal, workflow gagal (tidak menulis sinyal palsu).
- Bukan nasihat keuangan.
