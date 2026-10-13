"""T2 — Donchian breakout dengan chandelier exit (trailing).

Entry sama dengan S2 (breakout channel N bar sebelumnya). Exit long saat close turun di bawah
high tertinggi `chandelier_period` bar dikurangi `chandelier_mult` × ATR; short sebaliknya.
Stop awal tetap atr_mult × ATR sebagai pengaman. Fungsi murni: data masuk, sinyal keluar.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import indicators as ind


def generate_signals(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    h, l, c = bars["high"], bars["low"], bars["close"]
    up, lo = ind.donchian(h, l, int(p["entry_period"]))
    a = ind.atr(h, l, c, int(p["atr_period"]))
    n = int(p["chandelier_period"])
    hh = h.rolling(n, min_periods=n).max()
    ll = l.rolling(n, min_periods=n).min()
    long_entry = c > up
    short_entry = (c < lo) & bool(p["allow_short"])
    return pd.DataFrame({
        "entry": np.where(long_entry, 1, np.where(short_entry, -1, 0)),
        "exit_long": c < hh - p["chandelier_mult"] * a,
        "exit_short": c > ll + p["chandelier_mult"] * a,
        "stop_dist": p["atr_mult"] * a,
    }, index=bars.index)
