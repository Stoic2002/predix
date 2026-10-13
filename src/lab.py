"""Fase 2 B2 — Lab riset: seleksi kandidat strategi di banyak simbol dengan protokol pra-registrasi.

    python -m src.lab screen              # seleksi semua kandidat di periode selection (2020–2023)
    python -m src.lab holdout NAMA ...    # holdout SEKALI untuk kandidat yang lolos seleksi

Protokol (lihat bagian research di config.yaml):
  - Kandidat, parameter, varian sensitivitas, dan kriteria ditulis sebelum hasil dilihat.
  - Seleksi hanya memakai periode selection. Holdout dijalankan sekali per kandidat dan dikunci.
  - Setiap run (default + varian) dicatat di reports/lab/research_log.csv. Jumlah percobaan
    dipakai untuk Deflated Sharpe Ratio: makin banyak yang dicoba, makin tinggi standar lolosnya.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime, timezone
from statistics import NormalDist

import numpy as np
import pandas as pd
import yaml

from src import metrics
from src.backtest import BacktestConfig, BacktestResult, Sleeve, run_backtest
from src.data import ROOT, load_bars, load_config, load_funding
from src.run import asset
from src.strategies.registry import signals_for

LAB = ROOT / "reports" / "lab"
LOG = LAB / "research_log.csv"
HOLDOUT_LOCK = LAB / "holdout_lock.json"
FRESH_LOCK = LAB / "fresh_holdout_lock.json"
EULER_GAMMA = 0.5772156649


# ---------------------------------------------------------------- setup

def _hash(obj) -> str:
    return hashlib.sha256(yaml.safe_dump(obj, sort_keys=True).encode()).hexdigest()[:16]


def research_hash(cfg: dict) -> str:
    """Hash seluruh bagian riset (dipakai sampai 2026-10-12; sekarang hanya untuk catatan)."""
    return _hash({"research": cfg["research"], "costs": cfg["costs"], "risk": cfg["risk"], "backtest": cfg["backtest"]})


def candidate_hash(cfg: dict, key: str) -> str:
    """Kunci satu kandidat: definisinya + semua aturan yang memengaruhi hasilnya. Menambah kandidat lain
    tidak mengubah kunci kandidat yang sudah ada, jadi hasil lamanya tetap sah dan tidak dicatat ulang."""
    r = cfg["research"]
    return _hash({"candidate": expand_candidates(cfg)[key], "periods": r["periods"], "criteria": r["criteria"],
                  "risk_per_trade": r["risk_per_trade"], "universe": r["universe"], "costs": cfg["costs"],
                  "risk": cfg["risk"], "backtest": cfg["backtest"]})


def expand_candidates(cfg: dict) -> dict[str, dict]:
    """t1_donchian x [long_short, long_only] -> t1_donchian_ls, t1_donchian_lo."""
    r = cfg["research"]
    out = {}
    for name, c in r["candidates"].items():
        for mode in r["modes"]:
            key = f"{name}_{'ls' if mode == 'long_short' else 'lo'}"
            out[key] = {"strategy": c["strategy"], "mode": mode, "sensitivity": c.get("sensitivity", {}),
                        "params": {**c["params"], "allow_short": mode == "long_short"}}
    return out


def variants(cand: dict):
    yield "default", cand["params"]
    for key, values in cand["sensitivity"].items():
        for v in values:
            yield f"{key}={v}", {**cand["params"], key: v}


def lab_config(cfg: dict) -> BacktestConfig:
    return replace(BacktestConfig.from_config(cfg), risk_per_trade=float(cfg["research"]["risk_per_trade"]))


def run_candidate(cfg: dict, bars: dict, funding: dict, name: str, cand: dict, params: dict,
                  start, end, symbols: list[str] | None = None, universe: list[str] | None = None) -> BacktestResult:
    universe = universe or cfg["research"]["universe"]
    symbols = symbols or universe
    sigs = signals_for(cand["strategy"], params, bars, funding, symbols, universe)
    hold = params.get("max_hold_days")
    sleeves = [Sleeve(f"{name}:{asset(s)}", s, sigs[s], int(hold) if hold else None) for s in symbols]
    return run_backtest(bars, funding, sleeves, lab_config(cfg), start=start, end=end)


def rolling_windows(start: str, end: str, months: int) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Mulai satu tahun setelah awal periode (tahun pertama = pemanasan indikator)."""
    s = pd.Timestamp(start, tz="UTC") + pd.DateOffset(years=1)
    stop = pd.Timestamp(end, tz="UTC")
    out = []
    while s <= stop:
        e = min(s + pd.DateOffset(months=months) - pd.Timedelta(days=1), stop)
        out.append((s, e))
        s = s + pd.DateOffset(months=months)
    return out


