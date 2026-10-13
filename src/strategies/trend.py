"""S2 — Trend following: Donchian breakout.

Fungsi murni: OHLCV masuk, sinyal keluar. Channel dihitung dari bar sebelumnya,
tidak termasuk bar sinyal. Stop awal tetap (tidak trailing); exit lewat channel pendek.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import indicators as ind


def generate_signals(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    h, l, c = bars["high"], bars["low"], bars["close"]
    entry_up, entry_lo = ind.donchian(h, l, int(p["entry_period"]))
    exit_up, exit_lo = ind.donchian(h, l, int(p["exit_period"]))
    a = ind.atr(h, l, c, int(p["atr_period"]))

    long_entry = c > entry_up
    short_entry = (c < entry_lo) & bool(p["allow_short"])
    return pd.DataFrame({
        "entry": np.where(long_entry, 1, np.where(short_entry, -1, 0)),
        "exit_long": c < exit_lo,
        "exit_short": c > exit_up,
        "stop_dist": p["atr_mult"] * a,
    }, index=bars.index)
