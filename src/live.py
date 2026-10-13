"""Panel live: perkiraan sementara posisi dan untung/rugi hari ini, memakai candle harian yang belum close.

Memuat state robot dari db (read-only), lalu menjalankan Engine yang sama pada candle hari berjalan
(open, high/low sejauh ini, harga terakhir). Hasilnya = apa yang akan dicatat robot besok pukul 07:05 WIB
kalau harga tidak bergerak lagi sampai candle close. Tidak pernah menulis ke db.
"""
from __future__ import annotations

import copy
import json

import pandas as pd

from src.backtest import Engine, Sleeve
from src.data import ROOT
from src.paper import paper_settings, strategy_params
from src.run import asset
from src.summary import bar_delta, bot_timeframe, read_db

DAY = pd.Timedelta(days=1)


def robot_sleeves(cfg: dict, section: str) -> list[Sleeve]:
    """Sleeve tanpa sinyal, dengan nama & urutan yang sama seperti robot (cocok dengan state di db)."""
    pc = cfg[section]
    hold = strategy_params(cfg, pc).get("max_hold_days")
    return [Sleeve(f"{pc['strategy']}:{asset(s)}", s, pd.DataFrame(), int(hold) if hold else None)
            for s in pc["symbols"]]


def next_candle(cfg: dict, section: str) -> pd.Timestamp | None:
    """Candle yang sedang berjalan untuk robot ini = satu hari setelah candle terakhir yang diproses."""
    meta = read_db(ROOT / cfg[section]["db"])["meta"]
    if meta.get("last_bar"):
        return pd.Timestamp(meta["last_bar"]) + bar_delta(cfg, section)
    if meta.get("state"):  # robot sudah mulai tapi belum memproses candle apa pun
        return pd.Timestamp(cfg[section]["start"], tz="UTC")
    return None


def live_snapshot(cfg: dict, section: str, candle: dict[str, tuple], funding: dict[str, list],
                  candle_time: pd.Timestamp) -> dict:
    """candle[symbol] = (open, high, low, last) candle_time; funding[symbol] = rate event hari itu sejauh ini."""
    pc, bt = paper_settings(cfg, section)
    db = read_db(ROOT / pc["db"])
    meta = db["meta"]
    if not meta.get("state"):
        return {"ok": False, "reason": "robot belum mulai"}
    missing = [s for s in pc["symbols"] if s not in candle]
    if missing:
        return {"ok": False, "reason": f"harga live tidak tersedia: {', '.join(asset(s) for s in missing)}"}

    eng = Engine.from_state(robot_sleeves(cfg, section), bt, json.loads(meta["state"]))
    eq = db["equity"]
    # "hari ini" = sejak akhir hari UTC kemarin (robot 4 jam: bukan sekadar sejak candle 4 jam terakhir)
    if len(eq):
        times = pd.to_datetime(eq["time"], utc=True)
        before = eq[times < candle_time.normalize()]
        equity_before = float(before["equity"].iloc[-1]) if len(before) else bt.initial_equity
    else:
        equity_before = bt.initial_equity
    planned = eng.planned_orders()
    live = copy.deepcopy(eng)
    live.process_bar(candle_time, prices=candle, funding=funding, signals=None)
    _, equity_now, _, notional, n_open = live.last_row
    last = {s: candle[s][3] for s in pc["symbols"]}
    positions = []
    for p in live.open_positions(last):
        d = 1 if p["side"] == "long" else -1
        positions.append({**p, "change": d * (p["last_close"] / p["entry_price"] - 1),
                          "to_stop": d * (p["last_close"] - p["stop"]) / p["last_close"],
                          "today_entry": pd.Timestamp(p["entry_time"]) == candle_time})
    return {
        "ok": True, "candle_time": candle_time, "equity_before": equity_before, "equity_now": equity_now,
        "today_pnl": equity_now - equity_before, "today_pct": equity_now / equity_before - 1,
        "total_change": equity_now / bt.initial_equity - 1, "positions": positions,
        "stops_today": [t for t in live.trades if t["exit_reason"] == "stop"],
        "closed_today": live.trades, "planned": planned, "notional": notional, "n_open": n_open,
        "rejected_today": [{**r, "stop_pct": r["stop_dist"] / candle[r["symbol"]][0]} for r in live.rejections],
    }


# ---------------------------------------------------------------- ambil data live (jaringan, publik)

def fetch_candles(ex, symbols: list[str], candle_time: pd.Timestamp, timeframe: str = "1d") -> dict[str, tuple]:
    """Candle `candle_time` (biasanya belum close) dari endpoint publik."""
    t_ms = int(candle_time.value // 1_000_000)
    out = {}
    for s in symbols:
        for ts, o, h, l, c, _v in ex.fetch_ohlcv(s, timeframe, since=t_ms, limit=2):
            if ts == t_ms:
                out[s] = (float(o), float(h), float(l), float(c))
    return out


def fetch_funding(ex, symbols: list[str], candle_time: pd.Timestamp, timeframe: str = "1d") -> dict[str, list]:
    t_ms = int(candle_time.value // 1_000_000)
    end = t_ms + int(pd.Timedelta(ex.parse_timeframe(timeframe), unit="s").total_seconds() * 1000)
    out = {}
    for s in symbols:
        rows = ex.fetch_funding_rate_history(s, since=t_ms, limit=10)
        out[s] = [float(r["fundingRate"]) for r in rows if t_ms <= r["timestamp"] < end]
    return out
