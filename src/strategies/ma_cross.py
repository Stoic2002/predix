"""T4 — Persilangan moving average: long saat SMA cepat > SMA lambat, short sebaliknya.

Keluar saat urutan SMA berbalik. Stop awal atr_mult × ATR sebagai pengaman.
Fungsi murni: data masuk, sinyal keluar.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import indicators as ind


def generate_signals(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    c = bars["close"]
    fast, slow = ind.sma(c, int(p["fast"])), ind.sma(c, int(p["slow"]))
    a = ind.atr(bars["high"], bars["low"], c, int(p["atr_period"]))
    long_entry = fast > slow
    short_entry = (fast < slow) & bool(p["allow_short"])
    return pd.DataFrame({
        "entry": np.where(long_entry, 1, np.where(short_entry, -1, 0)),
        "exit_long": fast <= slow,
        "exit_short": fast >= slow,
        "stop_dist": p["atr_mult"] * a,
    }, index=bars.index)
