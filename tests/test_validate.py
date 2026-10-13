import numpy as np
import pandas as pd
import pytest

from src.data import load_config
from src.validate import monte_carlo_drawdown, sensitivity_variants, walk_forward_windows

CFG = load_config()


def test_walk_forward_windows_labels_and_partial():
    w = walk_forward_windows(CFG, pd.Timestamp("2020-01-01", tz="UTC"), pd.Timestamp("2026-10-11", tz="UTC"))
    starts = [s.strftime("%Y-%m-%d") for s, *_ in w]
    assert starts[0] == "2022-01-01" and starts[-1] == "2026-07-01"
    assert len(w) == 10
    assert [seg for _, _, seg, _ in w] == ["IS"] * 4 + ["OOS"] * 6
    assert w[0][1] == pd.Timestamp("2022-06-30", tz="UTC") and not w[0][3]
    assert w[-1][1] == pd.Timestamp("2026-10-11", tz="UTC") and w[-1][3]


def test_sensitivity_rsi_thresholds_are_symmetric():
    v = dict(sensitivity_variants(CFG, "s1"))
    assert v["rsi_entry=5"]["rsi_entry_long"] == 5 and v["rsi_entry=5"]["rsi_entry_short"] == 95
    assert v["rsi_exit=80"]["rsi_exit_short"] == 20
    assert v["default"] == CFG["s1"]


def test_monte_carlo_known_sequence():
    # tanpa pengacakan berarti: 2 kalah beruntun -1R pada risiko 10% -> DD 1 - 0,9² = 19%
    out = monte_carlo_drawdown(np.array([-1.0, -1.0]), 0.10, n=10, seed=0)
    assert out["actual_order"] == pytest.approx(0.19)
    assert out["median"] == pytest.approx(0.19)