# ---------------------------------------------------------------- statistik

def return_stats(equity: pd.Series, initial: float) -> dict:
    eq = pd.concat([pd.Series([initial]), equity.reset_index(drop=True)])
    r = eq.pct_change().dropna()
    sd = r.std()
    return {"sharpe_daily": r.mean() / sd if sd > 0 else 0.0, "n_obs": len(r),
            "skew": float(r.skew()) if sd > 0 else 0.0, "kurt": float(r.kurt() + 3) if sd > 0 else 3.0}


def deflated_sharpe(sr: float, n_trials: int, var_sr: float, n_obs: int, skew: float, kurt: float) -> float:
    """Probabilitas Sharpe sebenarnya > 0 setelah memperhitungkan jumlah percobaan
    (Bailey & López de Prado, 2014). sr dalam satuan harian, bukan tahunan."""
    z = NormalDist().inv_cdf
    sr0 = 0.0
    if n_trials > 1 and var_sr > 0:
        sr0 = math.sqrt(var_sr) * ((1 - EULER_GAMMA) * z(1 - 1 / n_trials)
                                   + EULER_GAMMA * z(1 - 1 / (n_trials * math.e)))
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0 or n_obs < 2:
        return math.nan
    return NormalDist().cdf((sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom))


def benchmarks(cfg: dict, bars: dict, start, end, universe: list[str] | None = None) -> dict:
    """Buy & hold BTC, dan equal-weight semua simbol (rebalance harian, tanpa biaya)."""
    s, e = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC") if end else None
    closes = pd.DataFrame({asset(sym): bars[sym]["close"] for sym in (universe or cfg["research"]["universe"])})
    if "BTC" not in closes:
        closes["BTC"] = bars["BTC/USDT:USDT"]["close"]
    closes = closes[(closes.index >= s) & ((closes.index <= e) if e is not None else True)]
    rets = closes.pct_change()
    ew = (1 + rets.mean(axis=1, skipna=True).fillna(0)).cumprod()
    btc = closes["BTC"] / closes["BTC"].iloc[0]
    out = {}
    for name, curve in (("buyhold_BTC", btc), ("equal_weight_universe", ew)):
        m = metrics.equity_metrics(curve * 10_000, 10_000)
        out[name] = {k: m[k] for k in ("total_return", "cagr", "max_drawdown", "sharpe")}
    return out


# ---------------------------------------------------------------- log riset

def append_log(rows: list[dict]) -> int:
    """Tambahkan percobaan ke log. Baris yang sudah ada (kunci kandidat+varian+periode sama) tidak diulang."""
    LAB.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame(rows)
    if LOG.exists():
        old = pd.read_csv(LOG)
        key = ["candidate_hash", "candidate", "variant", "period"]
        seen = set(map(tuple, old[key].astype(str).to_numpy()))
        new = new[[tuple(map(str, r)) not in seen for r in new[key].to_numpy()]]
        if new.empty:
            return 0
        pd.concat([old, new], ignore_index=True).to_csv(LOG, index=False)
    else:
        new.to_csv(LOG, index=False)
    return len(new)


def log_row(h: str, cand_name: str, cand: dict, variant: str, period: str, res: BacktestResult) -> dict:
    m = metrics.summarize(res)
    return {"logged_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "candidate_hash": h,
            "candidate": cand_name, "strategy": cand["strategy"], "mode": cand["mode"], "variant": variant,
            "period": period, "total_return": m["total_return"], "profit_factor": m.get("profit_factor", math.nan),
            "max_drawdown": m["max_drawdown"], "n_trades": m["n_trades"],
            **return_stats(res.equity["equity"], res.config.initial_equity)}


# ---------------------------------------------------------------- seleksi

