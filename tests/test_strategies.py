import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from src import indicators as ind
from src.data import load_config
from src.strategies import mean_reversion, trend
from tests.helpers import make_bars, random_bars

CFG = load_config()
S1, S2 = CFG["s1"], CFG["s2"]


@pytest.mark.parametrize("gen,params", [(mean_reversion.generate_signals, S1),
                                        (trend.generate_signals, S2)])
@pytest.mark.parametrize("t", [260, 400, 550])
def test_signals_no_lookahead(gen, params, t):
    """Mengubah data setelah t tidak mengubah sinyal sebelum atau pada t."""
    b = random_bars(600, seed=11)
    b2 = b.copy()
    b2.iloc[t + 1:, :4] *= np.random.default_rng(5).uniform(0.5, 1.5, (len(b) - t - 1, 1))
    pdt.assert_frame_equal(gen(b, params).iloc[: t + 1], gen(b2, params).iloc[: t + 1])


def test_s1_rules_match_definition():
    b = random_bars(800, seed=2)
    sig = mean_reversion.generate_signals(b, S1)
    c, sma, r = b.close, ind.sma(b.close, 200), ind.rsi(b.close, 2)
    exp_long = (c > sma) & (r < 10)
    exp_short = (c < sma) & (r > 90)
    assert ((sig.entry == 1) == exp_long).all()
    assert ((sig.entry == -1) == exp_short).all()
    assert (sig.exit_long == (r > 70)).all() and (sig.exit_short == (r < 30)).all()
    assert exp_long.sum() > 0 and exp_short.sum() > 0
    assert (sig.entry.iloc[:200] == 0).all()  # SMA(200) belum panas


def test_s1_allow_short_false_removes_shorts():
    b = random_bars(800, seed=2)
    sig = mean_reversion.generate_signals(b, {**S1, "allow_short": False})
    assert (sig.entry >= 0).all()


def test_s2_breakout_uses_previous_bars_only():
    # 55 bar datar di 100, lalu bar 55 close 101 dengan high 150.
    rows = [(100, 100, 100, 100)] * 55 + [(100, 150, 99, 101), (101, 101, 101, 101)]
    b = make_bars(rows)
    sig = trend.generate_signals(b, {**S2, "atr_period": 2})
    assert sig.entry.iloc[55] == 1           # 101 > max high 55 bar sebelumnya (100)
    assert sig.entry.iloc[56] == 0           # 101 tidak > 150 (high bar 55 kini masuk channel)
    assert sig.entry.iloc[:55].eq(0).all()


def test_s2_short_and_exit_channel():
    rows = [(100, 100, 100, 100)] * 55 + [(100, 100, 90, 95)]
    sig = trend.generate_signals(make_bars(rows), S2)
    assert sig.entry.iloc[55] == -1
    assert sig.exit_long.iloc[55]            # 95 < low 20 bar sebelumnya
    assert not sig.exit_short.iloc[55]
