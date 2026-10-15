import numpy as np
import pandas as pd
import pytest

from src.backtest import BacktestConfig, Sleeve, run_backtest
from src.strategies import intraday

ORB = {"range_hours": 4, "last_entry_hour": 20, "atr_period": 24, "atr_mult": 2.0, "allow_short": True}
MOM = {"signal_hour": 12, "threshold": 2.0, "atr_period": 24, "atr_mult": 2.0, "allow_short": True}
REV = {"window": 24, "z_entry": 2.5, "max_hold_bars": 6, "last_entry_hour": 20, "atr_period": 24,
       "atr_mult": 2.0, "allow_short": True}


def hourly(closes, start="2024-01-01") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(closes), freq="h", tz="UTC", name="time")
    c = pd.Series(closes, index=idx, dtype=float)
    o = c.shift(1).fillna(c.iloc[0])
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.1, "low": np.minimum(o, c) - 0.1,
                         "close": c, "volume": 1.0})


def random_hourly(n=24 * 60, seed=0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return hourly(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))))


def test_orb_first_breakout_only_and_exit_at_day_end():
    day1 = [100.0] * 24
    day2 = [100, 100.5, 99.8, 100.2] + [100.0] * 2 + [102.0, 103.0, 98.0] + [100.0] * 15
    sig = intraday.orb(hourly(day1 + day2), ORB)
    d2 = sig.iloc[24:]
    assert d2["entry"].tolist().count(1) == 1 and d2["entry"].iloc[6] == 1   # 06:00 close 102 > 100,6
    assert (d2["entry"] == -1).sum() == 0          # breakout bawah setelahnya diabaikan (hanya yang pertama)
    assert d2["exit_long"].iloc[23] and d2["exit_short"].iloc[23] and not d2["exit_long"].iloc[22]


def test_orb_long_only_skips_short_breakout():
    day = [100, 100.5, 99.8, 100.2, 100, 98.0, 102.0] + [100.0] * 17
    sig = intraday.orb(hourly(day), {**ORB, "allow_short": False})
    assert (sig["entry"] == 1).sum() == 1 and sig["entry"].iloc[6] == 1


def test_momentum_only_at_signal_hour():
    sig = intraday.intraday_momentum(random_hourly(seed=1), MOM)
    hours = sig.index[sig["entry"] != 0].hour
    assert len(hours) > 0 and set(hours) == {11}


def test_reversion_entries_before_cutoff():
    sig = intraday.intraday_reversion(random_hourly(seed=2), {**REV, "z_entry": 1.5})
    hours = sig.index[sig["entry"] != 0].hour
    assert len(hours) > 0 and hours.max() <= 20


@pytest.mark.parametrize("fn,p", [(intraday.orb, ORB), (intraday.intraday_momentum, MOM),
                                  (intraday.intraday_reversion, REV)])
def test_no_lookahead(fn, p):
    bars = random_hourly(seed=3)
    full = fn(bars, p)
    cut = 24 * 30 + 13  # potong di tengah hari
    part = fn(bars.iloc[:cut], p)
    pd.testing.assert_frame_equal(full.iloc[:cut], part)


@pytest.mark.parametrize("fn,p", [(intraday.orb, ORB), (intraday.intraday_momentum, {**MOM, "threshold": 0.5}),
                                  (intraday.intraday_reversion, {**REV, "z_entry": 1.5})])
def test_engine_never_holds_overnight(fn, p):
    bars = random_hourly(seed=4)
    sig = fn(bars, p)
    hold = p.get("max_hold_bars")
    res = run_backtest({"X": bars}, {"X": pd.DataFrame({"funding_rate": []}, index=pd.DatetimeIndex([], tz="UTC"))},
                       [Sleeve("a", "X", sig, hold)], BacktestConfig(), bar="h")
    t = res.trades[res.trades.exit_reason != "end"] if "exit_reason" in res.trades else res.trades
    assert len(t) > 5
    entry, exit_ = pd.to_datetime(t["entry_time"]), pd.to_datetime(t["exit_time"])
    assert (exit_ <= entry.dt.floor("D") + pd.Timedelta(days=1)).all()
