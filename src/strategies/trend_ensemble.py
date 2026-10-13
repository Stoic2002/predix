"""E1 — Trend ensemble: gabungan beberapa model breakout Donchian dengan panjang channel berbeda.

Model ke-i memakai channel N_i = base_lookback × 2^i (default 20, 40, 80, 160 hari). Tiap model punya
"suara": +1 setelah close menembus high N hari sebelumnya, -1 setelah menembus low N hari (kalau
allow_short), kembali 0 saat close menembus channel lawan N/2 hari. Skor = rata-rata suara.
Masuk long saat skor >= entry_score, short saat skor <= -entry_score; keluar saat skor kembali ke 0.
Mengurangi ketergantungan pada satu angka channel (Robot 1 hanya memakai 55/20).
Ukuran posisi berbasis volatilitas sudah dilakukan engine (risiko ÷ atr_mult × ATR).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import indicators as ind


def _model_state(h: pd.Series, l: pd.Series, c: pd.Series, n: int, allow_short: bool) -> np.ndarray:
    up_n, lo_n = (x.to_numpy() for x in ind.donchian(h, l, n))
    up_m, lo_m = (x.to_numpy() for x in ind.donchian(h, l, max(2, n // 2)))
    close = c.to_numpy()
    out = np.zeros(len(close))
    s = 0
    for i in range(len(close)):
        if np.isnan(up_n[i]):          # channel belum panas: model belum bersuara
            continue
        if close[i] > up_n[i]:
            s = 1
        elif allow_short and close[i] < lo_n[i]:
            s = -1
        elif s == 1 and close[i] < lo_m[i]:
            s = 0
        elif s == -1 and close[i] > up_m[i]:
            s = 0
        out[i] = s
    return out


def generate_signals(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    h, l, c = bars["high"], bars["low"], bars["close"]
    allow_short = bool(p["allow_short"])
    lookbacks = [int(p["base_lookback"]) * 2 ** i for i in range(int(p["n_models"]))]
    score = pd.Series(np.mean([_model_state(h, l, c, n, allow_short) for n in lookbacks], axis=0), index=bars.index)
    a = ind.atr(h, l, c, int(p["atr_period"]))
    th = float(p["entry_score"])
    return pd.DataFrame({
        "entry": np.where(score >= th, 1, np.where(allow_short & (score <= -th), -1, 0)),
        "exit_long": score <= 0,
        "exit_short": score >= 0,
        "stop_dist": p["atr_mult"] * a,
    }, index=bars.index)
