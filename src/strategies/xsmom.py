"""X1 — Momentum lintas koin (cross-sectional), rebalance mingguan.

Setiap minggu (candle hari `rebalance_weekday`, default Minggu yang close Senin 00:00 UTC) koin diurutkan
berdasarkan return `lookback` hari. Pegang top_k terkuat; kalau allow_short juga short top_k terlemah.
Koin yang keluar dari kelompoknya ditutup di rebalance berikutnya. Stop awal atr_mult × ATR sebagai pengaman.

Fungsi MULTI-ASET: dict bars masuk, dict sinyal keluar. Peringkat hanya memakai koin yang sudah punya
cukup data pada tanggal itu (koin yang listing belakangan ikut setelah lookback terpenuhi).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import indicators as ind

MULTI = True


def generate_signals(bars: dict[str, pd.DataFrame], p: dict) -> dict[str, pd.DataFrame]:
    k, lookback = int(p["top_k"]), int(p["lookback"])
    closes = pd.DataFrame({s: b["close"] for s, b in bars.items()})  # indeks gabungan, NaN sebelum listing
    ret = closes / closes.shift(lookback) - 1
    avail = ret.notna()
    strongest = ret.rank(axis=1, ascending=False, method="first") <= k
    weakest = ret.rank(axis=1, ascending=True, method="first") <= k
    enough = avail.sum(axis=1) >= 2 * k  # kelompok atas & bawah tidak boleh tumpang tindih
    top = strongest & avail
    bottom = weakest & avail & enough.to_numpy()[:, None] & bool(p["allow_short"])
    rebalance = pd.Series(closes.index.dayofweek == int(p["rebalance_weekday"]), index=closes.index)

    out = {}
    for s, b in bars.items():
        rb = rebalance.reindex(b.index, fill_value=False)
        t = top[s].reindex(b.index, fill_value=False)
        bo = bottom[s].reindex(b.index, fill_value=False)
        a = ind.atr(b["high"], b["low"], b["close"], int(p["atr_period"]))
        out[s] = pd.DataFrame({
            "entry": np.where(rb & t, 1, np.where(rb & bo, -1, 0)),
            "exit_long": rb & ~t,
            "exit_short": rb & ~bo,
            "stop_dist": p["atr_mult"] * a,
        }, index=b.index)
    return out
