"""Uji pra-2020 (stress test crash 2018): strategi robot yang sedang berjalan di data spot Binance 2017–2019.

    python -m src.pre2020

Ditetapkan 2026-10-13 SEBELUM hasilnya dilihat:
  - Data: spot Binance harian, 17 Agu 2017 s/d 31 Des 2019 (disimpan di data/spot/, terpisah dari futures).
  - Setting: persis seperti robot di VPS (biaya, stop, risiko per trade PER KOIN sama dengan robot live).
    Robot 2 hanya memakai koin yang sudah listing saat itu (SOL & AVAX belum ada).
  - Funding: belum ada perpetual, jadi diasumsikan 0,01% per 8 jam (tarif dasar Binance, kira-kira rata-rata
    historis). Long membayar, short menerima.
  - Bertahan kalau: profit factor > 1,0 (tidak rugi) dan max drawdown < 30%. Dijalankan SEKALI lalu dikunci.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone

import ccxt
import pandas as pd
import yaml

from src import metrics
from src.backtest import run_backtest
from src.data import (OHLCV_COLS, ROOT, fetch_ohlcv_since, load_config, merge_save, ohlcv_path, read_csv,
                      validate_ohlcv)
from src.paper import build_sleeves, paper_settings, strategy_params

SPOT_DIR = ROOT / "data" / "spot"
OUT = ROOT / "reports" / "pre2020"
LOCK = OUT / "lock.json"
START, END = "2017-08-17", "2019-12-31"
FUNDING_RATE = 0.0001
CRITERIA = {"profit_factor_min": 1.0, "max_drawdown_max": 0.30}


def spot_symbol(perp: str) -> str:
    return perp.split(":")[0]  # BTC/USDT:USDT -> BTC/USDT


def fetch_spot(symbols: list[str]) -> None:
    ex = ccxt.binance({"enableRateLimit": True})
    ex.load_markets()
    SPOT_DIR.mkdir(parents=True, exist_ok=True)
    for perp in symbols:
        s = spot_symbol(perp)
        p = ohlcv_path(SPOT_DIR, perp, "1d")
        old = read_csv(p, OHLCV_COLS)
        since = int(old["timestamp"].max()) + 86_400_000 if not old.empty else ex.parse8601(f"{START}T00:00:00Z")
        new = fetch_ohlcv_since(ex, s, "1d", since)
        new = new[new["timestamp"] <= ex.parse8601(f"{END}T00:00:00Z")]
        df = merge_save(p, old, new)
        print(f"[pre2020] {s}: {len(df)} candle")


def load_spot(perp: str) -> pd.DataFrame | None:
    p = ohlcv_path(SPOT_DIR, perp, "1d")
    if not p.exists():
        return None
    df = read_csv(p, OHLCV_COLS)
    if df.empty:
        return None
    issues = [i for i in validate_ohlcv(df, "1d", START) if "(info)" not in i]
    if issues:
        raise SystemExit(f"[pre2020] data {perp} bermasalah: {issues}")
    df.index = pd.DatetimeIndex(pd.to_datetime(df["timestamp"], unit="ms", utc=True), name="time")
    return df.drop(columns="timestamp")


def synthetic_funding(index: pd.DatetimeIndex) -> pd.DataFrame:
    t = pd.date_range(index[0], index[-1] + pd.Timedelta(hours=16), freq="8h", tz="UTC", name="time")
    return pd.DataFrame({"funding_rate": FUNDING_RATE}, index=t)


def settings_hash(cfg: dict) -> str:
    keep = {sec: {"paper": {k: v for k, v in cfg[sec].items() if k != "db"},
                  "strategy": strategy_params(cfg, cfg[sec])} for sec in ("paper", "paper_tsmom")}
    keep.update(costs=cfg["costs"], risk=cfg["risk"], period=[START, END], funding=FUNDING_RATE, criteria=CRITERIA)
    return hashlib.sha256(yaml.safe_dump(keep, sort_keys=True).encode()).hexdigest()[:16]


def run(cfg: dict) -> pd.DataFrame:
    h = settings_hash(cfg)
    if LOCK.exists():
        lock = json.loads(LOCK.read_text())
        if lock["hash"] != h:
            raise SystemExit(f"[pre2020] uji ini sudah dijalankan {lock['run_at']} dengan setting lain; tidak murni lagi.")
    rows, yearly = [], {}
    for sec in ("paper", "paper_tsmom"):
        pc, bt = paper_settings(cfg, sec)
        bars = {s: b for s in pc["symbols"] if (b := load_spot(s)) is not None}
        n_live = len(pc["symbols"])
        # risiko per trade PER KOIN sama dengan robot live (engine membagi rata ke jumlah koin yang ada)
        bt = replace(bt, risk_per_trade=bt.risk_per_trade / n_live * len(bars))
        sub = {**cfg, sec: {**pc, "symbols": list(bars)}}
        fund = {s: synthetic_funding(b.index) for s, b in bars.items()}
        r = run_backtest(bars, fund, build_sleeves(sub, bars, sec), bt, start=START, end=END)
        m = metrics.summarize(r)
        eq = r.equity["equity"]
        ye = eq.groupby(eq.index.year).last()
        prev = pd.concat([pd.Series([bt.initial_equity]), ye.iloc[:-1]]).to_numpy()
        name = cfg["display"]["robots"][sec]["name"]
        yearly[name] = {int(y): float(v) for y, v in (ye / prev - 1).items()}
        pf = m.get("profit_factor", float("nan"))
        rows.append({"robot": name, "koin": ", ".join(s.split("/")[0] for s in bars),
                     "total_return": m["total_return"], "cagr": m["cagr"], "max_drawdown": m["max_drawdown"],
                     "sharpe": m["sharpe"], "profit_factor": pf, "n_trades": m["n_trades"],
                     "pass": bool(pf > CRITERIA["profit_factor_min"] and m["max_drawdown"] < CRITERIA["max_drawdown_max"])})
        OUT.mkdir(parents=True, exist_ok=True)
        r.trades.to_csv(OUT / f"trades_{sec}.csv", index=False)
        r.equity.to_csv(OUT / f"equity_{sec}.csv")
    btc = load_spot("BTC/USDT:USDT")
    btc = btc[(btc.index >= pd.Timestamp(START, tz="UTC")) & (btc.index <= pd.Timestamp(END, tz="UTC"))]
    bh = metrics.equity_metrics(10_000 * btc["close"] / btc["open"].iloc[0], 10_000)
    out = pd.DataFrame(rows)
    out.to_csv(OUT / "results.csv", index=False)
    (OUT / "summary.json").write_text(json.dumps({"yearly": yearly, "buyhold_btc": bh}, indent=2, default=str) + "\n")
    LOCK.write_text(json.dumps({"hash": h, "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
                               indent=2) + "\n")
    return out


def main() -> None:
    cfg = load_config()
    syms = list(dict.fromkeys(cfg["paper"]["symbols"] + cfg["paper_tsmom"]["symbols"]))
    fetch_spot(syms)
    out = run(cfg)
    s = json.loads((OUT / "summary.json").read_text())
    pd.set_option("display.width", 200)
    show = out.copy()
    for c in ("total_return", "cagr", "max_drawdown"):
        show[c] = show[c].map(lambda v: f"{v:+.1%}" if c != "max_drawdown" else f"{v:.1%}")
    for c in ("sharpe", "profit_factor"):
        show[c] = show[c].map(lambda v: f"{v:.2f}")
    print(f"\n=== Uji pra-2020 ({START} s/d {END}), data spot, funding diasumsikan {FUNDING_RATE:.2%}/8 jam ===")
    print(show.to_string(index=False))
    print("\nReturn per tahun:", json.dumps({k: {y: f"{v:+.1%}" for y, v in d.items()} for k, d in s["yearly"].items()},
                                            indent=1))
    bh = s["buyhold_btc"]
    print(f"\nBuy & hold BTC periode sama: {bh['total_return']:+.1%}, max drawdown {bh['max_drawdown']:.1%}, "
          f"Sharpe {bh['sharpe']:.2f}")


if __name__ == "__main__":
    main()
