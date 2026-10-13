"""Metrik performa dari kurva ekuitas dan daftar trade."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from src.backtest import BacktestResult

DAYS_PER_YEAR = 365


def drawdown(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1


def longest_drawdown_days(equity: pd.Series) -> int:
    """Durasi terlama dari puncak sampai pulih (atau sampai akhir data), dalam hari."""
    peak_time = equity.index[0]
    longest = pd.Timedelta(0)
    running_max = -math.inf
    for t, v in equity.items():
        if v >= running_max:
            running_max, peak_time = v, t
        longest = max(longest, t - peak_time)
    return longest.days


def longest_losing_streak(net_pnl: pd.Series) -> int:
    best = cur = 0
    for x in net_pnl:
        cur = cur + 1 if x < 0 else 0
        best = max(best, cur)
    return best


def equity_metrics(equity: pd.Series, initial: float, marks=(0.20, 0.30)) -> dict:
    """equity: nilai close tiap bar. `initial` dipakai sebagai titik awal sebelum bar pertama."""
    start = equity.index[0] - pd.Timedelta(days=1)
    eq = pd.concat([pd.Series([initial], index=[start]), equity])
    years = (eq.index[-1] - eq.index[0]).days / DAYS_PER_YEAR
    total = eq.iloc[-1] / initial - 1
    cagr = (eq.iloc[-1] / initial) ** (1 / years) - 1 if years > 0 and eq.iloc[-1] > 0 else math.nan
    dd = drawdown(eq)
    max_dd = -dd.min()
    rets = eq.pct_change().dropna()
    sharpe = rets.mean() / rets.std() * math.sqrt(DAYS_PER_YEAR) if rets.std() > 0 else math.nan
    out = {
        "total_return": total,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "longest_dd_days": longest_drawdown_days(eq),
        "calmar": cagr / max_dd if max_dd > 0 else math.nan,
        "sharpe": sharpe,
    }
    for m in marks:
        hit = dd[dd <= -m]
        out[f"first_dd_{int(m * 100)}pct"] = hit.index[0].strftime("%Y-%m-%d") if len(hit) else "-"
    return out


def trade_metrics(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"n_trades": 0}
    t = trades.sort_values("exit_time")
    pnl = t["net_pnl"]
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    return {
        "n_trades": len(t),
        "winrate": len(wins) / len(t),
        "profit_factor": wins.sum() / -losses.sum() if len(losses) else math.inf,
        "expectancy_r": t["r_multiple"].mean(),
        "avg_win": wins.mean() if len(wins) else math.nan,
        "avg_loss": losses.mean() if len(losses) else math.nan,
        "avg_win_r": t.loc[pnl > 0, "r_multiple"].mean() if len(wins) else math.nan,
        "avg_loss_r": t.loc[pnl < 0, "r_multiple"].mean() if len(losses) else math.nan,
        "avg_bars_held": t["bars_held"].mean(),
        "total_fees": t["fees"].sum(),
        "total_funding": t["funding"].sum(),
        "total_slippage": t["slippage"].sum(),
        "longest_losing_streak": longest_losing_streak(pnl),
        "n_long": int((t["side"] == "long").sum()),
        "n_short": int((t["side"] == "short").sum()),
        "pnl_long": t.loc[t["side"] == "long", "net_pnl"].sum(),
        "pnl_short": t.loc[t["side"] == "short", "net_pnl"].sum(),
    }


def summarize(res: BacktestResult, marks=(0.20, 0.30)) -> dict:
    out = equity_metrics(res.equity["equity"], res.config.initial_equity, marks)
    out["time_in_market"] = (res.equity["n_open"] > 0).mean()
    out.update(trade_metrics(res.trades))
    out["n_rejected"] = len(res.rejections)
    return out


def daily_pnl_by_group(res: BacktestResult, groups: dict[str, list[str]]) -> pd.DataFrame:
    """PnL harian per kelompok sleeve (mis. {'s1': [...], 's2': [...]}) untuk korelasi."""
    daily = res.sleeve_pnl.diff()
    daily.iloc[0] = res.sleeve_pnl.iloc[0]
    return pd.DataFrame({g: daily[cols].sum(axis=1) for g, cols in groups.items()})


def buy_and_hold(bars: pd.DataFrame, initial: float, start=None, end=None) -> pd.Series:
    """Benchmark: beli di open bar pertama periode, tahan tanpa leverage, tanpa biaya/funding."""
    b = bars
    if start is not None:
        b = b[b.index >= pd.Timestamp(start, tz="UTC")]
    if end is not None:
        b = b[b.index <= pd.Timestamp(end, tz="UTC")]
    return initial * b["close"] / b["open"].iloc[0]


def fmt_table(rows: dict[str, dict]) -> pd.DataFrame:
    """Gabung beberapa dict metrik jadi tabel (baris = metrik, kolom = run)."""
    df = pd.DataFrame(rows)
    pct = ["total_return", "cagr", "max_drawdown", "time_in_market", "winrate"]
    out = df.astype(object).copy()
    for idx in df.index:
        for col in df.columns:
            v = df.at[idx, col]
            if isinstance(v, (float, np.floating)) and not math.isnan(v):
                out.at[idx, col] = f"{v:.1%}" if idx in pct else f"{v:,.2f}"
            elif v is None or (isinstance(v, float) and math.isnan(v)):
                out.at[idx, col] = "-"
    return out
