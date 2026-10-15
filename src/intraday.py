"""Putaran 4 — riset intraday (candle 1 jam). Kandidat & kriteria ada di config (research.candidates i1–i3).

    python -m src.intraday fetch          # ambil/ perbarui candle 1 jam (riset + koin segar), validasi
    python -m src.lab screen              # seleksi (kandidat baru saja), protokol sama dengan putaran lain
    python -m src.intraday costs          # INFORMASI: kandidat intraday dengan biaya paling optimistis

Skenario biaya tidak memengaruhi lolos/gagal; dijalankan di periode seleksi saja supaya holdout tetap murni.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace

import ccxt
import pandas as pd

from src import lab, metrics
from src.data import (OHLCV_COLS, ROOT, fetch_ohlcv_since, load_config, load_funding, merge_save, ohlcv_path,
                      read_csv, validate_ohlcv)

TF, START = "1h", "2020-01-01"
OUT = ROOT / "reports" / "lab"


def symbols(cfg: dict) -> list[str]:
    r = cfg["research"]
    return list(dict.fromkeys(r["universe"] + r["fresh_holdout"]["universe"]))


def fetch(cfg: dict) -> None:
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    ex.load_markets()
    d = ROOT / cfg["data"]["dir"]
    for s in symbols(cfg):
        p = ohlcv_path(d, s, TF)
        old = read_csv(p, OHLCV_COLS)
        since = int(old["timestamp"].max()) + 3_600_000 if not old.empty else ex.parse8601(f"{START}T00:00:00Z")
        df = merge_save(p, old, fetch_ohlcv_since(ex, s, TF, since))
        issues = [i for i in validate_ohlcv(df, TF, START) if "(info)" not in i]
        print(f"[intraday] {s}: {len(df)} candle 1 jam" + (f" | ANOMALI: {issues}" if issues else " | OK"), flush=True)


def intraday_candidates(cfg: dict) -> dict[str, dict]:
    return {k: v for k, v in lab.expand_candidates(cfg).items() if v.get("timeframe", "1d") != "1d"}


def costs(cfg: dict) -> pd.DataFrame:
    info = cfg["research"]["intraday_cost_info"]
    start, end = cfg["research"]["periods"]["selection"]
    base = lab.lab_config(cfg)
    cheap = replace(base, fee_taker=float(info["fee_taker"]), slippage=float(info["slippage"]))
    funding = {s: load_funding(s) for s in cfg["research"]["universe"]}
    rows = []
    for name, cand in intraday_candidates(cfg).items():
        for label, bt in (("biaya robot (Binance taker)", base), ("optimistis (maker HL, tanpa slippage)", cheap)):
            res = lab.run_candidate(cfg, {}, funding, name, cand, cand["params"], start, end, bt=bt)
            m = metrics.summarize(res)
            t = res.trades
            rows.append({"candidate": name, "biaya": label, "total_return": m["total_return"],
                         "profit_factor": m.get("profit_factor", math.nan), "max_drawdown": m["max_drawdown"],
                         "n_trades": m["n_trades"], "winrate": m.get("winrate", math.nan),
                         "fees_and_slippage": float(t["fees"].sum() + t["slippage"].sum()) if len(t) else 0.0,
                         "gross_pnl": float(t["gross_pnl"].sum()) if len(t) else 0.0})
            print(f"[intraday] {name} | {label}: PF {rows[-1]['profit_factor']:.2f}, "
                  f"return {m['total_return']:+.1%}, {m['n_trades']} trade", flush=True)
    out = pd.DataFrame(rows)
    out.to_csv(OUT / "intraday_costs_info.csv", index=False)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["fetch", "costs"])
    args = ap.parse_args()
    cfg = load_config()
    if args.cmd == "fetch":
        fetch(cfg)
    else:
        print(json.dumps(costs(cfg).to_dict("records"), indent=1, default=str))


if __name__ == "__main__":
    main()
