# Ringkasan riset: trader top Hyperliquid

Data on-chain publik bursa Hyperliquid, diambil 14 Okt 2026 (`python -m src.hl_study`). Wallet anonim;
laporan memakai nama samaran, alamat tidak disimpan di repo. Bukan saran investasi dan bukan ajakan copy trading.

## Seberapa langka pemenang yang konsisten

| Tahap | Jumlah akun |
| --- | --- |
| Semua akun di leaderboard | 47,269 |
| 200 profit terbesar sepanjang masa (modal ≥ $100 ribu) | 200 |
| Untung di ≥ 60% bulan, riwayat ≥ 6 bulan | **36 (18%)** |
| Dianalisis (punya transaksi perp) | 34 |

## Gaya trading mereka

| Gaya | Jumlah | Transaksi/hari (median) | Lama pegang (jam) | Winrate | Profit factor |
| --- | --- | --- | --- | --- | --- |
| HFT/market maker (limit order) | 9.0 | 8,641 | 0.4 | 66% | 0.29 |
| HFT taker (market order) | 7.0 | 15,529 | 0.0 | 68% | 0.47 |
| intraday (1–24 jam) | 5.0 | 36 | 7.9 | 40% | 1.27 |
| market maker (limit order) | 5.0 | 225 | 66.3 | 60% | 4.19 |
| swing (1–14 hari) | 4.0 | 41 | 50.4 | 40% | 1.27 |
| posisi besar dikelola terus (jarang tutup penuh) | 3.0 | 21 | - | - | - |
| posisi (> 14 hari) | 1.0 | 10 | 1079.3 | 48% | 1.83 |

## Temuan

1. **21 dari 34 (62%) adalah HFT/market maker.** Keunggulan dari kecepatan, fee sangat rendah, dan modal
   besar; tidak bisa ditiru dengan VPS biasa.
2. **Trader directional (10 akun) mirip robot Predix:** winrate median 40%, untung rata-rata
   sekitar 2.0× rugi rata-rata. Banyak rugi kecil, menang dari sedikit trade besar.
3. **Spesialis:** 3 koin teratas mencakup median 76% volume (banyak di HYPE dan pasar khusus
   Hyperliquid seperti minyak/saham).
4. **Leverage cukup tinggi** (median eksposur 4.3× modal saat data diambil), tetapi ini hanya akun
   yang selamat.
5. **Pemenang pun punya periode buruk:** beberapa trader swing/intraday rugi di jendela data terakhirnya.

## Keterbatasan

- Survivorship: akun yang habis tidak muncul di leaderboard, jadi gambarannya terlalu optimis.
- Data transaksi terbatas 10.000 terakhir per akun (untuk HFT hanya beberapa jam).
- Kita melihat apa yang dilakukan, bukan kenapa; posisi lindung nilai di bursa lain tidak terlihat.
