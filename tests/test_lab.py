import math

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from src import lab
from src.data import load_config
from src.strategies.registry import REGISTRY, signals_for
from tests.helpers import funding_every_8h, random_bars

CFG = load_config()


def _world(n=500, seed=4, freq="D"):
    syms = [f"S{i}" for i in range(8)]
    bars = {s: random_bars(n, seed=seed + i) for i, s in enumerate(syms)}
    if freq != "D":  # strategi intraday: indeks candle 1 jam
        idx = pd.date_range("2020-01-01", periods=n, freq=freq, tz="UTC", name="time")
        bars = {s: b.set_axis(idx) for s, b in bars.items()}
    rng = np.random.default_rng(seed)
    funding = {s: funding_every_8h(b.index, 0.0001).assign(
        funding_rate=lambda f: rng.normal(0.0001, 0.0003, len(f))) for s, b in bars.items()}
    return syms, bars, funding


@pytest.mark.parametrize("name", list(CFG["research"]["candidates"]))
def test_all_strategies_no_lookahead(name):
    """Mengubah harga DAN funding setelah t tidak mengubah sinyal sampai t (termasuk peringkat lintas koin)."""
    cand = CFG["research"]["candidates"][name]
    p = {**cand["params"], "allow_short": True}
    syms, bars, funding = _world(freq="h" if cand.get("timeframe", "1d") != "1d" else "D")
    t = 420
    cut = bars[syms[0]].index[t]
    bars2 = {s: b.copy() for s, b in bars.items()}
    fund2 = {s: f.copy() for s, f in funding.items()}
    rng = np.random.default_rng(1)
    for s in syms:
        bars2[s].iloc[t + 1:, :4] *= rng.uniform(0.5, 1.5, (len(bars2[s]) - t - 1, 1))
        later = fund2[s].index > cut + (bars[s].index[1] - bars[s].index[0])  # event setelah candle t close
        fund2[s].loc[later, "funding_rate"] = rng.normal(0.01, 0.01, later.sum())
    a = signals_for(cand["strategy"], p, bars, funding, syms, syms)
    b = signals_for(cand["strategy"], p, bars2, fund2, syms, syms)
    for s in syms:
        pdt.assert_frame_equal(a[s].iloc[: t + 1], b[s].iloc[: t + 1], obj=f"{name}:{s}")
    assert sum((a[s].entry != 0).sum() for s in syms) > 0


def test_xsmom_holds_top_k_and_trades_only_on_rebalance_day():
    syms, bars, funding = _world()
    p = {"lookback": 28, "top_k": 3, "rebalance_weekday": 6, "atr_period": 14, "atr_mult": 3.0, "allow_short": True}
    sig = signals_for("xsmom", p, bars, funding, syms, syms)
    entries = pd.DataFrame({s: sig[s].entry for s in syms})
    active = entries[(entries != 0).any(axis=1)]
    assert (active.index.dayofweek == 6).all()
    assert ((active == 1).sum(axis=1) == 3).all() and ((active == -1).sum(axis=1) == 3).all()


def test_funding_contra_reacts_to_extremes():
    syms, bars, funding = _world(n=600)
    f = funding[syms[0]].copy()
    spike = (f.index > bars[syms[0]].index[500]) & (f.index < bars[syms[0]].index[510])
    f.loc[spike, "funding_rate"] = 0.01  # funding ekstrem tinggi -> kerumunan long -> short
    p = {"avg_days": 7, "window": 365, "min_window": 180, "tail": 0.1, "max_hold_days": 14,
         "atr_period": 14, "atr_mult": 3.0, "allow_short": True}
    sig = signals_for("funding_contra", p, bars, {**funding, syms[0]: f}, [syms[0]])[syms[0]]
    assert (sig.entry.iloc[505:512] == -1).any()


