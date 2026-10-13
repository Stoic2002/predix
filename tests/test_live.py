"""Panel live = robot: menjalankan candle hari berjalan lewat Engine yang sama harus menghasilkan angka
yang identik dengan yang dicatat robot setelah candle itu close. Panel tidak boleh menulis ke db."""
import copy
import hashlib

import pytest

from src import live
from src.paper import run_paper
from src.summary import read_db
from tests.test_paper import START, funding_every_8h, make_world, truncated


def _cfg_with_db(tmp_path, section="paper"):
    cfg, bars, funding = make_world()
    cfg = copy.deepcopy(cfg)
    cfg[section]["db"] = str(tmp_path / f"{section}.db")
    return cfg, bars, funding


@pytest.mark.parametrize("k", [START + 40, START + 120, START + 250])
def test_live_equals_official_after_close(tmp_path, monkeypatch, k):
    monkeypatch.setattr(live, "ROOT", tmp_path.parent)
    cfg, bars, funding = _cfg_with_db(tmp_path)
    db = tmp_path / "paper.db"
    run_paper(cfg, truncated(bars, k), funding, db)          # robot sudah memproses sampai bar k-1

    t = bars["AAA"].index[k]
    assert live.next_candle(cfg, "paper") == t
    candle = {s: tuple(bars[s].loc[t, ["open", "high", "low", "close"]].astype(float)) for s in bars}
    fund = {s: funding[s].loc[(funding[s].index >= t) & (funding[s].index < t + live.DAY), "funding_rate"].tolist()
            for s in bars}
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    snap = live.live_snapshot(cfg, "paper", candle, fund, t)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before   # panel tidak menulis ke db
    assert snap["ok"]

    run_paper(cfg, truncated(bars, k + 1), funding, db)      # robot resmi memproses bar k
    official = read_db(db)
    assert snap["equity_now"] == pytest.approx(float(official["equity"].equity.iloc[-1]), rel=1e-12)
    assert len(snap["positions"]) == int(official["equity"].n_open.iloc[-1])


def test_live_reports_missing_prices(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "ROOT", tmp_path.parent)
    cfg, bars, funding = _cfg_with_db(tmp_path)
    run_paper(cfg, truncated(bars, START + 30), funding, tmp_path / "paper.db")
    t = live.next_candle(cfg, "paper")
    snap = live.live_snapshot(cfg, "paper", {"AAA": (1.0, 1.0, 1.0, 1.0)}, {}, t)
    assert not snap["ok"] and "BBB" in snap["reason"]


def test_live_before_robot_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "ROOT", tmp_path.parent)
    cfg, _, _ = _cfg_with_db(tmp_path)
    assert live.next_candle(cfg, "paper") is None


def test_live_lists_rejected_entries(tmp_path, monkeypatch):
    """Entry yang ditolak aturan likuidasi muncul di snapshot dengan jarak stop dalam persen."""
    monkeypatch.setattr(live, "ROOT", tmp_path.parent)
    cfg, bars, funding = _cfg_with_db(tmp_path)
    cfg["risk"]["liq_buffer"] = 100.0  # paksa semua entry ditolak
    for k in range(START + 30, START + 200):
        run_paper(cfg, truncated(bars, k), funding, tmp_path / "paper.db")
        t = bars["AAA"].index[k]
        candle = {s: tuple(bars[s].loc[t, ["open", "high", "low", "close"]].astype(float)) for s in bars}
        snap = live.live_snapshot(cfg, "paper", candle, {}, t)
        if snap["rejected_today"]:
            r = snap["rejected_today"][0]
            assert "likuidasi" in r["reason"] and 0 < r["stop_pct"] < 1
            return
    pytest.fail("tidak ada entry yang ditolak; test tidak menguji apa-apa")
