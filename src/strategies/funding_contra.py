"""F1 — Funding kontra: lawan posisi kerumunan saat funding rate ekstrem dibanding sejarahnya sendiri.

Rata-rata funding harian `avg_days` hari dibandingkan dengan sebarannya selama `window` hari terakhir
(kausal: hanya data sampai close hari ini). Di atas persentil (1 - tail): pasar terlalu optimis -> short
(kalau allow_short). Di bawah persentil tail: terlalu pesimis -> beli. Keluar saat kembali ke median
atau setelah max_hold_days. Stop awal atr_mult × ATR.

Butuh kolom `funding` di bars: jumlah funding rate semua event yang jatuh di hari itu (UTC).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import indicators as ind

NEEDS_FUNDING = True


def generate_signals(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    f = bars["funding"]
    n, w, tail = int(p["avg_days"]), int(p["window"]), float(p["tail"])
    avg = f.rolling(n, min_periods=n).mean()
    roll = avg.rolling(w, min_periods=int(p["min_window"]))
    hi, lo, mid = roll.quantile(1 - tail), roll.quantile(tail), roll.quantile(0.5)
    a = ind.atr(bars["high"], bars["low"], bars["close"], int(p["atr_period"]))
    long_entry = avg < lo
    short_entry = (avg > hi) & bool(p["allow_short"])
    return pd.DataFrame({
        "entry": np.where(long_entry, 1, np.where(short_entry, -1, 0)),
        "exit_long": avg >= mid,
        "exit_short": avg <= mid,
        "stop_dist": p["atr_mult"] * a,
    }, index=bars.index)
