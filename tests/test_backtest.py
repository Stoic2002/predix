"""Aturan eksekusi engine: next-open, stop, gap, prioritas stop, time stop, reversal, lookahead."""
import numpy as np
import pandas.testing as pdt
import pytest

from src import indicators as ind
from src.backtest import BacktestConfig, Sleeve, run_backtest
from tests.helpers import NO_COST, flat_bars, make_bars, make_signals, no_funding, random_bars


def run(bars, sig, cfg=NO_COST, **kw):
    sleeve = Sleeve("a", "X", sig, max_hold_bars=kw.pop("max_hold_bars", None))
    return run_backtest({"X": bars}, {"X": no_funding()}, [sleeve], cfg, **kw)


def test_entry_and_exit_at_next_open():
    bars = make_bars([(100, 101, 99, 100), (102, 103, 101, 102), (104, 105, 103, 104),
                      (106, 107, 105, 106), (108, 109, 107, 108)])
    t = run(bars, make_signals(bars.index, {0: 1}, exit_long=[2])).trades.iloc[0]
    assert t.entry_time == bars.index[1] and t.entry_price == 102
    assert t.exit_time == bars.index[3] and t.exit_price == 106
    assert t.exit_reason == "signal"


def test_stop_fills_at_stop_price():
    bars = make_bars([(100, 100, 100, 100), (100, 101, 99, 100), (100, 100, 85, 95), (95, 95, 95, 95)])
    t = run(bars, make_signals(bars.index, {0: 1}, stop_dist=10)).trades.iloc[0]
    assert t.exit_reason == "stop" and t.exit_price == pytest.approx(90.0)
    assert t.exit_time == bars.index[2]


def test_gap_through_stop_fills_at_open():
    bars = make_bars([(100, 100, 100, 100), (100, 101, 99, 100), (80, 82, 78, 81), (81, 81, 81, 81)])
    t = run(bars, make_signals(bars.index, {0: 1}, stop_dist=10)).trades.iloc[0]
    assert t.exit_reason == "stop" and t.exit_price == pytest.approx(80.0)


def test_short_stop_and_gap():
    bars = make_bars([(100, 100, 100, 100), (100, 101, 99, 100), (115, 120, 114, 118), (118,) * 4])
    t = run(bars, make_signals(bars.index, {0: -1}, stop_dist=10)).trades.iloc[0]
    assert t.exit_reason == "stop" and t.exit_price == pytest.approx(115.0)  # gap di atas stop 110


def test_stop_beats_exit_signal_on_same_bar():
    bars = make_bars([(100, 100, 100, 100), (100, 101, 99, 100), (100, 120, 85, 115), (115,) * 4])
    sig = make_signals(bars.index, {0: 1}, exit_long=[2], stop_dist=10)
    t = run(bars, sig).trades
    assert len(t) == 1
    assert t.iloc[0].exit_reason == "stop" and t.iloc[0].exit_price == pytest.approx(90.0)


def test_stop_can_hit_on_entry_bar():
    bars = make_bars([(100, 100, 100, 100), (100, 101, 85, 95), (95,) * 4])
    t = run(bars, make_signals(bars.index, {0: 1}, stop_dist=10)).trades.iloc[0]
    assert t.exit_reason == "stop" and t.exit_time == bars.index[1] and t.bars_held == 1


def test_time_stop_exits_after_10_days():
    bars = flat_bars(20)
    t = run(bars, make_signals(bars.index, {0: 1}), max_hold_bars=10).trades.iloc[0]
    assert t.exit_reason == "time"
    assert t.entry_time == bars.index[1] and t.exit_time == bars.index[11]
    assert t.bars_held == 10


