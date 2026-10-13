"""M4 — Validasi: sensitivitas (in-sample), walk-forward, out-of-sample sekali, benchmark, Monte Carlo.

    python -m src.validate

Parameter strategi tidak disetel di langkah mana pun; semuanya memakai nilai di config.yaml.
Out-of-sample dikunci ke hash config: kalau config strategi/biaya/risiko berubah setelah OOS
pernah dijalankan, script menolak jalan kecuali diberi --allow-oos-rerun (dan itu harus
dicatat di laporan, karena hasil OOS berikutnya tidak lagi murni).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yaml

from src import metrics
from src.backtest import BacktestConfig, BacktestResult, run_backtest
from src.data import ROOT, load_config
from src.run import STRATEGIES, asset, load_market, make_sleeve, run_suite, strategy_correlation

OUT = ROOT / "reports" / "m4"
LOCK = OUT / "oos_lock.json"
LOCKED_SECTIONS = ["data", "periods", "backtest", "costs", "risk", *STRATEGIES, "validation"]


# ---------------------------------------------------------------- helpers

def config_hash(cfg: dict) -> str:
    blob = yaml.safe_dump({k: cfg.get(k) for k in LOCKED_SECTIONS}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def check_oos_lock(cfg: dict, allow_rerun: bool) -> None:
    if not LOCK.exists():
        return
    lock = json.loads(LOCK.read_text())
    if lock["config_hash"] == config_hash(cfg):
        print(f"[oos] config sama dengan run OOS {lock['run_at']}; hasil akan identik.")
        return
    msg = (f"[oos] config berubah sejak OOS dijalankan ({lock['run_at']}, hash {lock['config_hash']}). "
           "Menjalankan ulang OOS dengan config baru membuat hasilnya tidak murni lagi.")
    if not allow_rerun:
        raise SystemExit(msg + " Pakai --allow-oos-rerun kalau memang disengaja, lalu catat di laporan.")
    print(msg + " Dilanjutkan karena --allow-oos-rerun.")


def run_strategy(cfg: dict, bars: dict, funding: dict, strategy: str, params: dict,
                 start, end, symbols: list[str] | None = None) -> BacktestResult:
    symbols = symbols or cfg["data"]["symbols"]
    sleeves = [make_sleeve(strategy, s, bars[s], params) for s in symbols]
    return run_backtest(bars, funding, sleeves, BacktestConfig.from_config(cfg), start=start, end=end)


def brief(res: BacktestResult) -> dict:
    m = metrics.summarize(res)
    return {k: m.get(k) for k in ("total_return", "profit_factor", "max_drawdown", "sharpe", "n_trades")}


# ---------------------------------------------------------------- 1. sensitivitas

def sensitivity_variants(cfg: dict, strategy: str):
    base = cfg[strategy]
    yield "default", base
    for key, values in cfg["validation"]["sensitivity"][strategy].items():
        for v in values:
            p = dict(base)
            if key == "rsi_entry":
                p["rsi_entry_long"], p["rsi_entry_short"] = v, 100 - v
            elif key == "rsi_exit":
                p["rsi_exit_long"], p["rsi_exit_short"] = v, 100 - v
            else:
                p[key] = v
            yield f"{key}={v}", p


def sensitivity(cfg: dict, bars: dict, funding: dict) -> pd.DataFrame:
    start, end = cfg["periods"]["in_sample"]
    rows = []
    for st in STRATEGIES:
        for label, p in sensitivity_variants(cfg, st):
            row = {"strategy": st, "variant": label,
                   **brief(run_strategy(cfg, bars, funding, st, p, start, end))}
            for sym in cfg["data"]["symbols"]:
                r = run_strategy(cfg, bars, funding, st, p, start, end, [sym])
                row[f"return_{asset(sym)}"] = metrics.equity_metrics(
                    r.equity["equity"], r.config.initial_equity)["total_return"]
            rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 2. walk-forward

def walk_forward_windows(cfg: dict, first_bar: pd.Timestamp, last_bar: pd.Timestamp) -> list[tuple]:
    wf = cfg["validation"]["walk_forward"]
    oos_start = pd.Timestamp(cfg["periods"]["out_of_sample_start"], tz="UTC")
    step = pd.DateOffset(months=int(wf["test_months"]))
    start = first_bar + pd.DateOffset(years=int(wf["train_years"]))
    windows = []
    while start <= last_bar:
        full_end = start + step - pd.Timedelta(days=1)
        end = min(full_end, last_bar)
        windows.append((start, end, "OOS" if start >= oos_start else "IS", end < full_end))
        start = start + step
    return windows


def walk_forward(cfg: dict, bars: dict, funding: dict) -> pd.DataFrame:
    any_bars = next(iter(bars.values()))
    rows = []
    for start, end, segment, partial in walk_forward_windows(cfg, any_bars.index[0], any_bars.index[-1]):
        results = run_suite(cfg, bars, funding, start, end)
        for name in (*STRATEGIES, "portfolio"):
            rows.append({"test_start": start.date(), "test_end": end.date(), "segment": segment,
                         "partial": partial, "run": name, **brief(results[name])})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 3. Monte Carlo (opsional)

def monte_carlo_drawdown(r_multiples: np.ndarray, risk_frac: float, n: int, seed: int) -> dict:
    """Acak urutan trade; tiap trade mengubah ekuitas sebesar r × risiko per trade (majemuk).
    Perkiraan kasar: mengabaikan posisi yang tumpang-tindih."""
    rng = np.random.default_rng(seed)
    rets = np.asarray(r_multiples) * risk_frac

    def max_dd(seq):
        eq = np.r_[1.0, np.cumprod(1 + seq)]
        return (1 - eq / np.maximum.accumulate(eq)).max()

    sims = np.array([max_dd(rng.permutation(rets)) for _ in range(n)])
    return {"actual_order": max_dd(rets), "median": np.median(sims),
            "p95": np.percentile(sims, 95), "p99": np.percentile(sims, 99), "worst": sims.max()}


# ---------------------------------------------------------------- 4. kriteria

def evaluate(cfg: dict, oos: dict, is_: dict, sens: pd.DataFrame, wf: pd.DataFrame) -> pd.DataFrame:
    c = cfg["validation"]["criteria"]
    rows = []
    for st in STRATEGIES:
        o = metrics.summarize(oos[st])
        n_total = len(is_[st].trades) + len(oos[st].trades)
        s = sens[sens.strategy == st]
        losing = s[s.total_return <= 0].variant.tolist()
        w = wf[wf.run == st]
        frac = (w.total_return > 0).mean()
        checks = [
            ("Profit factor OOS setelah biaya", f"> {c['oos_profit_factor_min']}",
             f"{o.get('profit_factor', float('nan')):.2f}",
             o.get("profit_factor", 0) > c["oos_profit_factor_min"]),
            ("Max drawdown OOS", f"< {c['oos_max_drawdown_max']:.0%}", f"{o['max_drawdown']:.1%}",
             o["max_drawdown"] < c["oos_max_drawdown_max"]),
            ("Jumlah trade IS + OOS", f">= {c['min_trades_total']}", str(n_total),
             n_total >= c["min_trades_total"]),
            ("Sensitivitas: tidak ada varian rugi (IS)", "0 varian rugi",
             f"{len(losing)}/{len(s)} rugi" + (f": {', '.join(losing)}" if losing else ""), not losing),
            ("Walk-forward: mayoritas jendela profit", f"> {c['walk_forward_min_profitable_frac']:.0%}",
             f"{(w.total_return > 0).sum()}/{len(w)} ({frac:.0%})",
             frac > c["walk_forward_min_profitable_frac"]),
        ]
        rows += [{"strategy": st, "kriteria": k, "ambang": a, "hasil": h, "lolos": bool(ok)}
                 for k, a, h, ok in checks]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--allow-oos-rerun", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    check_oos_lock(cfg, args.allow_oos_rerun)
    OUT.mkdir(parents=True, exist_ok=True)
    bars, funding = load_market(cfg)
    marks = cfg["risk"]["drawdown_marks"]
    initial = cfg["backtest"]["initial_equity"]
    is_start, is_end = cfg["periods"]["in_sample"]
    oos_start = cfg["periods"]["out_of_sample_start"]
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)

    print("=== 1. Sensitivitas parameter (in-sample saja) ===")
    sens = sensitivity(cfg, bars, funding)
    sens.to_csv(OUT / "sensitivity.csv", index=False)
    show = sens.copy()
    for col in ("total_return", *[c for c in sens if c.startswith("return_")]):
        show[col] = show[col].map(lambda v: f"{v:+.1%}")
    show["max_drawdown"] = show["max_drawdown"].map(lambda v: f"{v:.1%}")
    for col in ("profit_factor", "sharpe"):
        show[col] = show[col].map(lambda v: f"{v:.2f}")
    print(show.to_string(index=False))

    print("\n=== 2. Walk-forward (parameter default, jendela uji 6 bulan) ===")
    wf = walk_forward(cfg, bars, funding)
    wf.to_csv(OUT / "walk_forward.csv", index=False)
    piv = wf.pivot_table(index=["test_start", "segment"], columns="run", values="total_return")
    print(piv.map(lambda v: f"{v:+.1%}").to_string())

    print("\n=== 3. In-sample dan out-of-sample (sekali), parameter default ===")
    is_res = run_suite(cfg, bars, funding, is_start, is_end)
    oos_res = run_suite(cfg, bars, funding, oos_start, None)
    LOCK.write_text(json.dumps({"config_hash": config_hash(cfg),
                                "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
                               indent=2) + "\n")
    tables = {}
    for label, res, (s, e) in (("is", is_res, (is_start, is_end)), ("oos", oos_res, (oos_start, None))):
        t = {name: metrics.summarize(r, marks) for name, r in res.items()}
        for sym in cfg["data"]["symbols"]:
            t[f"buyhold:{asset(sym)}"] = metrics.equity_metrics(
                metrics.buy_and_hold(bars[sym], initial, s, e), initial, marks)
        tables[label] = pd.DataFrame(t)
        tables[label].to_csv(OUT / f"metrics_{label}.csv")
        pd.concat([r.trades.assign(run=n) for n, r in res.items()]).to_csv(OUT / f"trades_{label}.csv", index=False)
        eq = pd.DataFrame({n: r.equity["equity"] for n, r in res.items()})
        eq.to_csv(OUT / f"equity_{label}.csv")
    last = oos_res["portfolio"].equity.index[-1].date()
    print(f"OOS {oos_start} s/d {last}:")
    print(metrics.fmt_table(tables["oos"].to_dict()).to_string())
    corr = {"is": strategy_correlation(is_res["portfolio"]), "oos": strategy_correlation(oos_res["portfolio"])}
    print(f"\nKorelasi PnL harian S1 vs S2: in-sample {corr['is']:.3f}, out-of-sample {corr['oos']:.3f}")

    print("\n=== 4. Monte Carlo: urutan trade diacak (IS + OOS), sebaran max drawdown ===")
    mc_cfg = cfg["validation"]["monte_carlo"]
    mc = {}
    for name in (*STRATEGIES, "portfolio"):
        r = pd.concat([is_res[name].trades.r_multiple, oos_res[name].trades.r_multiple]).to_numpy()
        mc[name] = monte_carlo_drawdown(r, is_res[name].risk_per_trade, int(mc_cfg["n"]), int(mc_cfg["seed"]))
    mc_df = pd.DataFrame(mc)
    mc_df.to_csv(OUT / "monte_carlo.csv")
    print(mc_df.map(lambda v: f"{v:.1%}").to_string())

    print("\n=== 5. Kriteria lolos ===")
    crit = evaluate(cfg, oos_res, is_res, sens, wf)
    crit.to_csv(OUT / "criteria.csv", index=False)
    print(crit.to_string(index=False))
    for st in STRATEGIES:
        ok = crit[crit.strategy == st].lolos.all()
        print(f"{st.upper()}: {'LOLOS' if ok else 'TIDAK LOLOS'} ke paper trading")
    json.dump({"correlation": corr}, open(OUT / "summary.json", "w"), indent=2)
    print(f"\nTersimpan di {OUT.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
