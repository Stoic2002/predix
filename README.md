# Predix — robot trading tren (riset, backtest, paper trading)

Pipeline riset offline untuk BTC/ETH perpetual futures (harian). Tidak ada API key, login,
atau order di fase ini. Hasil akhir: [reports/REPORT.md](reports/REPORT.md).

## Setup

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

## Menjalankan

| Langkah | Perintah | Output |
| --- | --- | --- |
| M1 data (fetch/update inkremental + validasi) | `python -m src.data` | `data/*.csv` |
| Validasi data saja, tanpa jaringan | `python -m src.data --validate-only` | ringkasan di terminal |
| Unit test | `python -m pytest -q` | |
| M3 strategi di in-sample | `python -m src.run` | `reports/m3_insample/` |
| M4 validasi (sensitivitas, walk-forward, OOS sekali) | `python -m src.validate` | `reports/m4/` |
| M5 laporan + grafik | `python -m src.report` | `reports/REPORT.md`, `reports/charts/` |

## Fase 2: paper trading S2 (dry-run)

```bash
python -m src.paper
```

Jalankan sekali sehari setelah candle harian close (00:00 UTC = 07:00 WIB). Bot mengambil data
terbaru, memproses candle yang belum diproses (termasuk yang terlewat), lalu mencetak ekuitas,
posisi terbuka, dan rencana order untuk open berikutnya. State disimpan di `state/paper_s2.db`.
Mode dry-run tidak memakai akun exchange; fill disimulasikan dengan `src.backtest.Engine`, jadi
aturannya identik dengan backtest (dibuktikan di `tests/test_paper.py`).

- Kill switch: peringatan di drawdown 20%, entry baru diblokir di 30%.
  Reset: `python -m src.paper --reset-kill-switch`.
- Setting dikunci: kalau parameter S2, biaya, atau risiko berubah, bot menolak jalan.
- Demo tampilan (replay histori ke db terpisah, bukan bukti performa):
  `python -m src.paper --db state/paper_demo.db --start 2026-01-01`

## Fase 2: lab riset

```bash
python -m src.lab screen
```

Menguji semua kandidat yang didaftarkan di bagian `research` pada `config.yaml` (4 strategi trend ×
long-short/long-only) di 10 simbol, hanya pada periode seleksi 2020–2023. Kandidat, parameter, varian
sensitivitas, dan kriteria ditulis sebelum hasil dilihat; setiap run dicatat di
`reports/lab/research_log.csv` dan jumlah percobaan dipakai untuk Deflated Sharpe Ratio.

Holdout 2024+ dijalankan sekali per kandidat yang lolos seleksi, lalu dikunci:
`python -m src.lab holdout NAMA_KANDIDAT`.

## Deploy di VPS

IP, domain, dan nama kunci SSH sengaja tidak ditulis di repo; nilai aslinya ada di `deploy/LOCAL.md`
(file lokal, tidak diunggah). Link dashboard untuk laporan Telegram dibaca dari `DASHBOARD_URL` di `.env`.

Bot paper trading berjalan di VPS (`<user>@<IP-VPS>`, folder `~/crypto-bot`, Python 3.14.7
lewat `uv`). **Jangan jalankan `python -m src.paper` di laptop**; satu-satunya bot yang aktif ada di VPS.

- `crypto-paper.timer`: menjalankan semua bot (`python -m src.paper --all`: S2 di BTC/ETH dan TSMOM
  long-only di 10 simbol, db terpisah) setiap hari 00:05 UTC (07:05 WIB), cadangan 06:05 UTC.
- `crypto-dashboard.service`: dashboard di `127.0.0.1:8502` VPS, dibuka lewat Caddy di
  **https://<subdomain-dashboard>** (login basic auth; DNS Cloudflare proxied, sertifikat Let's Encrypt).
  Blok Caddy: `deploy/caddy-bot-block.caddy`. Ganti password:
  `ssh -t -i ~/.ssh/<kunci-ssh> <user>@<IP-VPS> 'sudo bash ~/crypto-bot/deploy/set-dashboard-password.sh'`.

Cek log bot: `ssh -i ~/.ssh/<kunci-ssh> <user>@<IP-VPS> 'journalctl -u crypto-paper.service -n 30 -o cat'`.
Update kode: rsync proyek ke VPS (tanpa `.venv`, `state/`, `data/`, `.env`), lalu
`sudo systemctl restart crypto-dashboard.service`. File unit systemd ada di `deploy/`.

## Laporan Telegram

Setelah robot jalan tiap pagi, ringkasan dikirim ke Telegram (hanya kalau ada candle baru, robot gagal,
atau robot terlambat; run cadangan siang tidak mengirim ulang). Setup sekali, token diketik tersembunyi
dan disimpan di `.env` VPS (izin 600):

```bash
ssh -t -i ~/.ssh/<kunci-ssh> <user>@<IP-VPS> 'cd ~/crypto-bot && .venv/bin/python -m src.notify --setup'
```

Uji kirim: `.venv/bin/python -m src.notify --test`. Saat rsync ke VPS, sertakan `--exclude .env`.

## Dashboard (read-only)

```bash
streamlit run app.py
```

Buka http://localhost:8501. Isinya: ringkasan lolos/tidak lolos, kurva ekuitas dan drawdown
(bisa dibandingkan dengan buy & hold), tabel metrik IS/OOS, daftar trade dengan grafik candle per
trade, sensitivitas, walk-forward, Monte Carlo, dan status data. Dashboard hanya membaca hasil di
`reports/m4/`; tidak ada kontrol untuk mengubah parameter, sesuai aturan riset di PRD. Setelah
menjalankan ulang `src.validate`, tekan **R** di halaman untuk memuat hasil baru.

Semua parameter ada di `config.yaml`. Out-of-sample dikunci ke hash config
(`reports/m4/oos_lock.json`): kalau config berubah, `src.validate` menolak jalan kecuali
diberi `--allow-oos-rerun`, dan perubahan itu harus dicatat di laporan.

## Struktur

```
src/data.py              fetch, update inkremental, validasi, loader
src/indicators.py        SMA, RSI, ATR (Wilder), Donchian
src/strategies/          S1 mean_reversion.py, S2 trend.py — fungsi murni: data masuk, sinyal keluar
src/costs.py             fee, slippage, funding
src/sizing.py            ukuran posisi, batas leverage, cek likuidasi
src/backtest.py          engine bar-per-bar; asumsi eksekusi tertulis di docstring
src/metrics.py           metrik performa
src/run.py               runner M3
src/validate.py          M4
src/report.py            M5
reports/notes.md         catatan analis (ditulis tangan, terikat ke hash OOS)
```

Strategi tidak tahu soal saldo atau order, jadi bisa dipakai ulang di fase 2: bot harian
cukup memanggil `generate_signals()` pada candle terakhir yang sudah close.
