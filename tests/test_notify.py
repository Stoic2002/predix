"""Laporan Telegram: tanpa jaringan (API dipalsukan), token tidak pernah bocor."""
import copy

import pandas as pd
import pytest

from src import notify, summary
from src.data import load_config


def test_should_send_rules():
    assert notify.should_send({"a": 1, "b": 0}, [], ["ok", "ok"])
    assert not notify.should_send({"a": 0, "b": 0}, [], ["ok", "ok"])   # run cadangan: diam
    assert notify.should_send({"a": 0}, ["b"], ["ok"])                 # ada yang gagal
    assert notify.should_send({"a": 0}, [], ["late"])                  # terlambat


def test_split_message_respects_limit():
    text = "\n".join(f"baris {i} " + "x" * 50 for i in range(300))
    parts = notify.split_message(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts) and "".join(parts) == text


def test_env_roundtrip_is_private(tmp_path):
    env = tmp_path / ".env"
    notify.save_env({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"}, env)
    assert notify.load_env(env) == {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"}
    assert oct(env.stat().st_mode)[-3:] == "600"


def test_after_run_sends_once_and_never_raises(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(notify, "call", lambda token, method, params=None: sent.append(params) or {})
    cfg = load_config()
    env = {"TELEGRAM_BOT_TOKEN": "123:rahasia", "TELEGRAM_CHAT_ID": "42"}
    notify.after_run(cfg, {"paper": 1}, [], env)
    assert len(sent) == 1 and sent[0]["chat_id"] == "42" and "Robot 1" in sent[0]["text"]
    assert "rahasia" not in sent[0]["text"]

    def boom(*a, **k):
        raise notify.TelegramError("tidak bisa menghubungi Telegram: URLError")
    monkeypatch.setattr(notify, "call", boom)
    notify.after_run(cfg, {"paper": 1}, [], env)  # tidak boleh melempar error
    assert "gagal kirim" in capsys.readouterr().out


def test_unconfigured_is_silent(monkeypatch):
    monkeypatch.setattr(notify, "call", lambda *a, **k: pytest.fail("tidak boleh memanggil API"))
    notify.after_run(load_config(), {"paper": 1}, [], env={})


def test_summary_wording_on_demo(tmp_path):
    cfg = copy.deepcopy(load_config())
    cfg["paper"].update(db="state/paper_demo.db", start="2026-01-01")
    s = summary.robot_summary(cfg, "paper", now=pd.Timestamp("2026-10-12 01:00", tz="UTC"))
    if s["status"] == "error":
        pytest.skip("db demo tidak ada di mesin ini")
    assert s["status"] == "ok" and s["name"] == "Robot 1 · Breakout"
    assert all(p.split(":")[0] in ("BTC", "ETH") for p in s["positions"])


def test_number_format_indonesian():
    assert summary.angka(10078.6) == "10.078,60"
    assert summary.persen(0.0079) == "+0,8%" and summary.persen(-0.033) == "-3,3%"
    assert summary.harga(0.25668) == "0,2567"


def test_daily_pnl_reconciles_with_equity(tmp_path):
    """Jumlah untung/rugi harian = perubahan modal total; trade ditutup muncul di harinya."""
    from src.paper import run_paper
    from tests.test_paper import make_world
    cfg, bars, funding = make_world()
    db = tmp_path / "p.db"
    run_paper(cfg, bars, funding, db)
    d = summary.daily_pnl(summary.read_db(db), 10_000)
    assert d.pnl.sum() == pytest.approx(d.equity.iloc[-1] - 10_000)
    assert (d.trades != "").sum() > 0
    assert d.date.is_monotonic_increasing and d.date.is_unique


def test_combined_daily_pnl_sums_robots(tmp_path, monkeypatch):
    from src.paper import run_paper
    from tests.test_paper import make_world
    cfg, bars, funding = make_world()
    cfg["paper"]["db"] = str(tmp_path / "a.db")
    cfg["paper_tsmom"].update(db=str(tmp_path / "b.db"), symbols=["AAA", "BBB"], start=cfg["paper"]["start"],
                              params={"lookback": 20, "atr_period": 14, "atr_mult": 3.0, "allow_short": False})
    monkeypatch.setattr(summary, "ROOT", tmp_path.parent)  # path db sudah absolut
    run_paper(cfg, bars, funding, tmp_path / "a.db")
    run_paper(cfg, bars, funding, tmp_path / "b.db", section="paper_tsmom")
    a = summary.daily_pnl(summary.read_db(tmp_path / "a.db"), 10_000)
    b = summary.daily_pnl(summary.read_db(tmp_path / "b.db"), 10_000)
    cfg["paper_h4"]["db"] = str(tmp_path / "c.db")  # robot 3 belum punya db di test ini
    g, initial = summary.combined_daily_pnl(cfg)
    assert initial == sum(float(cfg[s]["initial_equity"]) for s in summary.robot_sections(cfg))
    assert g.pnl.sum() == pytest.approx(a.pnl.sum() + b.pnl.sum())