def test_candidate_hash_independent_of_other_candidates():
    import copy
    cfg2 = copy.deepcopy(CFG)
    cfg2["research"]["candidates"]["zz_baru"] = {"strategy": "tsmom", "params": {"lookback": 10, "atr_period": 14,
                                                                                 "atr_mult": 3.0}, "sensitivity": {}}
    for k in lab.expand_candidates(CFG):
        assert lab.candidate_hash(CFG, k) == lab.candidate_hash(cfg2, k)


def test_long_only_mode_never_shorts():
    for name, cand in lab.expand_candidates(CFG).items():
        if cand["mode"] == "long_only":
            syms, bars, funding = _world(seed=9)
            sig = signals_for(cand["strategy"], cand["params"], bars, funding, syms, syms)
            assert all((sig[s].entry >= 0).all() for s in syms), name


def test_expand_and_variants():
    c = lab.expand_candidates(CFG)
    assert len(c) == 2 * len(CFG["research"]["candidates"])
    v = dict(lab.variants(c["t3_tsmom_ls"]))
    assert v["lookback=45"]["lookback"] == 45 and v["lookback=45"]["allow_short"] is True
    assert v["default"]["lookback"] == 60


def test_rolling_windows_skip_warmup_year():
    w = lab.rolling_windows("2020-01-01", "2023-12-31", 6)
    assert w[0][0] == pd.Timestamp("2021-01-01", tz="UTC") and len(w) == 6
    assert w[-1][1] == pd.Timestamp("2023-12-31", tz="UTC")


def test_deflated_sharpe_behaviour():
    one = lab.deflated_sharpe(0.05, 1, 0.0, 1000, 0.0, 3.0)
    assert one == pytest.approx(NormalCDF(0.05 * math.sqrt(999) / math.sqrt(1 + 0.5 * 0.05 ** 2)), rel=1e-9)
    many = lab.deflated_sharpe(0.05, 100, 0.02 ** 2, 1000, 0.0, 3.0)
    assert many < one          # makin banyak percobaan, makin kecil keyakinan
    assert lab.deflated_sharpe(-0.02, 1, 0.0, 1000, 0.0, 3.0) < 0.5


def NormalCDF(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def test_append_log_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(lab, "LAB", tmp_path)
    monkeypatch.setattr(lab, "LOG", tmp_path / "log.csv")
    row = {"candidate_hash": "h", "candidate": "c", "variant": "default", "period": "selection", "x": 1}
    assert lab.append_log([row]) == 1
    assert lab.append_log([row]) == 0
    assert lab.append_log([{**row, "variant": "v2"}]) == 1
    assert len(pd.read_csv(tmp_path / "log.csv")) == 2


def test_trend_ensemble_model_state_machine():
    from src.strategies.trend_ensemble import _model_state
    # 6 bar datar di 100, tembus ke atas (long), bertahan, lalu jatuh di bawah channel keluar (flat).
    close = [100] * 6 + [110, 111, 112, 105, 90, 90]
    h = pd.Series([c + 0.5 for c in close], dtype=float)
    l = pd.Series([c - 0.5 for c in close], dtype=float)
    c = pd.Series(close, dtype=float)
    st = _model_state(h, l, c, 4, allow_short=False)
    assert list(st[:6]) == [0] * 6
    assert st[6] == 1 and st[7] == 1 and st[8] == 1
    assert st[10] == 0                      # 90 < low 2 bar sebelumnya -> keluar
    st_short = _model_state(h, l, c, 4, allow_short=True)
    assert st_short[10] == -1               # versi long-short: tembus bawah channel 4 bar -> short


def test_trend_ensemble_entry_requires_majority():
    syms, bars, funding = _world()
    p = {"base_lookback": 20, "n_models": 4, "entry_score": 0.5, "atr_period": 14, "atr_mult": 3.0,
         "allow_short": True}
    sig = signals_for("trend_ensemble", p, bars, funding, syms[:1])[syms[0]]
    assert set(np.unique(sig.entry)) <= {-1, 0, 1}
    assert (sig.entry.iloc[:20] == 0).all()   # semua channel belum panas
