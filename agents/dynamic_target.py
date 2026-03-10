"""
Dynamic target pricing for Magnus.

Computes which GTC sell price to place given:
- fill_price
- days_until_end
- range_pct (historical volatility)
- hype_score (Scout output)
- spread_pct
- ai_max_price (Quant max)
"""

from __future__ import annotations

from typing import Optional


def _safe_float(x: Optional[float], default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def compute_dynamic_target(
    *,
    fill_price: float,
    days_until_end: Optional[float],
    range_pct: float,
    hype_score: int,
    spread_pct: Optional[float],
    ai_max_price: float,
    base_target_pct: float,
    high_target_pct: float,
    price_high_threshold: float,
) -> float:
    """
    Returns target price (decimal 0–1) for GTC sell order.
    """
    fill_price = _safe_float(fill_price)
    ai_max_price = _safe_float(ai_max_price, default=0.0)
    range_pct = _safe_float(range_pct)
    spread_pct_f = _safe_float(spread_pct, default=0.0) if spread_pct is not None else None
    hype = int(hype_score or 0)

    if fill_price <= 0:
        return 0.0

    target_pct = base_target_pct

    # Cheap entries → use higher base target.
    if fill_price < price_high_threshold:
        target_pct = high_target_pct

    # Volatility: larger range → slightly higher target, low range → lower.
    if range_pct > 30:
        target_pct += 0.03
    elif range_pct > 20:
        target_pct += 0.02
    elif range_pct < 10:
        target_pct -= 0.02

    # Hype: starkt case → pressa upp target lite.
    if hype >= 8:
        target_pct += 0.02
    elif hype <= 3:
        target_pct -= 0.02

    # Time left: very little time → be more aggressive (lower target).
    if days_until_end is not None:
        d = _safe_float(days_until_end)
        if d < 1.0:
            target_pct -= 0.02
        elif d > 7.0:
            target_pct += 0.01

    # Spread: high spread → requires higher price to compensate friction.
    if spread_pct_f is not None:
        if spread_pct_f > 15:
            target_pct += 0.02
        elif spread_pct_f < 5:
            target_pct -= 0.005

    # Set floor/ceiling on target percent.
    target_pct = max(0.03, min(target_pct, 0.40))

    target_price = fill_price * (1.0 + target_pct)

    # Hard cap against Quant max if set (>0).
    if ai_max_price > 0:
        target_price = min(target_price, ai_max_price)

    # Ensure target > fill with margin.
    if target_price <= fill_price * 1.02:
        target_price = fill_price * 1.02

    return round(target_price, 3)

