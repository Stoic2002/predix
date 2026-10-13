"""Daftar semua strategi yang bisa dipakai lab riset dan robot paper (nama -> generate_signals).

Dua jenis khusus:
  - MULTI: fungsi menerima dict {simbol: bars} dan mengembalikan dict sinyal (strategi lintas koin).
  - NEEDS_FUNDING: bars diberi kolom `funding` (jumlah funding rate per hari UTC) sebelum dipanggil.
"""
from __future__ import annotations

import pandas as pd

from src.strategies import (chandelier, funding_contra, ma_cross, mean_reversion, trend, trend_ensemble,
                            tsmom, xsmom)

REGISTRY = {
    "rsi2": mean_reversion.generate_signals,   # S1 Fase 1
    "donchian": trend.generate_signals,        # S2 Fase 1
    "chandelier": chandelier.generate_signals,
    "tsmom": tsmom.generate_signals,
    "ma_cross": ma_cross.generate_signals,
    "xsmom": xsmom.generate_signals,
    "funding_contra": funding_contra.generate_signals,
    "trend_ensemble": trend_ensemble.generate_signals,
}
MULTI = {"xsmom"}
NEEDS_FUNDING = {"funding_contra"}


def daily_funding(f: pd.DataFrame) -> pd.Series:
    """Jumlah funding rate per hari UTC. Event 00:00 masuk ke hari yang baru dimulai (diketahui
    tepat saat candle sebelumnya close), jadi tidak ada lookahead."""
    return f["funding_rate"].groupby(f.index.floor("D")).sum()


def signals_for(strategy: str, params: dict, bars: dict, funding: dict, symbols: list[str],
                universe: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """Sinyal untuk `symbols`. Strategi lintas koin selalu dihitung di seluruh `universe`
    (peringkat butuh semua koin), lalu diambil bagian `symbols` saja."""
    fn = REGISTRY[strategy]
    if strategy in MULTI:
        allsig = fn({s: bars[s] for s in (universe or symbols)}, params)
        return {s: allsig[s] for s in symbols}
    out = {}
    for s in symbols:
        b = bars[s]
        if strategy in NEEDS_FUNDING:
            b = b.assign(funding=daily_funding(funding[s]).reindex(b.index).fillna(0.0))
        out[s] = fn(b, params)
    return out