def _read(name: str) -> pd.DataFrame:
    path = LAB / name
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def _merge_save(name: str, new: pd.DataFrame, done: list[str]) -> pd.DataFrame:
    """Ganti baris kandidat yang baru diseleksi, pertahankan baris kandidat lain."""
    old = _read(name)
    if not old.empty:
        new = pd.concat([old[~old.candidate.isin(done)], new], ignore_index=True)
    new.to_csv(LAB / name, index=False)
    return new


def screen(cfg: dict, bars: dict, funding: dict, names: list[str] | None = None) -> pd.DataFrame:
    """Seleksi kandidat. Default: hanya kandidat yang belum pernah diseleksi dengan kunci yang sama,
    sehingga kandidat lama tidak dijalankan & dicatat ulang. `names` memaksa kandidat tertentu."""
    r = cfg["research"]
    crit = r["criteria"]["selection"]
    start, end = r["periods"]["selection"]
    windows = rolling_windows(start, end, int(crit["rolling_window_months"]))
    rows, logs, sens_rows, win_rows, sym_rows = [], [], [], [], []
    cands = expand_candidates(cfg)
    prev = _read("screen.csv")
    done_hashes = set(prev.candidate_hash) if "candidate_hash" in prev else set()
    todo = names or [k for k in cands if candidate_hash(cfg, k) not in done_hashes]
    for k in todo:
        if k not in cands:
            raise SystemExit(f"[lab] kandidat tidak dikenal: {k}")
    if not todo:
        print("[lab] semua kandidat sudah diseleksi dengan setting yang sama; tidak ada yang dijalankan.")

    for name in todo:
        cand = cands[name]
        h = candidate_hash(cfg, name)
        print(f"[lab] {name} ...", flush=True)
        port = run_candidate(cfg, bars, funding, name, cand, cand["params"], start, end)
        m = metrics.summarize(port)
        logs.append(log_row(h, name, cand, "default", "selection", port))

        sym_ret = {}
        for sym in r["universe"]:
            single = run_candidate(cfg, bars, funding, name, cand, cand["params"], start, end, [sym])
            sm = metrics.summarize(single)
            sym_ret[asset(sym)] = sm["total_return"]
            sym_rows.append({"candidate": name, "symbol": asset(sym), "total_return": sm["total_return"],
                             "profit_factor": sm.get("profit_factor", math.nan), "n_trades": sm["n_trades"]})

        losing = []
        for label, params in variants(cand):
            if label == "default":
                continue
            v = run_candidate(cfg, bars, funding, name, cand, params, start, end)
            vm = metrics.summarize(v)
            logs.append(log_row(h, name, cand, label, "selection", v))
            sens_rows.append({"candidate": name, "variant": label, "total_return": vm["total_return"],
                              "profit_factor": vm.get("profit_factor", math.nan), "n_trades": vm["n_trades"]})
            if vm["total_return"] <= 0:
                losing.append(label)

        win_ret = []
        for ws, we in windows:
            w = run_candidate(cfg, bars, funding, name, cand, cand["params"], ws, we)
            wr = metrics.equity_metrics(w.equity["equity"], w.config.initial_equity)["total_return"]
            win_ret.append(wr)
            win_rows.append({"candidate": name, "window_start": ws.date(), "window_end": we.date(),
                             "total_return": wr, "n_trades": len(w.trades)})

        # Informasi konsentrasi (bukan kriteria): seberapa bergantung hasil pada sedikit trade.
        pnl = port.trades.net_pnl if len(port.trades) else pd.Series(dtype=float)
        top5_share = pnl.nlargest(5).sum() / pnl.sum() if pnl.sum() > 0 else math.nan
        best_sym = max(sym_ret, key=sym_ret.get)
        ex_best = port.trades[~port.trades.sleeve.str.endswith(":" + best_sym)].net_pnl if len(pnl) else pnl
        pf_ex_best = ex_best[ex_best > 0].sum() / -ex_best[ex_best < 0].sum() if (ex_best < 0).any() else math.nan

        prof_syms = sum(v > 0 for v in sym_ret.values()) / len(sym_ret)
        prof_wins = sum(v > 0 for v in win_ret) / len(win_ret)
        checks = {
            "pass_profit_factor": m.get("profit_factor", 0) > crit["profit_factor_min"],
            "pass_trades": m["n_trades"] >= crit["min_trades"],
            "pass_symbols": prof_syms >= crit["profitable_symbols_frac_min"],
            "pass_sensitivity": not losing if crit["sensitivity_no_loss"] else True,
            "pass_rolling": prof_wins > crit["rolling_profitable_frac_min"],
        }
        rows.append({
            "candidate": name, "candidate_hash": h, "strategy": cand["strategy"], "mode": cand["mode"],
            "total_return": m["total_return"], "cagr": m["cagr"], "max_drawdown": m["max_drawdown"],
            "sharpe": m["sharpe"], "profit_factor": m.get("profit_factor", math.nan), "n_trades": m["n_trades"],
            "winrate": m.get("winrate", math.nan), "expectancy_r": m.get("expectancy_r", math.nan),
            "total_funding": m.get("total_funding", 0.0), "total_fees": m.get("total_fees", 0.0),
            "profitable_symbols": f"{sum(v > 0 for v in sym_ret.values())}/{len(sym_ret)}",
            "losing_variants": ", ".join(losing) or "-",
            "profitable_windows": f"{sum(v > 0 for v in win_ret)}/{len(win_ret)}",
            "top5_trades_share": top5_share, "best_symbol": best_sym, "pf_ex_best_symbol": pf_ex_best,
            **checks, "pass_all": all(checks.values()),
        })

    n_new = append_log(logs) if logs else 0
    LAB.mkdir(parents=True, exist_ok=True)
    table = _merge_save("screen.csv", pd.DataFrame(rows), todo) if rows else prev
    _merge_save("screen_sensitivity.csv", pd.DataFrame(sens_rows), todo) if sens_rows else None
    _merge_save("screen_windows.csv", pd.DataFrame(win_rows), todo) if win_rows else None
    _merge_save("screen_symbols.csv", pd.DataFrame(sym_rows), todo) if sym_rows else None

    # Deflated Sharpe untuk SEMUA kandidat, dihitung ulang dengan jumlah percobaan terbaru:
    # tiap kandidat baru menaikkan standar untuk semua (termasuk yang lama).
    log = pd.read_csv(LOG)
    trials = log[log.period == "selection"]
    var_sr = trials.sharpe_daily.var()
    dsr = []
    for name, h in zip(table.candidate, table.candidate_hash):
        d = trials[(trials.candidate == name) & (trials.variant == "default") & (trials.candidate_hash == h)].iloc[-1]
        dsr.append(deflated_sharpe(d["sharpe_daily"], len(trials), var_sr, int(d["n_obs"]), d["skew"], d["kurt"]))
    table["deflated_sharpe_prob"] = dsr
    table.to_csv(LAB / "screen.csv", index=False)

    meta = {"run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "screened_now": todo,
            "period": [start, end], "n_trials_total": len(trials), "n_trials_new": n_new,
            "benchmarks": benchmarks(cfg, bars, start, end)}
    (LAB / "screen_meta.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")
    return table[table.candidate.isin(todo)] if todo else table.iloc[0:0]


