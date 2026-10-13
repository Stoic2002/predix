"""T3 — Time-series momentum: arah posisi = tanda return `lookback` hari terakhir.

Long saat return N hari > 0, short saat < 0 (kalau allow_short). Keluar saat tanda berbalik.
Stop awal atr_mult × ATR sebagai pengaman. Fungsi murni: data masuk, sinyal keluar.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import indicators as ind


def generate_signals(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    c = bars["close"]
    ret = c / c.shift(int(p["lookback"])) - 1
    a = ind.atr(bars["high"], bars["low"], c, int(p["atr_period"]))
    long_entry = ret > 0
    short_entry = (ret < 0) & bool(p["allow_short"])
    return pd.DataFrame({
        "entry": np.where(long_entry, 1, np.where(short_entry, -1, 0)),
        "exit_long": ret <= 0,
        "exit_short": ret >= 0,
        "stop_dist": p["atr_mult"] * a,
    }, index=bars.index)
