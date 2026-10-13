"""Position sizing berbasis risiko, batas leverage, dan cek likuidasi (margin isolated)."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SizeResult:
    qty: float = 0.0
    notional: float = 0.0
    risk_amount: float = 0.0       # qty × jarak stop, sebelum biaya
    capped_by: str | None = None   # "leverage" / "total_notional" kalau ukuran dipotong
    rejected: str | None = None    # alasan penolakan; None kalau trade boleh
    liq_price: float = math.nan


def liquidation_price(entry: float, direction: int, leverage: float, mmr: float) -> float:
    """Perkiraan harga likuidasi margin isolated untuk kontrak linear (USDT-M).
    Margin awal = notional / leverage; likuidasi saat sisa margin = maintenance margin."""
    if direction > 0:
        return entry * (1 - 1 / leverage + mmr)
    return entry * (1 + 1 / leverage - mmr)


def size_position(
    *,
    equity: float,
    entry_price: float,
    stop_dist: float,
    direction: int,
    risk_frac: float,
    max_leverage: float,
    max_total_notional: float,
    open_notional: float,
    isolated_leverage: float,
    maintenance_margin_rate: float,
    liq_buffer: float,
) -> SizeResult:
    """Ukuran = (risk_frac × ekuitas) ÷ jarak stop, lalu dipotong ke batas leverage per posisi
    dan ke sisa ruang batas total notional. Ditolak kalau likuidasi terlalu dekat ke stop."""
    if equity <= 0:
        return SizeResult(rejected="ekuitas <= 0")
    if not (math.isfinite(stop_dist) and stop_dist > 0):
        return SizeResult(rejected=f"jarak stop tidak valid ({stop_dist})")

    liq = liquidation_price(entry_price, direction, isolated_leverage, maintenance_margin_rate)
    liq_dist = abs(entry_price - liq)
    if liq_dist < liq_buffer * stop_dist:
        return SizeResult(
            liq_price=liq,
            rejected=(f"likuidasi terlalu dekat: jarak likuidasi {liq_dist:.2f} "
                      f"< {liq_buffer} × jarak stop {stop_dist:.2f}"),
        )

    room = max_total_notional * equity - open_notional
    if room <= 0:
        return SizeResult(liq_price=liq, rejected="batas total notional sudah penuh")

    notional = risk_frac * equity / stop_dist * entry_price
    capped_by = None
    if notional > max_leverage * equity:
        notional, capped_by = max_leverage * equity, "leverage"
    if notional > room:
        notional, capped_by = room, "total_notional"

    qty = notional / entry_price
    return SizeResult(qty=qty, notional=notional, risk_amount=qty * stop_dist,
                      capped_by=capped_by, liq_price=liq)