def test_reversal_closes_then_opens_opposite_one_bar_later():
    bars = flat_bars(10)
    # long dari bar1; sinyal short di close bar3 -> exit open bar4, short open bar5.
    sig = make_signals(bars.index, {0: 1, 3: -1})
    tr = run(bars, sig).trades
    assert tr.iloc[0].side == "long" and tr.iloc[0].exit_reason == "reversal"
    assert tr.iloc[0].exit_time == bars.index[4]
    assert tr.iloc[1].side == "short" and tr.iloc[1].entry_time == bars.index[5]
    assert tr.iloc[1].signal_time == bars.index[3]


def test_open_position_closed_at_end():
    bars = flat_bars(5)
    t = run(bars, make_signals(bars.index, {0: 1})).trades.iloc[0]
    assert t.exit_reason == "end" and t.exit_time == bars.index[-1]
    assert run(bars, make_signals(bars.index, {0: 1})).equity.n_open.iloc[-1] == 0


def test_start_end_window_uses_warm_signals():
    bars = flat_bars(30)
    sig = make_signals(bars.index, {12: 1}, exit_long=[15])
    res = run(bars, sig, start=bars.index[10], end=bars.index[20])
    assert res.equity.index[0] == bars.index[10] and res.equity.index[-1] == bars.index[20]
    assert res.trades.iloc[0].entry_time == bars.index[13]


def _simple_strategy(b):
    """Strategi uji: long saat close > SMA(20), short saat close < SMA(20)."""
    m = ind.sma(b.close, 20)
    a = ind.atr(b.high, b.low, b.close, 14)
    sig = make_signals(b.index, stop_dist=0)
    sig["entry"] = np.where(b.close > m * 1.02, 1, np.where(b.close < m * 0.98, -1, 0))
    sig["exit_long"] = b.close < m
    sig["exit_short"] = b.close > m
    sig["stop_dist"] = 3 * a
    return sig


@pytest.mark.parametrize("t", [150, 300])
def test_engine_no_lookahead(t):
    """Mengubah bar setelah t tidak mengubah ekuitas atau trade yang selesai sampai t."""
    b = random_bars(400, seed=3)
    b2 = b.copy()
    b2.iloc[t + 1:, :4] *= np.random.default_rng(7).uniform(0.6, 1.4, (len(b) - t - 1, 1))
    cfg = BacktestConfig()
    r1 = run(b, _simple_strategy(b), cfg)
    r2 = run(b2, _simple_strategy(b2), cfg)
    assert len(r1.trades) > 5
    pdt.assert_frame_equal(r1.equity.iloc[: t + 1], r2.equity.iloc[: t + 1])
    done1 = r1.trades[r1.trades.exit_time <= b.index[t]]
    done2 = r2.trades[r2.trades.exit_time <= b.index[t]]
    pdt.assert_frame_equal(done1.reset_index(drop=True), done2.reset_index(drop=True))


def test_equity_reconciles_with_trades():
    b = random_bars(400, seed=5)
    res = run(b, _simple_strategy(b), BacktestConfig())
    assert res.equity.equity.iloc[-1] == pytest.approx(10_000 + res.trades.net_pnl.sum())


def test_symbol_listed_mid_period_trades_only_after_listing():
    early = flat_bars(30)
    late = flat_bars(20, start=str(early.index[10].date()))
    sig_late = make_signals(late.index.union(early.index), {12: 1}, exit_long=[15])
    res = run_backtest({"A": early, "B": late}, {"A": no_funding(), "B": no_funding()},
                       [Sleeve("a", "A", make_signals(early.index)), Sleeve("b", "B", sig_late)], NO_COST)
    assert len(res.equity) == 30
    assert res.trades.iloc[0].entry_time == early.index[13]


def test_gap_after_listing_still_rejected():
    early = flat_bars(30)
    late = flat_bars(20, start=str(early.index[10].date())).drop(early.index[15])
    with pytest.raises(ValueError, match="bar hilang"):
        run_backtest({"A": early, "B": late}, {"A": no_funding(), "B": no_funding()},
                     [Sleeve("a", "A", make_signals(early.index)), Sleeve("b", "B", make_signals(early.index))],
                     NO_COST)
