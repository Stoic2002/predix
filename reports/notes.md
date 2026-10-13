<!-- oos_hash: 7bf6e48972648fda — catatan ini ditulis untuk run OOS 2026-10-12 06:11 UTC.
Kalau OOS dijalankan ulang dengan config lain, report.py akan memberi peringatan bahwa catatan ini basi. -->

**S1 (mean reversion RSI(2)) tidak punya edge setelah biaya di data ini.** Hasilnya rugi di
in-sample (PF 0,69), di out-of-sample (PF 0,88), dan di ke-13 varian parameter. Profilnya sesuai
hipotesis, sering menang kecil (winrate ~60%), tapi rata-rata kemenangannya (+0,25R) tidak
menutup rata-rata kerugiannya (−0,4 s/d −0,6R). Sisi short-nya rugi di kedua periode dan kedua
simbol. Kegagalan S1 tidak bergantung pada angka parameter.

**S2 (Donchian breakout) hanya gagal di jumlah trade (60 dari syarat 100).** Profit-nya bertahan di
out-of-sample (PF 1,56), semua varian sensitivitasnya profit, dan 6 dari 10 jendela walk-forward
profit. Tapi bukti edge-nya tipis:

- Hanya 60 trade dalam ~6,8 tahun. Dengan sampel sekecil itu, PF 1,56 masih bisa terjadi karena kebetulan.
- Di in-sample, satu trade BTC (Okt 2020–Mei 2021, +1.567) lebih besar dari total profit bersih S2-BTC.
- Di out-of-sample, PF S2-BTC hanya 1,03; hampir seluruh profit datang dari ETH.
- Sharpe S2 di bawah buy-and-hold di kedua periode (OOS 0,29 vs BTC 0,76 / ETH 0,39), meski
  drawdown-nya jauh lebih kecil.
- Funding memakan sebagian besar profit trend panjang (S2-BTC in-sample: funding 1.149 vs
  profit bersih 1.320), karena posisi long trend terbuka saat funding paling mahal.

**Diversifikasi bekerja secara statistik** (korelasi S1–S2 0,06 IS / 0,20 OOS), tapi tidak berguna
karena salah satu komponennya (S1) tidak punya edge.

**Return absolut kecil karena desainnya.** Risiko 1% per trade dengan stop 3×ATR menghasilkan
notional rata-rata sekitar 0,1x ekuitas. Return dan drawdown kira-kira berbanding lurus dengan
risiko per trade, jadi menaikkan risiko tidak mengubah profit factor maupun Sharpe.

**Langkah yang masuk akal berikutnya** (semuanya keputusan kamu, bukan rekomendasi untuk
meloloskan strategi):

1. Terima hasil ini apa adanya dan jangan lanjut ke fase 2.
2. Tinjau ulang kriteria ≥100 trade untuk strategi trend harian. Keputusan itu harus dicatat
   sebagai perubahan **setelah** melihat OOS, dan S2 sebaiknya diuji di data yang belum
   tersentuh, misalnya beberapa bulan paper trading atau simbol lain, sebelum dianggap lolos.
3. Uji hipotesis baru (simbol lain, varian strategi) sebagai riset baru dengan pembagian data
   baru. Data OOS 2024–2026 sudah terpakai dan tidak lagi murni untuk menguji ide yang lahir
   dari laporan ini.
