"""Uji emas & perak: aturan robot yang sedang berjalan di futures emas (GC=F) & perak (SI=F), 2000–sekarang.

    python -m src.metals

Ditetapkan 2026-10-13 SEBELUM hasilnya dilihat:
  - Data: Yahoo Finance (yfinance), kontrak futures front-month berkelanjutan, harian, sejak Agu 2000.
    Catatan: tidak di-adjust untuk roll kontrak (lompatan kecil saat ganti kontrak); hanya hari bursa buka.
  - Strategi: aturan Robot 1 (Donchian 55/20 long-short) dan Robot 2 (TSMOM 60 beli saja), TIDAK diubah.
    Risiko 1% per trade dibagi rata ke 2 logam (0,5% per logam). Biaya & stop sama dengan robot.
  - Funding: rata-rata nyata perpetual Binance (XAU 0,0354%/hari, XAG 0,0566%/hari; long bayar, short terima),
    dihitung per hari KALENDER: funding Sabtu–Minggu dibebankan ke bar Senin.
  - Lolos kalau: PF > 1,2; max drawdown < 30%; >= 100 trade; profit di 2000–2012 DAN 2013–sekarang; profit di
    emas DAN perak masing-masing; mayoritas jendela 5 tahun profit. Dijalankan SEKALI lalu dikunci.

Penanganan data (diputuskan 2026-10-13 setelah run pertama berhenti karena data, SEBELUM ada hasil):
  - 441 bar emas & 243 bar perak (2003–2011) punya open/close di luar high/low (umumnya close = settlement
    di luar rentang sesi elektronik, median 0,3–0,4%). High/low diperlebar mencakup open & close:
    pilihan yang merugikan robot (stop lebih mudah tersentuh).
  - Hanya hari ketika emas DAN perak sama-sama punya bar yang dipakai (5 hari dibuang).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd
import yaml

from src import metrics
from src.backtest import BacktestConfig, Sleeve, run_backtest
from src.data import ROOT, load_config
from src.strategies.registry import REGISTRY

DIR = ROOT / "data" / "yahoo"
OUT = ROOT / "reports" / "metals"
LOCK = OUT / "lock.json"
START = "2000-08-30"
SPLIT = "2013-01-01"
ASSETS = {"GOLD": {"ticker": "GC=F", "funding_per_day": 0.000354},
          "SILVER": {"ticker": "SI=F", "funding_per_day": 0.000566}}
RULES = {  # aturan robot yang sedang berjalan, apa adanya
    "robot1_breakout": {"strategy": "donchian",
                        "params": {"entry_period": 55, "exit_period": 20, "atr_period": 14, "atr_mult": 3.0,
                                   "allow_short": True}},
    "robot2_momentum": {"strategy": "tsmom",
                        "params": {"lookback": 60, "atr_period": 14, "atr_mult": 3.0, "allow_short": False}},
}
CRITERIA = {"profit_factor_min": 1.2, "max_drawdown_max": 0.30, "min_trades": 100, "window_years": 5}


def fetch() -> None:
    import yfinance as yf
    DIR.mkdir(parents=True, exist_ok=True)
    for name, a in ASSETS.items():
        df = yf.download(a["ticker"], start=START, interval="1d", progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        df.index = pd.DatetimeIndex(pd.to_datetime(df.index).tz_localize("UTC"), name="time")
        df.to_csv(DIR / f"{name}_1d.csv")
        print(f"[metals] {name} ({a['ticker']}): {len(df)} bar, {df.index[0].date()} s/d {df.index[-1].date()}")


def load(name: str) -> tuple[pd.DataFrame, list[str]]:
    """Bar hari bursa. Anomali DILAPORKAN; baris yang tidak bisa dipakai (NaN/<=0) dibuang dan dicatat."""
    df = pd.read_csv(DIR / f"{name}_1d.csv", index_col=0, parse_dates=True)
    df.index = pd.DatetimeIndex(df.index, name="time")
    notes = []
    bad = df[["open", "high", "low", "close"]].isna().any(axis=1) | (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
    if bad.any():
        notes.append(f"{int(bad.sum())} bar NaN/<=0 dibuang: {', '.join(str(d.date()) for d in df.index[bad][:5])}")
        df = df[~bad]
    hi = df["high"] < df[["open", "close"]].max(axis=1)
    lo = df["low"] > df[["open", "close"]].min(axis=1)
    if (hi | lo).any():
        notes.append(f"{int((hi | lo).sum())} bar high/low tidak konsisten, diperlebar mencakup open/close: "
                     f"{', '.join(str(d.date()) for d in df.index[hi | lo][:5])}")
        df = df.assign(high=df[["high", "open", "close"]].max(axis=1), low=df[["low", "open", "close"]].min(axis=1))
    gaps = df.index.to_series().diff().dt.days
    big = gaps[gaps > 5]
    if len(big):
        notes.append(f"{len(big)} celah > 5 hari kalender: {', '.join(f'{d.date()} ({int(g)} hari)' for d, g in big.head(5).items())}")
    return df, notes


def calendar_funding(index: pd.DatetimeIndex, per_day: float) -> pd.DataFrame:
    """Satu event per bar = funding semua hari kalender sejak bar sebelumnya (akhir pekan ikut dibayar)."""
    days = index.to_series().diff().dt.days.fillna(1).clip(lower=1)
    return pd.DataFrame({"funding_rate": (days * per_day).to_numpy()}, index=index)


def config_hash() -> str:
    keep = {"assets": ASSETS, "rules": RULES, "criteria": CRITERIA, "start": START, "split": SPLIT,
            "costs": load_config()["costs"], "risk": load_config()["risk"]}
    return hashlib.sha256(yaml.safe_dump(keep, sort_keys=True).encode()).hexdigest()[:16]


def _run(bars, funding, rule, bt, symbols, start=None, end=None):
    fn = REGISTRY[rule["strategy"]]
    sleeves = [Sleeve(f"{rule['strategy']}:{s}", s, fn(bars[s], rule["params"]), None) for s in symbols]
    return run_backtest(bars, funding, sleeves, bt, start=start, end=end)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    h = config_hash()
    if LOCK.exists() and json.loads(LOCK.read_text())["hash"] != h:
        raise SystemExit("[metals] uji ini sudah dijalankan dengan setting lain; tidak murni lagi.")
    fetch()
    bars, notes = {}, {}
    for name in ASSETS:
        bars[name], notes[name] = load(name)
    common = bars["GOLD"].index.intersection(bars["SILVER"].index)
    for name in bars:
        dropped = bars[name].index.difference(common)
        if len(dropped):
            notes[name].append(f"{len(dropped)} hari tanpa pasangan dibuang: {', '.join(str(d.date()) for d in dropped)}")
        bars[name] = bars[name].loc[common]
    for name, n in notes.items():
        print(f"[metals] validasi {name}: " + ("; ".join(n) if n else "OK"))
    funding = {n: calendar_funding(b.index, ASSETS[n]["funding_per_day"]) for n, b in bars.items()}
    no_funding = {n: f.assign(funding_rate=0.0) for n, f in funding.items()}
    cfg = load_config()
    bt = replace(BacktestConfig.from_config(cfg), risk_per_trade=0.01)
    names = list(ASSETS)
    rows = []
    for key, rule in RULES.items():
        r = _run(bars, funding, rule, bt, names)
        m = metrics.summarize(r)
        free = metrics.summarize(_run(bars, no_funding, rule, bt, names))
        halves = [metrics.summarize(_run(bars, funding, rule, bt, names, s, e))["total_return"]
                  for s, e in ((START, "2012-12-31"), (SPLIT, None))]
        singles = {n: metrics.summarize(_run(bars, funding, rule, replace(bt, risk_per_trade=0.005), [n]))["total_return"]
                   for n in names}
        eq = r.equity["equity"]
        win = []
        y0 = eq.index[0].year + 1
        for y in range(y0, eq.index[-1].year - CRITERIA["window_years"] + 2, CRITERIA["window_years"]):
            w = eq[(eq.index.year >= y) & (eq.index.year < y + CRITERIA["window_years"])]
            if len(w) > 200:
                win.append((y, w.iloc[-1] / w.iloc[0] - 1))
        pf = m.get("profit_factor", float("nan"))
        checks = {"pass_pf": pf > CRITERIA["profit_factor_min"], "pass_dd": m["max_drawdown"] < CRITERIA["max_drawdown_max"],
                  "pass_trades": m["n_trades"] >= CRITERIA["min_trades"], "pass_halves": all(x > 0 for x in halves),
                  "pass_both_metals": all(v > 0 for v in singles.values()),
                  "pass_windows": sum(v > 0 for _, v in win) > len(win) / 2}
        rows.append({"aturan": key, "total_return": m["total_return"], "cagr": m["cagr"], "max_drawdown": m["max_drawdown"],
                     "sharpe": m["sharpe"], "profit_factor": pf, "n_trades": m["n_trades"],
                     "funding_dibayar": r.trades["funding"].sum(), "return_tanpa_funding": free["total_return"],
                     "return_2000_2012": halves[0], "return_2013_now": halves[1],
                     "return_emas": singles["GOLD"], "return_perak": singles["SILVER"],
                     "jendela_5th": ", ".join(f"{y}-{y + 4}: {v:+.0%}" for y, v in win),
                     **checks, "pass_all": all(checks.values())})
        r.trades.to_csv(OUT / f"trades_{key}.csv", index=False)
        r.equity.to_csv(OUT / f"equity_{key}.csv")
    out = pd.DataFrame(rows)
    out.to_csv(OUT / "results.csv", index=False)
    bh = {n: metrics.equity_metrics(10_000 * b["close"] / b["open"].iloc[0], 10_000) for n, b in bars.items()}
    (OUT / "summary.json").write_text(json.dumps({"buyhold": bh, "validation": notes}, indent=2, default=str) + "\n")
    LOCK.write_text(json.dumps({"hash": h, "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}, indent=2) + "\n")

    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    show = out.T
    print("\n=== Uji emas & perak (2000–sekarang), funding nyata Binance ===")
    print(show.to_string())
    for n, b in bh.items():
        print(f"Buy & hold {n}: {b['total_return']:+.0%}, CAGR {b['cagr']:+.1%}, max DD {b['max_drawdown']:.0%}, Sharpe {b['sharpe']:.2f}")


if __name__ == "__main__":
    main()
