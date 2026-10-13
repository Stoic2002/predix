"""Bot paper: paritas dengan backtest, catch-up, idempoten, kunci config, kill switch."""
import copy
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from src.backtest import BacktestConfig, run_backtest
from src.data import load_config
from src.paper import build_sleeves, paper_settings, reset_kill_switch, run_paper
from tests.helpers import funding_every_8h, random_bars

N, START = 420, 120


def make_world(seed=21):
    bars = {"AAA": random_bars(N, seed=seed), "BBB": random_bars(N, seed=seed + 1)}
    funding = {s: funding_every_8h(b.index, 0.0001) for s, b in bars.items()}
    cfg = copy.deepcopy(load_config())
    cfg["paper"].update(symbols=["AAA", "BBB"], start=str(bars["AAA"].index[START].date()))
    # channel pendek hanya supaya data acak menghasilkan banyak trade; yang diuji engine/bot, bukan S2
    cfg["s2"].update(entry_period=10, exit_period=5)
    return cfg, bars, funding


def truncated(d: dict, k: int) -> dict:
    return {s: v.iloc[:k] for s, v in d.items()}


def read(db, table):
    with sqlite3.connect(db) as c:
        return pd.read_sql(f"SELECT * FROM {table}", c)


def test_day_by_day_matches_backtest(tmp_path):
    """Paper yang dijalankan tiap hari (data bertambah satu candle) = backtest sekali jalan."""
    cfg, bars, funding = make_world()
    db = tmp_path / "p.db"
    for k in range(START, N + 1):
        run_paper(cfg, truncated(bars, k), funding, db)

    _, bt = paper_settings(cfg)
    ref = run_backtest(bars, funding, build_sleeves(cfg, bars), bt, start=bars["AAA"].index[START])
    eq = read(db, "equity")
    assert len(eq) == N - START
    # bar terakhir backtest menutup semua posisi ("end"), paper tidak: bandingkan sebelum itu
    np.testing.assert_allclose(eq.equity.to_numpy()[:-1], ref.equity.equity.to_numpy()[:-1], rtol=1e-12)
    tr = read(db, "trades")
    ref_tr = ref.trades[ref.trades.exit_reason != "end"]
    assert len(tr) == len(ref_tr) and len(tr) >= 15
    np.testing.assert_allclose(tr.net_pnl, ref_tr.net_pnl, rtol=1e-12)
    assert list(tr.exit_reason) == list(ref_tr.exit_reason)


def test_catch_up_equals_daily_and_rerun_is_idempotent(tmp_path):
    cfg, bars, funding = make_world()
    daily, catchup = tmp_path / "d.db", tmp_path / "c.db"
    for k in range(START, 300):
        run_paper(cfg, truncated(bars, k), funding, daily)
    run_paper(cfg, truncated(bars, 200), funding, catchup)
    r = run_paper(cfg, truncated(bars, 299), funding, catchup)   # laptop mati 99 hari
    assert r["processed"] == 99
    pd.testing.assert_frame_equal(read(daily, "equity"), read(catchup, "equity"))
    pd.testing.assert_frame_equal(read(daily, "trades"), read(catchup, "trades"))

    again = run_paper(cfg, truncated(bars, 299), funding, catchup)
    assert again["processed"] == 0
    assert len(read(catchup, "equity")) == len(read(daily, "equity"))


def test_before_first_candle_closes_nothing_happens(tmp_path):
    cfg, bars, funding = make_world()
    r = run_paper(cfg, truncated(bars, START), funding, tmp_path / "p.db")
    assert r["processed"] == 0 and r["last_bar"] is None
    r = run_paper(cfg, truncated(bars, START + 1), funding, tmp_path / "p.db")
    assert r["processed"] == 1


def test_refuses_when_settings_change(tmp_path):
    cfg, bars, funding = make_world()
    db = tmp_path / "p.db"
    run_paper(cfg, truncated(bars, 200), funding, db)
    changed = copy.deepcopy(cfg)
    changed["s2"]["entry_period"] = 40
    with pytest.raises(SystemExit, match="berubah"):
        run_paper(changed, truncated(bars, 210), funding, db)


def test_refuses_on_gap_in_data(tmp_path):
    cfg, bars, funding = make_world()
    db = tmp_path / "p.db"
    run_paper(cfg, truncated(bars, 200), funding, db)
    gappy = {s: pd.concat([b.iloc[:200], b.iloc[205:220]]) for s, b in bars.items()}
    with pytest.raises(SystemExit, match="candle hilang"):
        run_paper(cfg, gappy, funding, db)


def test_kill_switch_blocks_entries_until_reset(tmp_path):
    cfg, bars, funding = make_world()
    cfg["paper"]["kill_switch"] = {"warn_drawdown": 0.0005, "halt_drawdown": 0.001}
    db = tmp_path / "p.db"
    r = run_paper(cfg, bars, funding, db)
    assert r["entries_blocked"]
    rej = read(db, "rejections")
    assert (rej.reason == "kill switch aktif").any()
    first_block = read(db, "runs").note.str.contains("KILL SWITCH").any()
    assert first_block

    reset_kill_switch(db)
    with sqlite3.connect(db) as c:
        state = json.loads(dict(c.execute("SELECT key, value FROM meta").fetchall())["state"])
    assert state["entries_blocked"] is False


def test_engine_state_roundtrip_is_lossless():
    from src.backtest import Engine
    cfg, bars, funding = make_world()
    sleeves = build_sleeves(cfg, bars)
    eng = Engine(sleeves, BacktestConfig())
    eng.pos[sleeves[0].name] = None
    eng.pending_entry[sleeves[1].name] = (1, 12.5, pd.Timestamp("2024-01-01", tz="UTC"))
    back = Engine.from_state(sleeves, BacktestConfig(), json.loads(json.dumps(eng.to_state())))
    assert back.to_state() == eng.to_state()


def test_params_bot_section_matches_backtest(tmp_path):
    """Bot dengan parameter di config-nya sendiri (paper_tsmom) juga identik dengan backtest."""
    cfg, bars, funding = make_world(seed=33)
    cfg["paper_tsmom"].update(symbols=["AAA", "BBB"], start=str(bars["AAA"].index[START].date()),
                              params={"lookback": 20, "atr_period": 14, "atr_mult": 3.0, "allow_short": False})
    db = tmp_path / "t.db"
    for k in range(START, N + 1, 7):  # jalan tiap minggu: sekaligus menguji catch-up
        run_paper(cfg, truncated(bars, k), funding, db, section="paper_tsmom")
    run_paper(cfg, bars, funding, db, section="paper_tsmom")

    _, bt = paper_settings(cfg, "paper_tsmom")
    assert bt.risk_per_trade == 0.01
    ref = run_backtest(bars, funding, build_sleeves(cfg, bars, "paper_tsmom"), bt, start=bars["AAA"].index[START])
    eq = read(db, "equity")
    np.testing.assert_allclose(eq.equity.to_numpy()[:-1], ref.equity.equity.to_numpy()[:-1], rtol=1e-12)
    tr = read(db, "trades")
    ref_tr = ref.trades[ref.trades.exit_reason != "end"]
    assert len(tr) == len(ref_tr) >= 10
    assert (tr.side == "long").all()
    assert tr.sleeve.str.startswith("tsmom:").all()