# ---------------------------------------------------------------- holdout

def holdout(cfg: dict, bars: dict, funding: dict, names: list[str], force: bool = False) -> pd.DataFrame:
    r = cfg["research"]
    crit = r["criteria"]["holdout"]
    cands = expand_candidates(cfg)
    screen_tbl = _read("screen.csv")
    lock = json.loads(HOLDOUT_LOCK.read_text()) if HOLDOUT_LOCK.exists() else {}
    start = r["periods"]["holdout_start"]
    rows, logs = [], []
    for name in names:
        if name not in cands:
            raise SystemExit(f"[lab] kandidat tidak dikenal: {name}")
        h = candidate_hash(cfg, name)
        row = screen_tbl[(screen_tbl.candidate == name) & (screen_tbl.candidate_hash == h)] if len(screen_tbl) else screen_tbl
        if row.empty:
            raise SystemExit(f"[lab] {name} belum diseleksi dengan setting sekarang; jalankan `screen` dulu.")
        passed = bool(row["pass_all"].iloc[0])
        if not passed and not force:
            raise SystemExit(f"[lab] {name} tidak lolos seleksi; holdout hanya untuk yang lolos (atau --force, dicatat).")
        if name in lock and lock[name].get("candidate_hash") != h:
            raise SystemExit(f"[lab] holdout {name} sudah pernah dijalankan dengan setting lain "
                             f"({lock[name]['run_at']}); hasil baru tidak lagi murni.")
        cand = cands[name]
        res = run_candidate(cfg, bars, funding, name, cand, cand["params"], start, None)
        m = metrics.summarize(res)
        logs.append(log_row(h, name, cand, "default" + (" (forced)" if not passed else ""), "holdout", res))
        lock[name] = {"candidate_hash": h, "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                      "forced": not passed}
        rows.append({"candidate": name, "total_return": m["total_return"], "cagr": m["cagr"],
                     "max_drawdown": m["max_drawdown"], "sharpe": m["sharpe"],
                     "profit_factor": m.get("profit_factor", math.nan), "n_trades": m["n_trades"],
                     "pass_profit_factor": m.get("profit_factor", 0) > crit["profit_factor_min"],
                     "pass_drawdown": m["max_drawdown"] < crit["max_drawdown_max"]})
        res.trades.to_csv(LAB / f"holdout_trades_{name}.csv", index=False)
        res.equity.to_csv(LAB / f"holdout_equity_{name}.csv")
    append_log(logs)
    HOLDOUT_LOCK.write_text(json.dumps(lock, indent=2) + "\n")
    out = pd.DataFrame(rows)
    out["pass_all"] = out.pass_profit_factor & out.pass_drawdown
    path = LAB / "holdout.csv"
    if path.exists():
        prev = pd.read_csv(path)
        out = pd.concat([prev[~prev.candidate.isin(out.candidate)], out], ignore_index=True)
    out.to_csv(path, index=False)
    bm = benchmarks(cfg, bars, start, None)
    (LAB / "holdout_benchmarks.json").write_text(json.dumps(bm, indent=2) + "\n")
    return out[out.candidate.isin(names)]


# ---------------------------------------------------------------- ujian koin segar

def fresh_holdout(cfg: dict, bars: dict, funding: dict, names: list[str]) -> pd.DataFrame:
    """Uji kandidat SEKALI di 10 koin yang belum pernah dipakai riset, periode penuh.
    Hanya untuk kandidat yang lolos seleksi DAN holdout waktu."""
    fh = cfg["research"]["fresh_holdout"]
    crit, uni = fh["criteria"], fh["universe"]
    cands = expand_candidates(cfg)
    holdout_tbl = _read("holdout.csv")
    hold_lock = json.loads(HOLDOUT_LOCK.read_text()) if HOLDOUT_LOCK.exists() else {}
    lock = json.loads(FRESH_LOCK.read_text()) if FRESH_LOCK.exists() else {}
    start = cfg["research"]["periods"]["selection"][0]
    rows, logs, sym_rows = [], [], []
    for name in names:
        if name not in cands:
            raise SystemExit(f"[lab] kandidat tidak dikenal: {name}")
        h = candidate_hash(cfg, name)
        passed_holdout = (name in hold_lock and hold_lock[name].get("candidate_hash") == h and
                          bool(holdout_tbl.loc[holdout_tbl.candidate == name, "pass_all"].iloc[0]))
        if not passed_holdout:
            raise SystemExit(f"[lab] {name} belum lolos holdout waktu dengan setting sekarang; ujian segar ditolak.")
        fh_hash = _hash({"candidate_hash": h, "fresh_holdout": fh})
        if name in lock and lock[name]["fresh_hash"] != fh_hash:
            raise SystemExit(f"[lab] ujian segar {name} sudah pernah dijalankan dengan setting lain; tidak murni lagi.")
        cand = cands[name]
        res = run_candidate(cfg, bars, funding, name, cand, cand["params"], start, None, uni, uni)
        m = metrics.summarize(res)
        prof = 0
        for sym in uni:
            single = run_candidate(cfg, bars, funding, name, cand, cand["params"], start, None, [sym], uni)
            sm = metrics.summarize(single)
            prof += sm["total_return"] > 0
            sym_rows.append({"candidate": name, "symbol": asset(sym), "total_return": sm["total_return"],
                             "profit_factor": sm.get("profit_factor", math.nan), "n_trades": sm["n_trades"]})
        logs.append(log_row(h, name, cand, "default", "fresh_holdout", res))
        lock[name] = {"candidate_hash": h, "fresh_hash": fh_hash,
                      "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
        rows.append({"candidate": name, "total_return": m["total_return"], "cagr": m["cagr"],
                     "max_drawdown": m["max_drawdown"], "sharpe": m["sharpe"],
                     "profit_factor": m.get("profit_factor", math.nan), "n_trades": m["n_trades"],
                     "profitable_symbols": f"{prof}/{len(uni)}",
                     "pass_profit_factor": m.get("profit_factor", 0) > crit["profit_factor_min"],
                     "pass_drawdown": m["max_drawdown"] < crit["max_drawdown_max"],
                     "pass_symbols": prof / len(uni) >= crit["profitable_symbols_frac_min"]})
        res.trades.to_csv(LAB / f"fresh_trades_{name}.csv", index=False)
    append_log(logs)
    FRESH_LOCK.write_text(json.dumps(lock, indent=2) + "\n")
    out = pd.DataFrame(rows)
    out["pass_all"] = out.pass_profit_factor & out.pass_drawdown & out.pass_symbols
    _merge_save("fresh_holdout.csv", out, names)
    _merge_save("fresh_holdout_symbols.csv", pd.DataFrame(sym_rows), names)
    (LAB / "fresh_holdout_benchmarks.json").write_text(
        json.dumps(benchmarks(cfg, bars, start, None, uni), indent=2) + "\n")
    return out


# ---------------------------------------------------------------- CLI

def _market(cfg: dict) -> tuple[dict, dict]:
    d = cfg["data"]
    r = cfg["research"]
    u = list(dict.fromkeys(r["universe"] + r.get("fresh_holdout", {}).get("universe", []) + ["BTC/USDT:USDT"]))
    return {s: load_bars(s, d) for s in u}, {s: load_funding(s, d) for s in u}


def _print(df: pd.DataFrame, pct=("total_return", "cagr", "max_drawdown", "winrate")) -> None:
    show = df.copy()
    for c in show.columns:
        if c in pct:
            show[c] = show[c].map(lambda v: f"{v:+.1%}" if c in ("total_return", "cagr") else f"{v:.1%}")
        elif c.startswith("pass"):
            show[c] = show[c].map({True: "✅", False: "❌"})
        elif show[c].dtype.kind == "f":
            show[c] = show[c].map(lambda v: f"{v:.2f}")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    print(show.to_string(index=False))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("screen")
    sc.add_argument("names", nargs="*", help="kandidat tertentu (default: yang belum diseleksi)")
    ho = sub.add_parser("holdout")
    ho.add_argument("names", nargs="+")
    ho.add_argument("--force", action="store_true", help="holdout kandidat yang tidak lolos seleksi (dicatat)")
    fr = sub.add_parser("fresh", help="ujian sekali di 10 koin segar (setelah lolos holdout)")
    fr.add_argument("names", nargs="+")
    args = ap.parse_args()

    cfg = load_config()
    bars, funding = _market(cfg)
    if args.cmd == "screen":
        t = screen(cfg, bars, funding, args.names or None)
        if t.empty:
            return
        meta = json.loads((LAB / "screen_meta.json").read_text())
        s, e = meta["period"]
        print(f"\n=== Seleksi {s} s/d {e}, {len(cfg['research']['universe'])} simbol, setelah biaya ===")
        _print(t[["candidate", "total_return", "cagr", "max_drawdown", "sharpe", "profit_factor", "n_trades",
                  "profitable_symbols", "profitable_windows", "deflated_sharpe_prob"]])
        print("\nKonsentrasi (informasi, bukan kriteria):")
        _print(t[["candidate", "top5_trades_share", "best_symbol", "pf_ex_best_symbol"]])
        print("\nKriteria:")
        _print(t[["candidate", "pass_profit_factor", "pass_trades", "pass_symbols", "pass_sensitivity",
                  "pass_rolling", "pass_all", "losing_variants"]])
        print(f"\nBenchmark periode yang sama: {json.dumps(meta['benchmarks'], indent=1)}")
        print(f"\nPercobaan tercatat di log (periode selection): {meta['n_trials_total']} "
              f"(+{meta['n_trials_new']} baru). Hasil di {LAB.relative_to(ROOT)}/")
    elif args.cmd == "holdout":
        _print(holdout(cfg, bars, funding, args.names, args.force))
    else:
        _print(fresh_holdout(cfg, bars, funding, args.names))


if __name__ == "__main__":
    main()
