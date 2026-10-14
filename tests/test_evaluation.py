import math

import pandas as pd
import pytest

from src import evaluation
from src.strategies import trend, tsmom
from tests.helpers import random_bars

DONCHIAN = {"allow_short": True, "entry_period": 55, "exit_period": 20, "atr_period": 14, "atr_mult": 3.0}
TSMOM = {"lookback": 60, "atr_period": 14, "atr_mult": 3.0, "allow_short": False}


def with_close(bars: pd.DataFrame, close: float) -> pd.DataFrame:
    """Tambah satu candle baru dengan close tertentu (high/low tidak ikut dihitung di sinyalnya sendiri)."""
    t = bars.index[-1] + pd.Timedelta(days=1)
    row = pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1.0},
                       index=pd.DatetimeIndex([t], name="time"))
    return pd.concat([bars, row])


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_donchian_triggers_match_strategy(seed):
    bars = random_bars(300, seed=seed)
    t = evaluation.triggers("donchian", DONCHIAN, bars)
    sig = lambda c: trend.generate_signals(with_close(bars, c), DONCHIAN).iloc[-1]  # noqa: E731
    assert sig(t["long_entry"] * 1.0001)["entry"] == 1
    assert sig(t["long_entry"] * 0.9999)["entry"] != 1
    assert sig(t["short_entry"] * 0.9999)["entry"] == -1
    assert sig(t["short_entry"] * 1.0001)["entry"] != -1
    assert sig(t["long_exit"] * 0.9999)["exit_long"] and not sig(t["long_exit"] * 1.0001)["exit_long"]
    assert sig(t["short_exit"] * 1.0001)["exit_short"] and not sig(t["short_exit"] * 0.9999)["exit_short"]


@pytest.mark.parametrize("seed", [0, 3])
def test_tsmom_trigger_matches_strategy(seed):
    bars = random_bars(300, seed=seed)
    t = evaluation.triggers("tsmom", TSMOM, bars)
    assert t["short_entry"] is None  # long only
    sig = lambda c: tsmom.generate_signals(with_close(bars, c), TSMOM).iloc[-1]  # noqa: E731
    assert sig(t["long_entry"] * 1.0001)["entry"] == 1
    assert sig(t["long_entry"] * 0.9999)["entry"] == 0
    assert sig(t["long_exit"] * 0.9999)["exit_long"] and not sig(t["long_exit"] * 1.0001)["exit_long"]


def test_return_band_percentiles():
    idx = pd.date_range("2024-01-01", periods=101, freq="D", tz="UTC")
    eq = pd.Series(100.0 * 1.01 ** pd.Series(range(101)).values, index=idx)  # naik 1% per hari
    lo, mid, hi = evaluation.return_band(eq, 10, (5, 95))
    assert lo == pytest.approx(1.01 ** 10 - 1) and hi == pytest.approx(lo) and mid == pytest.approx(lo)
    assert all(math.isnan(x) for x in evaluation.return_band(eq, 200, (5, 95)))


CFG = {"evaluation": {"reference_start": "2024-01-01", "verdict_after_days": 183, "return_band_pct": [5, 95],
                      "drawdown_warn_x": 1.5, "drawdown_fail_x": 2.0, "min_trades_for_pf": 10, "pf_warn": 1.0,
                      "pf_fail": 0.8, "min_days_for_frequency": 60, "frequency_band_x": [0.5, 2.0]},
       "bot": {"start": "2026-01-01"}}
IDX = pd.date_range("2024-01-01", periods=400, freq="D", tz="UTC")
REF = {"equity": pd.Series(10_000 * (1 + 0.001 * pd.Series(range(400)).values), index=IDX), "max_drawdown": 0.10,
       "profit_factor": 1.5, "trades_per_month": 4.0, "n_trades": 50, "median_gap_days": 7.0,
       "longest_idle_days": 40.0, "flat_share": 0.3}


def run_eval(monkeypatch, **stats):
    base = {"days": 30.0, "return": 0.03, "max_drawdown": 0.05, "n_trades": 3, "profit_factor": math.nan,
            "entries": 4, "kill_switch": False, "last_fill": None}
    monkeypatch.setattr(evaluation, "paper_stats", lambda cfg, section: {**base, **stats})
    e = evaluation.evaluate(CFG, "bot", REF)
    return {c["aspek"]: c["status"] for c in e["checks"]}, e["verdict"]


def test_evaluate_waits_for_enough_data(monkeypatch):
    st, verdict = run_eval(monkeypatch)
    assert st["Profit factor"] == "wait" and st["Frekuensi trade"] == "wait"
    assert st["Turun terdalam (drawdown)"] == "ok" and st["Kill switch"] == "ok"
    assert verdict[0].startswith("Berjalan")


def test_evaluate_flags(monkeypatch):
    st, _ = run_eval(monkeypatch, max_drawdown=0.17)
    assert st["Turun terdalam (drawdown)"] == "warn"
    st, verdict = run_eval(monkeypatch, max_drawdown=0.25)
    assert st["Turun terdalam (drawdown)"] == "fail" and verdict[1] == "fail"
    st, _ = run_eval(monkeypatch, **{"return": -0.5})
    assert st["Return vs kisaran normal"] == "fail"
    st, _ = run_eval(monkeypatch, n_trades=12, profit_factor=0.9)
    assert st["Profit factor"] == "warn"
    st, _ = run_eval(monkeypatch, n_trades=12, profit_factor=0.5)
    assert st["Profit factor"] == "fail"
    st, _ = run_eval(monkeypatch, days=90.0, entries=1)
    assert st["Frekuensi trade"] == "warn"  # 0,3/bulan vs 4/bulan
    st, _ = run_eval(monkeypatch, kill_switch=True)
    assert st["Kill switch"] == "fail"


def test_final_verdict(monkeypatch):
    _, verdict = run_eval(monkeypatch, days=190.0, n_trades=20, profit_factor=1.3, entries=25, **{"return": 0.20})
    assert verdict == ("LOLOS", "ok")
    _, verdict = run_eval(monkeypatch, days=190.0, n_trades=20, profit_factor=0.95, entries=25, **{"return": 0.20})
    assert verdict == ("GAGAL", "fail")  # PF < 1 walau hanya kuning
