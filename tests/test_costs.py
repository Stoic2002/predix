"""Satu trade buatan, dihitung manual: fee, slippage, dan funding."""
import pytest

from src import costs
from src.backtest import BacktestConfig, Sleeve, run_backtest
from tests.helpers import funding_every_8h, make_bars, make_signals, no_funding

CFG = BacktestConfig(fee_taker=0.0005, slippage=0.0005)


def test_primitives():
    assert costs.apply_slippage(100, 1, 0.001) == pytest.approx(100.1)
    assert costs.apply_slippage(100, -1, 0.001) == pytest.approx(99.9)
    assert costs.fee(-2000, 0.0005) == pytest.approx(1.0)
    assert costs.funding_payment(1, 2, 100, 0.0001) == pytest.approx(0.02)    # long bayar
    assert costs.funding_payment(-1, 2, 100, 0.0001) == pytest.approx(-0.02)  # short terima


def _scenario():
    # bar0: sinyal long. bar1: entry di open 100. bar2: sinyal exit. bar3: exit di open 110.
    return make_bars([
        (100, 100, 100, 100),
        (100, 101, 99, 100),
        (100, 111, 99, 110),
        (110, 110, 110, 110),
        (110, 110, 110, 110),
    ])


def test_single_long_trade_costs_by_hand():
    bars = _scenario()
    sig = make_signals(bars.index, {0: 1}, exit_long=[2], stop_dist=10.0)
    fund = funding_every_8h(bars.index, 0.0001)  # 3 event per hari
    res = run_backtest({"X": bars}, {"X": fund}, [Sleeve("a", "X", sig)], CFG)
    t = res.trades.iloc[0]

    entry = 100 * 1.0005                 # 100,05
    qty = 100 / 10                       # 1% × 10.000 / stop 10 = 10
    exit_ = 110 * 0.9995                 # 109,945
    fee_in = qty * entry * 0.0005        # 0,50025
    fee_out = qty * exit_ * 0.0005       # 0,549725
    funding = 6 * qty * 100 * 0.0001     # bar1 + bar2 (open 100), bar3 tidak: 0,6
    gross = qty * (exit_ - entry)        # 98,95

    assert t.entry_price == pytest.approx(entry)
    assert t.exit_price == pytest.approx(exit_)
    assert t.qty == pytest.approx(qty)
    assert t.fees == pytest.approx(fee_in + fee_out)
    assert t.slippage == pytest.approx(qty * (0.05 + 0.055))
    assert t.funding == pytest.approx(funding)
    assert t.gross_pnl == pytest.approx(gross)
    assert t.net_pnl == pytest.approx(gross - fee_in - fee_out - funding)  # 97,300025
    assert res.equity.equity.iloc[-1] == pytest.approx(10_000 + t.net_pnl)
    assert res.sleeve_pnl["a"].iloc[-1] == pytest.approx(t.net_pnl)


def test_short_receives_positive_funding():
    bars = _scenario()
    sig = make_signals(bars.index, {0: -1}, exit_short=[2], stop_dist=20.0)
    res = run_backtest({"X": bars}, {"X": funding_every_8h(bars.index, 0.0001)},
                       [Sleeve("a", "X", sig)], CFG)
    t = res.trades.iloc[0]
    assert t.funding == pytest.approx(-6 * t.qty * 100 * 0.0001)


def test_funding_interval_read_from_data_not_assumed():
    bars = _scenario()
    sig = make_signals(bars.index, {0: 1}, exit_long=[2], stop_dist=10.0)
    import pandas as pd
    times = pd.date_range(bars.index[0], periods=24 * 5, freq="1h", tz="UTC", name="time")
    hourly = pd.DataFrame({"funding_rate": 0.00001}, index=times)
    res = run_backtest({"X": bars}, {"X": hourly}, [Sleeve("a", "X", sig)], CFG)
    t = res.trades.iloc[0]
    assert t.funding == pytest.approx(48 * t.qty * 100 * 0.00001)  # 2 hari × 24 event


def test_no_costs_when_zeroed():
    bars = _scenario()
    sig = make_signals(bars.index, {0: 1}, exit_long=[2], stop_dist=10.0)
    res = run_backtest({"X": bars}, {"X": no_funding()}, [Sleeve("a", "X", sig)],
                       BacktestConfig(fee_taker=0, slippage=0))
    t = res.trades.iloc[0]
    assert t.net_pnl == pytest.approx(10 * (110 - 100))


def test_funding_grouped_per_4h_bar():
    """Candle 4 jam: event funding 8 jam jatuh di bar 00:00, 08:00, 16:00 saja; posisi masuk di bar 1
    dan keluar di open bar 7 membayar event di bar 1..6 (08:00 dan 16:00 hari pertama, lalu 00:00 hari kedua)."""
    import pandas as pd
    idx = pd.date_range("2024-01-01", periods=10, freq="4h", tz="UTC", name="time")
    bars = make_bars([(100, 101, 99, 100)] * 10).set_axis(idx)
    sig = make_signals(idx, {0: 1}, exit_long=[6], stop_dist=10.0)
    fund = pd.DataFrame({"funding_rate": 0.0001},
                        index=pd.date_range(idx[0], idx[-1], freq="8h", tz="UTC", name="time"))
    res = run_backtest({"X": bars}, {"X": fund}, [Sleeve("a", "X", sig)], BacktestConfig(fee_taker=0, slippage=0),
                       bar="4h")
    t = res.trades.iloc[0]
    assert t.entry_time == idx[1] and t.exit_time == idx[7]
    # event di 08:00 (bar 2), 16:00 (bar 4), 00:00 hari ke-2 (bar 6); bar 0 (00:00 hari pertama) sebelum entry
    assert t.funding == pytest.approx(3 * t.qty * 100 * 0.0001)
