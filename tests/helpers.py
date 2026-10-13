import numpy as np
import pandas as pd

from src.backtest import BacktestConfig

NO_COST = BacktestConfig(fee_taker=0.0, slippage=0.0)


def make_bars(rows, start="2024-01-01") -> pd.DataFrame:
    """rows: list (open, high, low, close)."""
    idx = pd.date_range(start, periods=len(rows), freq="D", tz="UTC", name="time")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)
    return df.assign(volume=1.0)


def flat_bars(n, price=100.0, start="2024-01-01") -> pd.DataFrame:
    return make_bars([(price, price, price, price)] * n, start)


def random_bars(n, seed=0, start="2020-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, n)))
    open_ = np.r_[100.0, close[:-1]] * np.exp(rng.normal(0, 0.005, n))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.03, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.03, n))
    return make_bars(list(zip(open_, high, low, close)), start)


def make_signals(index, entries=None, exit_long=(), exit_short=(), stop_dist=10.0) -> pd.DataFrame:
    """entries: {posisi bar: +1/-1}; exit_*: posisi bar dengan sinyal exit."""
    df = pd.DataFrame({"entry": 0, "exit_long": False, "exit_short": False,
                       "stop_dist": float(stop_dist)}, index=index)
    for i, d in (entries or {}).items():
        df.iloc[i, df.columns.get_loc("entry")] = d
    for i in exit_long:
        df.iloc[i, df.columns.get_loc("exit_long")] = True
    for i in exit_short:
        df.iloc[i, df.columns.get_loc("exit_short")] = True
    return df


def no_funding() -> pd.DataFrame:
    return pd.DataFrame({"funding_rate": pd.Series(dtype=float)},
                        index=pd.DatetimeIndex([], tz="UTC", name="time"))


def funding_every_8h(index, rate) -> pd.DataFrame:
    times = pd.date_range(index[0], index[-1] + pd.Timedelta(hours=16), freq="8h", tz="UTC", name="time")
    return pd.DataFrame({"funding_rate": rate}, index=times)
