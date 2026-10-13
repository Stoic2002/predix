"""Uji versi 4 jam: aturan robot yang sedang berjalan di candle 4 jam vs versi harian.

    python -m src.h4

Ditetapkan 2026-10-13 SEBELUM hasilnya dilihat:
  - Data: futures Binance 4 jam sejak 2020-01-01, funding nyata (dikelompokkan per candle 4 jam).
  - Strategi: aturan Robot 1 (Donchian 55/20 long-short, BTC & ETH) dan Robot 2 (TSMOM 60 beli saja, 10 koin)
    dengan JUMLAH CANDLE yang sama (55 candle 4 jam ≈ 9 hari: robot jauh lebih cepat). Risiko, biaya, stop
    sama dengan robot live.
  - Pembanding: versi harian yang persis sama di periode yang sama.
  - Layak kalau: PF > 1,2 di 2020–2023 DAN 2024–sekarang; max drawdown < 30%; >= 100 trade; DAN Sharpe
    (dari ekuitas harian) lebih tinggi daripada versi harian. Dijalankan SEKALI lalu dikunci.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import ccxt
import pandas as pd
import yaml

from src import metrics
from src.backtest import BacktestResult, run_backtest
from src.data import (OHLCV_COLS, ROOT, fetch_ohlcv_since, load_bars, load_config, load_funding, merge_save,
                      ohlcv_path, read_csv, validate_ohlcv)
from src.paper import build_sleeves, paper_settings

OUT = ROOT / "reports" / "h4"
LOCK = OUT / "lock.json"
START, SPLIT = "2020-01-01", "2024-01-01"
CRITERIA = {"profit_factor_min": 1.2, "max_drawdown_max": 0.30, "min_trades": 100}


def fetch_4h(symbols: list[str]) -> None:
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    ex.load_markets()
    d = ROOT / "data"
    for s in symbols:
        p = ohlcv_path(d, s, "4h")
        old = read_csv(p, OHLCV_COLS)
        since = int(old["timestamp"].max()) + 4 * 3_600_000 if not old.empty else ex.parse8601(f"{START}T00:00:00Z")
        df = merge_save(p, old, fetch_ohlcv_since(ex, s, "4h", since))
        issues = [i for i in validate_ohlcv(df, "4h", START) if "(info)" not in i]
        print(f"[h4] {s}: {len(df)} candle 4 jam" + (f" | ANOMALI: {issues}" if issues else " | OK"))


def load_4h(symbol: str) -> pd.DataFrame:
    df = read_csv(ohlcv_path(ROOT / "data", symbol, "4h"), OHLCV_COLS)
    df.index = pd.DatetimeIndex(pd.to_datetime(df["timestamp"], unit="ms", utc=True), name="time")
    return df.drop(columns="timestamp")


def daily_view(res: BacktestResult) -> BacktestResult:
    """Metrik ekuitas dihitung dari close harian supaya Sharpe (×√365) sebanding dengan versi harian."""
    eq = res.equity.resample("1D").last().dropna()
    return BacktestResult(equity=eq, sleeve_pnl=res.sleeve_pnl, trades=res.trades, rejections=res.rejections,
                          config=res.config, risk_per_trade=res.risk_per_trade)


def config_hash(cfg: dict) -> str:
    keep = {"paper": {k: v for k, v in cfg["paper"].items() if k != "db"}, "s2": cfg["s2"],
            "paper_tsmom": {k: v for k, v in cfg["paper_tsmom"].items() if k != "db"},
            "costs": cfg["costs"], "risk": cfg["risk"], "start": START, "split": SPLIT, "criteria": CRITERIA}
    return hashlib.sha256(yaml.safe_dump(keep, sort_keys=True).encode()).hexdigest()[:16]


def evaluate(cfg: dict, sec: str, bars: dict, funding: dict, bar: str) -> dict:
    pc, bt = paper_settings(cfg, sec)
    sleeves = build_sleeves(cfg, bars, sec)
    out = {}
    for label, (s, e) in {"full": (START, None), "2020-2023": (START, "2023-12-31 23:59"),
                          "2024-now": (SPLIT, None)}.items():
        r = run_backtest(bars, funding, sleeves, bt, start=s, end=e, bar=bar)
        if bar != "D":
            r = daily_view(r)
        m = metrics.summarize(r)
        out[label] = {"total_return": m["total_return"], "cagr": m["cagr"], "max_drawdown": m["max_drawdown"],
                      "sharpe": m["sharpe"], "profit_factor": m.get("profit_factor", float("nan")),
                      "n_trades": m["n_trades"], "fees": m.get("total_fees", 0.0), "funding": m.get("total_funding", 0.0),
                      "avg_bars_held": m.get("avg_bars_held", float("nan"))}
    return out


def main() -> None:
    cfg = load_config()
    OUT.mkdir(parents=True, exist_ok=True)
    h = config_hash(cfg)
    if LOCK.exists() and json.loads(LOCK.read_text())["hash"] != h:
        raise SystemExit("[h4] uji ini sudah dijalankan dengan setting lain; tidak murni lagi.")
    syms = list(dict.fromkeys(cfg["paper"]["symbols"] + cfg["paper_tsmom"]["symbols"]))
    fetch_4h(syms)
    funding = {s: load_funding(s) for s in syms}
    bars4 = {s: load_4h(s) for s in syms}
    bars1 = {s: load_bars(s) for s in syms}
    rows = []
    for sec in ("paper", "paper_tsmom"):
        name = cfg["display"]["robots"][sec]["name"]
        sym = cfg[sec]["symbols"]
        r4 = evaluate(cfg, sec, {s: bars4[s] for s in sym}, {s: funding[s] for s in sym}, "4h")
        r1 = evaluate(cfg, sec, {s: bars1[s] for s in sym}, {s: funding[s] for s in sym}, "D")
        checks = {"pass_pf": r4["2020-2023"]["profit_factor"] > CRITERIA["profit_factor_min"]
                  and r4["2024-now"]["profit_factor"] > CRITERIA["profit_factor_min"],
                  "pass_dd": r4["full"]["max_drawdown"] < CRITERIA["max_drawdown_max"],
                  "pass_trades": r4["full"]["n_trades"] >= CRITERIA["min_trades"],
                  "pass_beats_daily": r4["full"]["sharpe"] > r1["full"]["sharpe"]}
        for tf, res in (("4 jam", r4), ("harian", r1)):
            for period, m in res.items():
                rows.append({"robot": name, "timeframe": tf, "periode": period, **m})
        rows.append({"robot": name, "timeframe": "KEPUTUSAN", "periode": "",
                     **{k: v for k, v in checks.items()}, "layak": all(checks.values())})
    out = pd.DataFrame(rows)
    out.to_csv(OUT / "results.csv", index=False)
    LOCK.write_text(json.dumps({"hash": h, "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
                               indent=2) + "\n")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    res = out[out.timeframe != "KEPUTUSAN"].copy()
    for c in ("total_return", "cagr", "max_drawdown"):
        res[c] = res[c].map(lambda v: f"{v:+.1%}")
    for c in ("sharpe", "profit_factor", "fees", "funding", "avg_bars_held"):
        res[c] = res[c].map(lambda v: f"{v:,.2f}")
    print("\n=== Uji 4 jam vs harian (setelah semua biaya) ===")
    print(res[["robot", "timeframe", "periode", "total_return", "cagr", "max_drawdown", "sharpe", "profit_factor",
               "n_trades", "fees", "funding", "avg_bars_held"]].to_string(index=False))
    print("\nKeputusan:")
    print(out[out.timeframe == "KEPUTUSAN"][["robot", "pass_pf", "pass_dd", "pass_trades", "pass_beats_daily", "layak"]]
          .to_string(index=False))


if __name__ == "__main__":
    main()
