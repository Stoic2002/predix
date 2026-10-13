"""S1 — Mean reversion: RSI(2) dengan filter tren SMA(200).

Fungsi murni: OHLCV masuk, sinyal keluar. Tidak tahu soal saldo, posisi, atau order.
Time stop (max_hold_days) dijalankan engine karena butuh tahu kapan posisi dibuka.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import indicators as ind


def generate_signals(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    c = bars["close"]
    trend = ind.sma(c, int(p["sma_period"]))
    r = ind.rsi(c, int(p["rsi_period"]))
    a = ind.atr(bars["high"], bars["low"], c, int(p["atr_period"]))

    long_entry = (c > trend) & (r < p["rsi_entry_long"])
    short_entry = (c < trend) & (r > p["rsi_entry_short"]) & bool(p["allow_short"])
    return pd.DataFrame({
        "entry": np.where(long_entry, 1, np.where(short_entry, -1, 0)),
        "exit_long": r > p["rsi_exit_long"],
        "exit_short": r < p["rsi_exit_short"],
        "stop_dist": p["atr_mult"] * a,
    }, index=bars.index)
