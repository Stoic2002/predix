import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from src import indicators as ind
from tests.helpers import random_bars


def test_sma():
    s = pd.Series([1.0, 2, 3, 4, 5])
    out = ind.sma(s, 3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2:].tolist() == [2.0, 3.0, 4.0]


def test_rsi_hand_computed():
    # delta: -, +1, +1, -1. Wilder n=2 (alpha 0.5):
    # bar2: up=1, down=0 -> 100. bar3: up=0.5, down=0.5 -> 50.
    out = ind.rsi(pd.Series([1.0, 2, 3, 2]), 2)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx(100.0)
    assert out.iloc[3] == pytest.approx(50.0)


def test_rsi_flat_series_is_50_and_bounded():
    assert (ind.rsi(pd.Series([5.0] * 10), 2).dropna() == 50.0).all()
    b = random_bars(500)
    r = ind.rsi(b.close, 2).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_true_range_and_atr():
    h = pd.Series([10.0, 12, 11])
    l = pd.Series([8.0, 9, 7])
    c = pd.Series([9.0, 11, 8])
    # TR: 2; max(3, |12-9|, |9-9|)=3; max(4, |11-11|, |7-11|)=4
    assert ind.true_range(h, l, c).tolist() == [2.0, 3.0, 4.0]
    a = ind.atr(h, l, c, 2)
    assert np.isnan(a.iloc[0])
    assert a.iloc[1] == pytest.approx(2.5)        # 0.5*3 + 0.5*2
    assert a.iloc[2] == pytest.approx(3.25)       # 0.5*4 + 0.5*2.5


def test_donchian_excludes_current_bar():
    high = pd.Series([1.0, 2, 3, 10])
    low = pd.Series([5.0, 4, 6, 0])
    up, lo = ind.donchian(high, low, 2)
    assert up.iloc[3] == 3.0   # max(2, 3), bukan 10 dari bar itu sendiri
    assert lo.iloc[3] == 4.0   # min(4, 6), bukan 0
    assert up.iloc[:2].isna().all()


@pytest.mark.parametrize("t", [250, 300, 450])
def test_indicators_no_lookahead(t):
    """Mengubah data setelah bar t tidak boleh mengubah nilai indikator di bar <= t."""
    b = random_bars(500, seed=1)
    b2 = b.copy()
    rng = np.random.default_rng(99)
    b2.iloc[t + 1:, :4] *= rng.uniform(0.5, 1.5, (len(b) - t - 1, 1))

    def all_ind(x):
        up, lo = ind.donchian(x.high, x.low, 55)
        return {
            "sma": ind.sma(x.close, 200), "rsi": ind.rsi(x.close, 2),
            "atr": ind.atr(x.high, x.low, x.close, 14), "dc_up": up, "dc_lo": lo,
        }

    a, c = all_ind(b), all_ind(b2)
    for k in a:
        pdt.assert_series_equal(a[k].iloc[: t + 1], c[k].iloc[: t + 1], obj=k)
