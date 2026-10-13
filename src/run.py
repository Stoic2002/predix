"""M3 — Jalankan S1 dan S2 dengan parameter default di periode in-sample.

    python -m src.run

Hasil: reports/m3_insample/{metrics.csv, trades.csv, rejections.csv}.
Out-of-sample sengaja tidak bisa dipilih di sini; itu bagian M4 dan dijalankan sekali saja.
"""
from __future__ import annotations

import argparse

import pandas as pd

from src import metrics
from src.backtest import BacktestConfig, BacktestResult, Sleeve, run_backtest
from src.data import ROOT, load_bars, load_config, load_funding
from src.strategies import mean_reversion, trend

STRATEGIES = {"s1": mean_reversion.generate_signals, "s2": trend.generate_signals}


def asset(symbol: str) -> str:
    return symbol.split("/")[0]


def load_market(cfg: dict) -> tuple[dict, dict]:
    d = cfg["data"]
    return ({s: load_bars(s, d) for s in d["symbols"]},
            {s: load_funding(s, d) for s in d["symbols"]})


def make_sleeve(strategy: str, symbol: str, bars: pd.DataFrame, params: dict) -> Sleeve:
    """Sinyal dihitung dari seluruh histori supaya indikator sudah panas di awal periode."""
    hold = params.get("max_hold_days")
    return Sleeve(f"{strategy}:{asset(symbol)}", symbol,
                  STRATEGIES[strategy](bars, params), int(hold) if hold else None)


def run_suite(cfg: dict, bars: dict, funding: dict, start, end,
              params: dict | None = None) -> dict[str, BacktestResult]:
    """Run per strategi per simbol (risiko penuh), per strategi (2 simbol), dan portofolio (4 sleeve)."""
    params = params or {k: cfg[k] for k in STRATEGIES}
    bt = BacktestConfig.from_config(cfg)
    sleeves = {(st, sym): make_sleeve(st, sym, bars[sym], params[st])
               for st in STRATEGIES for sym in cfg["data"]["symbols"]}
    runs: dict[str, list[Sleeve]] = {sl.name: [sl] for sl in sleeves.values()}
    for st in STRATEGIES:
        runs[st] = [sl for (s, _), sl in sleeves.items() if s == st]
    runs["portfolio"] = list(sleeves.values())
    return {name: run_backtest(bars, funding, sl, bt, start=start, end=end) for name, sl in runs.items()}


def strategy_correlation(port: BacktestResult) -> float:
    groups = {st: [c for c in port.sleeve_pnl.columns if c.startswith(st + ":")] for st in STRATEGIES}
    daily = metrics.daily_pnl_by_group(port, groups)
    return daily["s1"].corr(daily["s2"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", choices=["in_sample"], default="in_sample")
    ap.parse_args()

    cfg = load_config()
    start, end = cfg["periods"]["in_sample"]
    marks = cfg["risk"]["drawdown_marks"]
    initial = cfg["backtest"]["initial_equity"]
    bars, funding = load_market(cfg)

    results = run_suite(cfg, bars, funding, start, end)
    table = {name: metrics.summarize(r, marks) for name, r in results.items()}
    for sym in cfg["data"]["symbols"]:
        bh = metrics.buy_and_hold(bars[sym], initial, start, end)
        table[f"buyhold:{asset(sym)}"] = metrics.equity_metrics(bh, initial, marks)

    out = ROOT / "reports" / "m3_insample"
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(table).to_csv(out / "metrics.csv")
    single = [n for n in results if ":" in n]
    pd.concat([results[n].trades.assign(run=n) for n in single]).to_csv(out / "trades.csv", index=False)
    rej = [results[n].rejections.assign(run=n) for n in single if not results[n].rejections.empty]
    (pd.concat(rej) if rej else pd.DataFrame()).to_csv(out / "rejections.csv", index=False)

    pd.set_option("display.width", 200)
    print(f"In-sample {start} s/d {end}, ekuitas awal {initial:,.0f}, parameter default\n")
    print(metrics.fmt_table(table).to_string())
    print(f"\nKorelasi PnL harian S1 vs S2 (portofolio): {strategy_correlation(results['portfolio']):.3f}")
    print(f"\nTersimpan di {out.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
