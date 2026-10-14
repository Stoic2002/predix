"""Radar sinyal dan evaluasi paper vs backtest, untuk Beranda dashboard.

Radar: seberapa jauh harga dari pemicu berikutnya (dicek saat candle tutup), dihitung dengan rumus yang sama
dengan fungsi strategi (diuji di tests/test_evaluation.py).
Evaluasi: aturan di bagian `evaluation` config.yaml (ditetapkan 2026-10-14 sebelum ada hasil paper yang berarti).
Hanya membaca; tidak pernah mengubah robot atau database-nya.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from src import metrics
from src.backtest import run_backtest
from src.data import ROOT, load_bars, load_funding
from src.paper import PANDAS_FREQ, build_sleeves, paper_settings
from src.summary import angka, bar_delta, bot_timeframe, koin, persen, read_db



# ---------------------------------------------------------------- radar

def triggers(strategy: str, params: dict, bars: pd.DataFrame) -> dict:
    """Level harga untuk keputusan di close candle BERIKUTNYA, dari candle yang sudah close."""
    h, l, c = bars["high"], bars["low"], bars["close"]
    if strategy == "donchian":
        n, m = int(params["entry_period"]), int(params["exit_period"])
        return {"long_entry": float(h.iloc[-n:].max()),
                "short_entry": float(l.iloc[-n:].min()) if params.get("allow_short") else None,
                "long_exit": float(l.iloc[-m:].min()), "short_exit": float(h.iloc[-m:].max())}
    if strategy == "tsmom":
        ref = float(c.iloc[-int(params["lookback"])])
        return {"long_entry": ref, "short_entry": ref if params.get("allow_short") else None,
                "long_exit": ref, "short_exit": ref}
    raise ValueError(f"radar belum mendukung strategi {strategy}")


def rule_text(strategy: str, params: dict) -> str:
    if strategy == "donchian":
        return (f"masuk saat close menembus tertinggi/terendah {params['entry_period']} candle terakhir, "
                f"keluar saat close menembus terendah/tertinggi {params['exit_period']} candle")
    return (f"pegang selama close di atas close {params['lookback']} candle sebelumnya "
            "(level bergeser tiap candle)")


def radar_strategy(cfg: dict, section: str) -> tuple[str, dict]:
    pc = cfg[section]
    if "params" in pc:
        return pc["strategy"], pc["params"]
    if pc["strategy"] == "s2":  # S2 Fase 1 = Donchian breakout
        return "donchian", cfg["s2"]
    raise ValueError(f"radar belum mendukung strategi {pc['strategy']}")


def _why(reason: str) -> str:
    if "likuidasi" in reason:
        return "stop loss terlalu dekat dengan harga likuidasi karena pasar sangat bergejolak (aturan keamanan)"
    return reason


def _row(sym, status, action, level, price, stop=None, note=""):
    return {"koin": koin(sym), "status": status, "aksi berikutnya": action, "level": level,
            "jarak": (level / price - 1) if level else None, "stop loss": stop, "harga": price, "catatan": note}


def radar(cfg: dict, section: str, prices: dict[str, float] | None = None) -> pd.DataFrame:
    """Satu baris per pemicu: jarak harga (live kalau ada, kalau tidak close terakhir) ke level pemicu.

    Pemicu sinyal dicek pada close candle berikutnya; stop loss dicek sepanjang candle (high/low)."""
    pc = cfg[section]
    strat, params = radar_strategy(cfg, section)
    tf = bot_timeframe(cfg, section)
    db = read_db(ROOT / pc["db"])
    meta = db["meta"]
    rej = db["rejections"]
    last_rej = {}
    if len(rej) and meta.get("last_bar"):  # hanya penolakan di candle terakhir yang diproses
        recent = rej[pd.to_datetime(rej["time"], utc=True) >= pd.Timestamp(meta["last_bar"])]
        last_rej = {r["symbol"]: r["reason"] for _, r in recent.iterrows()}
    held = {p["symbol"]: p for p in json.loads(meta.get("positions", "[]"))}
    planned = {o["symbol"]: o for o in json.loads(meta.get("planned", "[]"))}
    rows = []
    for sym in pc["symbols"]:
        bars = load_bars(sym, timeframe=tf)
        if meta.get("last_bar"):  # candle yang sama dengan yang sudah diproses robot
            bars = bars[bars.index <= pd.Timestamp(meta["last_bar"])]
        t = triggers(strat, params, bars)
        price = (prices or {}).get(sym) or float(bars["close"].iloc[-1])
        note = ""
        if sym in planned:
            o = planned[sym]
            act = ("tutup posisi" if o["action"] == "exit"
                   else "beli" if o.get("side") == "long" else "short")
            note = f"akan {act} saat candle berikutnya dibuka"
        p = held.get(sym)
        if p and p["side"] == "long":
            rows.append(_row(sym, "dipegang (long)", "jual kalau close di bawah", t["long_exit"], price,
                             float(p["stop"]), note))
        elif p:
            rows.append(_row(sym, "dipegang (short)", "tutup kalau close di atas", t["short_exit"], price,
                             float(p["stop"]), note))
        else:
            if sym in last_rej:
                note = "; ".join(x for x in (note, "entry terakhir ditolak: " + _why(last_rej[sym])) if x)
            elif not note and price > t["long_entry"]:
                note = "harga sudah di atas level; dicek lagi saat candle tutup"
            rows.append(_row(sym, "menunggu", "beli kalau close di atas", t["long_entry"], price, None, note))
            if t["short_entry"]:
                rows.append(_row(sym, "menunggu", "short kalau close di bawah", t["short_entry"], price, None, note))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- pembanding backtest

def _daily(eq: pd.Series) -> pd.Series:
    return eq.resample("1D").last().dropna()


def reference(cfg: dict, section: str) -> dict:
    """Backtest robot yang sama sampai sehari sebelum robot mulai (tidak memakai data periode paper)."""
    pc, bt = paper_settings(cfg, section)
    ev = cfg["evaluation"]
    tf = bot_timeframe(cfg, section)
    bars = {s: load_bars(s, timeframe=tf) for s in pc["symbols"]}
    fund = {s: load_funding(s) for s in pc["symbols"]}
    start = pd.Timestamp(pc["start"], tz="UTC")
    end = start - bar_delta(cfg, section)
    sleeves = build_sleeves(cfg, bars, section)
    full = run_backtest(bars, fund, sleeves, bt, start="2020-01-01", end=end, bar=PANDAS_FREQ[tf])
    recent = run_backtest(bars, fund, sleeves, bt, start=ev["reference_start"], end=end, bar=PANDAS_FREQ[tf])
    eq_full, eq_recent = _daily(full.equity["equity"]), _daily(recent.equity["equity"])
    m = metrics.summarize(recent)
    days = max((eq_recent.index[-1] - eq_recent.index[0]).days, 1)
    flat = full.equity["n_open"] == 0
    runs = flat.groupby((flat != flat.shift()).cumsum()).agg(["first", "size"])
    per_day = pd.Timedelta(days=1) / bar_delta(cfg, section)
    entries = pd.to_datetime(full.trades["entry_time"]).sort_values() if len(full.trades) else pd.Series(dtype="datetime64[ns, UTC]")
    gaps = entries.diff().dt.total_seconds().div(86400).dropna()
    return {"equity": eq_recent, "max_drawdown": m["max_drawdown"], "profit_factor": m.get("profit_factor", math.nan),
            "trades_per_month": m["n_trades"] / days * 30.4, "n_trades": m["n_trades"],
            "median_gap_days": float(gaps.median()) if len(gaps) else math.nan,
            "longest_idle_days": float(runs[runs["first"]]["size"].max() / per_day) if flat.any() else 0.0,
            "flat_share": float(flat.mean())}


def return_band(eq: pd.Series, days: int, pct: tuple[float, float]) -> tuple[float, float, float]:
    """Persentil (rendah, median, tinggi) return semua jendela `days` hari di kurva ekuitas harian."""
    days = max(int(days), 1)
    if len(eq) <= days:
        return math.nan, math.nan, math.nan
    r = (eq.shift(-days) / eq - 1).dropna()
    return float(np.percentile(r, pct[0])), float(r.median()), float(np.percentile(r, pct[1]))


# ---------------------------------------------------------------- evaluasi

def paper_stats(cfg: dict, section: str) -> dict:
    pc = cfg[section]
    db = read_db(ROOT / pc["db"])
    eq = db["equity"]
    initial = float(pc["initial_equity"])
    start = pd.Timestamp(pc["start"], tz="UTC")
    if eq.empty:
        return {"days": 0, "return": 0.0, "max_drawdown": 0.0, "n_trades": 0, "profit_factor": math.nan,
                "entries": 0, "kill_switch": False, "last_fill": None}
    eq = eq.assign(time=pd.to_datetime(eq["time"], utc=True)).set_index("time")["equity"]
    curve = pd.concat([pd.Series([initial], index=[start - pd.Timedelta(seconds=1)]), eq])
    last_close = eq.index[-1] + bar_delta(cfg, section)
    tr, fills = db["trades"], db["fills"]
    pnl = tr["net_pnl"] if len(tr) else pd.Series(dtype=float)
    state = json.loads(db["meta"]["state"]) if db["meta"].get("state") else {}
    return {"days": (last_close - start).total_seconds() / 86400, "return": float(eq.iloc[-1] / initial - 1),
            "max_drawdown": max(0.0, float(-metrics.drawdown(curve).min())), "n_trades": len(tr),
            "profit_factor": float(pnl[pnl > 0].sum() / -pnl[pnl < 0].sum()) if (pnl < 0).any() else math.nan,
            "entries": int((fills["action"] == "entry").sum()) if len(fills) else 0,
            "kill_switch": bool(state.get("entries_blocked")),
            "last_fill": pd.Timestamp(fills["time"].iloc[-1]) if len(fills) else None}


def _dd(x: float) -> str:
    return angka(x * 100, 1) + "%"


ICON = {"ok": "🟢", "warn": "🟡", "fail": "🔴", "wait": "⏳"}


def evaluate(cfg: dict, section: str, ref: dict) -> dict:
    ev = cfg["evaluation"]
    ps = paper_stats(cfg, section)
    days = ps["days"]
    checks = []

    lo, mid, hi = return_band(ref["equity"], round(days), tuple(ev["return_band_pct"]))
    if days < 1 or math.isnan(lo):
        checks.append(("Return vs kisaran normal", "-", "-", "wait", "Belum ada hari yang lengkap."))
    else:
        st = "fail" if ps["return"] < lo else "ok"
        checks.append(("Return vs kisaran normal", persen(ps["return"]),
                       f"{persen(lo)} s/d {persen(hi)} (median {persen(mid)}) untuk {round(days)} hari", st,
                       "Lebih buruk dari 95% jendela backtest dengan panjang sama." if st == "fail"
                       else "Masih dalam kisaran yang biasa terjadi di backtest."))

    ddr = ps["max_drawdown"] / ref["max_drawdown"] if ref["max_drawdown"] > 0 else 0.0
    st = "fail" if ddr > ev["drawdown_fail_x"] else "warn" if ddr > ev["drawdown_warn_x"] else "ok"
    checks.append(("Turun terdalam (drawdown)", _dd(ps["max_drawdown"]),
                   f"backtest {_dd(ref['max_drawdown'])}; waspada > {angka(ev['drawdown_warn_x'], 1)}×, gagal > {angka(ev['drawdown_fail_x'], 1)}×",
                   st, f"{angka(ddr, 1)}× drawdown backtest."))

    if ps["n_trades"] < ev["min_trades_for_pf"]:
        checks.append(("Profit factor", f"{ps['n_trades']} trade selesai", f"dinilai setelah {ev['min_trades_for_pf']} trade",
                       "wait", f"Backtest: PF {angka(ref['profit_factor'])}."))
    else:
        pf = ps["profit_factor"]
        st = "ok" if (math.isnan(pf) or pf >= ev["pf_warn"]) else "warn" if pf >= ev["pf_fail"] else "fail"
        checks.append(("Profit factor", "∞" if math.isnan(pf) else angka(pf),
                       f"≥ {angka(ev['pf_warn'], 1)} hijau, < {angka(ev['pf_fail'], 1)} merah (backtest {angka(ref['profit_factor'])})", st, ""))

    if days < ev["min_days_for_frequency"]:
        checks.append(("Frekuensi trade", f"{ps['entries']} entry dalam {days:.0f} hari",
                       f"dinilai setelah {ev['min_days_for_frequency']} hari", "wait",
                       f"Backtest: {angka(ref['trades_per_month'], 1)} trade/bulan."))
    else:
        rate = ps["entries"] / days * 30.4
        lo_x, hi_x = ev["frequency_band_x"]
        ok = lo_x * ref["trades_per_month"] <= rate <= hi_x * ref["trades_per_month"]
        checks.append(("Frekuensi trade", f"{angka(rate, 1)}/bulan",
                       f"{angka(lo_x, 1)}× s/d {angka(hi_x, 1)}× backtest ({angka(ref['trades_per_month'], 1)}/bulan)", "ok" if ok else "warn",
                       "" if ok else "Robot trading jauh lebih sering/jarang dari backtest; cek data & aturan."))

    checks.append(("Kill switch", "AKTIF" if ps["kill_switch"] else "tidak aktif", "harus tidak aktif",
                   "fail" if ps["kill_switch"] else "ok", ""))

    worst = "fail" if any(c[3] == "fail" for c in checks) else "warn" if any(c[3] == "warn" for c in checks) else "ok"
    if days >= ev["verdict_after_days"]:
        pf = ps["profit_factor"]
        passed = worst != "fail" and (not math.isnan(pf) and pf >= 1.0)
        verdict = ("LOLOS" if passed else "GAGAL", "ok" if passed else "fail")
    else:
        verdict = (f"Berjalan: hari {days:.0f} dari {ev['verdict_after_days']}", worst)
    idle_since = ps["last_fill"] or pd.Timestamp(cfg[section]["start"], tz="UTC")
    idle_days = (pd.Timestamp.now(tz="UTC") - idle_since).total_seconds() / 86400
    return {"checks": [{"aspek": a, "nilai": v, "patokan": p, "status": s, "keterangan": k} for a, v, p, s, k in checks],
            "verdict": verdict, "days": days, "idle_days": idle_days, "has_fill": ps["last_fill"] is not None,
            "ref": {k: v for k, v in ref.items() if k != "equity"}}
