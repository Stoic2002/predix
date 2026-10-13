"""Model biaya: slippage, fee taker, funding.

Konvensi arah: +1 = beli/long, -1 = jual/short.
"""
from __future__ import annotations


def apply_slippage(price: float, side: int, slippage: float) -> float:
    """Harga fill setelah slippage, selalu merugikan: beli lebih mahal, jual lebih murah."""
    return price * (1 + side * slippage)


def fee(notional: float, rate: float) -> float:
    return abs(notional) * rate


def funding_payment(direction: int, qty: float, price: float, rate: float) -> float:
    """Funding untuk satu event. Positif = posisi membayar, negatif = menerima.
    Long membayar saat rate positif, short menerima (dan sebaliknya)."""
    return direction * qty * price * rate
