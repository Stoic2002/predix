"""Indikator teknikal. Semua kausal: nilai di bar t hanya memakai data bar <= t.

Smoothing RSI dan ATR memakai metode Wilder (EMA dengan alpha = 1/n).
Nilai selama masa pemanasan (kurang dari n bar) adalah NaN.
"""
from __future__ import annotations

import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's moving average."""
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    up = rma(delta.clip(lower=0), n)
    down = rma(-delta.clip(upper=0), n)
    out = 100 - 100 / (1 + up / down)  # down == 0 -> rs = inf -> 100
    flat = (up == 0) & (down == 0)     # tidak ada pergerakan sama sekali
    return out.mask(flat, 50.0)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1)
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int) -> pd.Series:
    return rma(true_range(high, low, close), n)


def donchian(high: pd.Series, low: pd.Series, n: int) -> tuple[pd.Series, pd.Series]:
    """Channel dari n bar SEBELUMNYA (bar t tidak ikut), sesuai aturan S2."""
    upper = high.rolling(n, min_periods=n).max().shift(1)
    lower = low.rolling(n, min_periods=n).min().shift(1)
    return upper, lower
