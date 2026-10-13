"""Robot 3 (candle 4 jam): paritas dengan backtest, catch-up, live, kalender harian, Telegram tidak berisik."""
import copy

import numpy as np
import pandas as pd
import pytest

from src import live, notify, summary
from src.backtest import run_backtest
from src.data import load_config
from src.paper import build_sleeves, paper_settings, run_paper
from tests.helpers import make_bars


def world_4h(n=600, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC", name="time")
    bars = {}
    for i, s in enumerate(["AAA", "BBB"]):
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
        open_ = np.r_[100.0, close[:-1]]
        high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, n))
        low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, n))
        bars[s] = make_bars(list(zip(open_, high, low, close))).set_axis(idx)
    fund_idx = pd.date_range(idx[0], idx[-1], freq="8h", tz="UTC", name="time")
    funding = {s: pd.DataFrame({"funding_rate": 0.0001}, index=fund_idx) for s in bars}
    cfg = copy.deepcopy(load_config())
    cfg["paper_h4"].update(symbols=["AAA", "BBB"], start=str(idx[100]),
                           params={"entry_period": 20, "exit_period": 10, "atr_period": 14, "atr_mult": 3.0,
                                   "allow_short": True})
    return cfg, bars, funding


def trunc(d, k):
    return {s: v.iloc[:k] for s, v in d.items()}


def test_h4_paper_matches_backtest_with_catch_up(tmp_path):
    cfg, bars, funding = world_4h()
    db = tmp_path / "h4.db"
    for k in range(100, 601, 13):          # jalan tidak teratur: menguji catch-up beberapa candle sekaligus
        run_paper(cfg, trunc(bars, k), funding, db, section="paper_h4")
    run_paper(cfg, bars, funding, db, section="paper_h4")
    _, bt = paper_settings(cfg, "paper_h4")
    ref = run_backtest(bars, funding, build_sleeves(cfg, bars, "paper_h4"), bt, start=bars["AAA"].index[100], bar="4h")
    eq = summary.read_db(db)["equity"]
    assert len(eq) == 500
    np.testing.assert_allclose(eq.equity.to_numpy()[:-1], ref.equity.equity.to_numpy()[:-1], rtol=1e-12)
    tr = summary.read_db(db)["trades"]
    assert len(tr) == (ref.trades.exit_reason != "end").sum() >= 10


def test_h4_gap_detection_uses_4h_step(tmp_path):
    cfg, bars, funding = world_4h()
    db = tmp_path / "h4.db"
    run_paper(cfg, trunc(bars, 200), funding, db, section="paper_h4")
    gappy = {s: pd.concat([b.iloc[:200], b.iloc[202:220]]) for s, b in bars.items()}
    with pytest.raises(SystemExit, match="candle hilang"):
        run_paper(cfg, gappy, funding, db, section="paper_h4")


def test_h4_live_equals_official(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "ROOT", tmp_path.parent)
    cfg, bars, funding = world_4h()
    cfg["paper_h4"]["db"] = str(tmp_path / "h4.db")
    k = 333
    run_paper(cfg, trunc(bars, k), funding, tmp_path / "h4.db", section="paper_h4")
    t = bars["AAA"].index[k]
    assert live.next_candle(cfg, "paper_h4") == t
    candle = {s: tuple(bars[s].loc[t, ["open", "high", "low", "close"]].astype(float)) for s in bars}
    fund = {s: funding[s].loc[(funding[s].index >= t) & (funding[s].index < t + pd.Timedelta(hours=4)),
                              "funding_rate"].tolist() for s in bars}
    snap = live.live_snapshot(cfg, "paper_h4", candle, fund, t)
    run_paper(cfg, trunc(bars, k + 1), funding, tmp_path / "h4.db", section="paper_h4")
    assert snap["equity_now"] == pytest.approx(float(summary.read_db(tmp_path / "h4.db")["equity"].equity.iloc[-1]))
    # "hari ini" dihitung sejak akhir hari UTC kemarin, bukan sejak candle 4 jam sebelumnya
    eq = summary.read_db(tmp_path / "h4.db")["equity"]
    times = pd.to_datetime(eq.time, utc=True)
    prev_day = eq[times < t.normalize()].equity.iloc[-1]
    assert snap["equity_before"] == pytest.approx(float(prev_day))


def test_h4_calendar_is_daily(tmp_path):
    cfg, bars, funding = world_4h()
    db = tmp_path / "h4.db"
    run_paper(cfg, bars, funding, db, section="paper_h4")
    d = summary.daily_pnl(summary.read_db(db), 10_000)
    assert d.date.is_unique and len(d) == len({t.date() for t in bars["AAA"].index[100:]})
    assert d.pnl.sum() == pytest.approx(d.equity.iloc[-1] - 10_000)


def test_h4_status_uses_4h_steps():
    cfg, _, _ = world_4h()
    cfg = copy.deepcopy(load_config())
    assert summary.bar_delta(cfg, "paper_h4") == pd.Timedelta(hours=4)
    assert not summary.is_daily(cfg, "paper_h4") and summary.is_daily(cfg, "paper")


def test_telegram_quiet_for_4h_without_trades(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "call", lambda token, method, params=None: sent.append(params) or {})
    monkeypatch.setattr(notify, "robot_summary", lambda cfg, s: {"status": "ok"})
    env = {"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_CHAT_ID": "9"}
    cfg = load_config()
    # run 04:05 UTC: robot harian tidak memproses apa-apa, robot 4 jam memproses 1 candle tanpa transaksi
    notify.after_run(cfg, {"paper": 0, "paper_tsmom": 0, "paper_h4": 1}, [], env,
                     results={"paper_h4": {"fills": [], "trades": []}})
    assert sent == []
    # robot 4 jam bertransaksi: satu pesan singkat
    fill = {"time": pd.Timestamp("2026-10-13 12:00", tz="UTC"), "symbol": "BTC/USDT:USDT", "action": "entry",
            "side": "long", "reason": "signal", "fill_price": 83587.1}
    notify.after_run(cfg, {"paper": 0, "paper_tsmom": 0, "paper_h4": 1}, [], env,
                     results={"paper_h4": {"fills": [fill], "trades": []}})
    assert len(sent) == 1 and "Robot 3" in sent[0]["text"] and "Beli BTC" in sent[0]["text"]
