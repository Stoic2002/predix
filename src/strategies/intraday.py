"""Putaran 4 — strategi intraday di candle 1 jam (jam UTC, indeks = waktu open candle).

Semua posisi ditutup paling lambat di open 00:00 UTC: sinyal exit di candle 23:00 (yang close 00:00),
dieksekusi engine di open candle berikutnya. Entry juga dieksekusi di open candle setelah sinyal.
Fungsi murni: data masuk, sinyal keluar.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import indicators as ind


def _frame(bars: pd.DataFrame, long_entry, short_entry, exit_long, exit_short, p: dict) -> pd.DataFrame:
    eod = pd.Series(bars.index.hour == 23, index=bars.index)
    a = ind.atr(bars["high"], bars["low"], bars["close"], int(p["atr_period"]))
    short_entry = short_entry & bool(p["allow_short"])
    return pd.DataFrame({
        "entry": np.where(long_entry, 1, np.where(short_entry, -1, 0)),
        "exit_long": exit_long | eod,
        "exit_short": exit_short | eod,
        "stop_dist": p["atr_mult"] * a,
    }, index=bars.index)


def orb(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    """Opening range breakout: rentang = high/low `range_hours` candle pertama hari itu (harus lengkap).
    Entry hanya di breakout PERTAMA hari itu (close menembus rentang), sampai `last_entry_hour`."""
    h, l, c = bars["high"], bars["low"], bars["close"]
    hour = pd.Series(bars.index.hour, index=bars.index)
    day = bars.index.floor("D")
    n = int(p["range_hours"])
    in_range = hour < n
    hi = h.where(in_range).groupby(day).transform("max")
    lo = l.where(in_range).groupby(day).transform("min")
    complete = in_range.groupby(day).transform("sum") == n
    window = complete & (hour >= n) & (hour <= int(p["last_entry_hour"]))
    up = window & (c > hi)
    dn = window & (c < lo) & bool(p["allow_short"])
    first = (up | dn) & ((up | dn).groupby(day).cumsum() == 1)
    none = pd.Series(False, index=bars.index)
    return _frame(bars, first & up, first & dn, none, none, p)


def intraday_momentum(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    """Di close candle (signal_hour - 1): gerak sejak open 00:00 > threshold x ATR -> ikut arahnya sampai 00:00."""
    o, c = bars["open"], bars["close"]
    hour = pd.Series(bars.index.hour, index=bars.index)
    day = bars.index.floor("D")
    day_open = o.where(hour == 0).groupby(day).transform("first")
    a = ind.atr(bars["high"], bars["low"], c, int(p["atr_period"]))
    move = c - day_open
    at = hour == int(p["signal_hour"]) - 1
    none = pd.Series(False, index=bars.index)
    return _frame(bars, at & (move > p["threshold"] * a), at & (move < -p["threshold"] * a), none, none, p)


def intraday_reversion(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    """z = (close - SMA) / stdev `window` jam. Beli saat z < -z_entry, short saat z > z_entry (sampai
    last_entry_hour); keluar saat close kembali ke SMA, setelah max_hold_bars (engine), atau 00:00."""
    c = bars["close"]
    hour = pd.Series(bars.index.hour, index=bars.index)
    w = int(p["window"])
    sma = c.rolling(w, min_periods=w).mean()
    z = (c - sma) / c.rolling(w, min_periods=w).std()
    ok = hour <= int(p["last_entry_hour"])
    return _frame(bars, ok & (z < -p["z_entry"]), ok & (z > p["z_entry"]), c >= sma, c <= sma, p)
