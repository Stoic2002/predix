import pytest

from src import sizing
from src.backtest import BacktestConfig, Sleeve, run_backtest
from tests.helpers import flat_bars, make_signals, no_funding

BASE = dict(equity=10_000.0, entry_price=100.0, direction=1, risk_frac=0.01,
            max_leverage=2.0, max_total_notional=3.0, open_notional=0.0,
            isolated_leverage=3.0, maintenance_margin_rate=0.005, liq_buffer=1.5)


def test_risk_is_exactly_one_percent():
    r = sizing.size_position(**BASE, stop_dist=5.0)
    assert r.rejected is None and r.capped_by is None
    assert r.qty == pytest.approx(20.0)
    assert r.risk_amount == pytest.approx(100.0)  # 1% × 10.000


def test_leverage_cap_per_position():
    r = sizing.size_position(**BASE, stop_dist=0.2)  # mentah: 500 unit = 50.000 notional
    assert r.capped_by == "leverage"
    assert r.notional == pytest.approx(20_000.0)  # 2 × ekuitas
    assert r.risk_amount < 100.0


def test_total_notional_cap_and_full_book():
    r = sizing.size_position(**{**BASE, "open_notional": 25_000.0}, stop_dist=0.5)
    assert r.capped_by == "total_notional"
    assert r.notional == pytest.approx(5_000.0)   # 30.000 - 25.000
    r = sizing.size_position(**{**BASE, "open_notional": 30_000.0}, stop_dist=5.0)
    assert r.rejected and "total notional" in r.rejected


def test_liquidation_price_isolated():
    assert sizing.liquidation_price(100, 1, 3, 0.005) == pytest.approx(67.1666667)
    assert sizing.liquidation_price(100, -1, 3, 0.005) == pytest.approx(132.8333333)


def test_rejects_when_liquidation_too_close_to_stop():
    # jarak likuidasi 32,83; 1,5 × 25 = 37,5 -> tolak. 1,5 × 20 = 30 -> boleh.
    assert sizing.size_position(**BASE, stop_dist=25.0).rejected
    assert sizing.size_position(**{**BASE, "direction": -1}, stop_dist=25.0).rejected
    assert sizing.size_position(**BASE, stop_dist=20.0).rejected is None


def test_engine_applies_one_percent_and_logs_rejection():
    bars = flat_bars(6)
    ok = make_signals(bars.index, {0: 1}, stop_dist=5.0)
    res = run_backtest({"X": bars}, {"X": no_funding()}, [Sleeve("a", "X", ok)], BacktestConfig())
    t = res.trades.iloc[0]
    assert t.risk_amount == pytest.approx(0.01 * 10_000)

    too_wide = make_signals(bars.index, {0: 1}, stop_dist=30.0)
    res = run_backtest({"X": bars}, {"X": no_funding()}, [Sleeve("a", "X", too_wide)], BacktestConfig())
    assert res.trades.empty
    assert "likuidasi" in res.rejections.iloc[0].reason


def test_portfolio_splits_risk_equally():
    bars = flat_bars(6)
    sig = make_signals(bars.index, {0: 1}, stop_dist=5.0)
    sleeves = [Sleeve(n, "X", sig) for n in ("a", "b", "c", "d")]
    res = run_backtest({"X": bars}, {"X": no_funding()}, sleeves, BacktestConfig(fee_taker=0, slippage=0))
    assert res.risk_per_trade == pytest.approx(0.0025)
    assert res.trades.risk_amount.tolist() == pytest.approx([25.0] * 4)
