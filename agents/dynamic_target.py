"""
Magnus V4 Dynamic Profit Target Calculator.

Computes adaptive sell target based on:
- Time remaining until market resolution
- Historical price volatility (range %)
- Hype score from Scout
- Bid/ask spread
"""


def compute_dynamic_target(
    fill_price: float,
    days_until_end: float | None,
    range_pct: float,
    hype_score: int,
    spread_pct: float | None,
    ai_max_price: float | None,
    base_target_pct: float = 0.07,
    high_target_pct: float = 0.10,
    price_high_threshold: float = 0.30,
) -> float:
    """
    Returns a target sell price, clamped to [0.01, 0.99].

    Logic:
    - Start from base target (7%) or high target (10%) for cheap fills.
    - Increase target when: high volatility, high hype, lots of time.
    - Decrease target when: low volatility, high spread, little time.
    - Cap at AI max_price if available.
    """
    if fill_price < price_high_threshold:
        pct = high_target_pct
    else:
        pct = base_target_pct

    # Time factor: more time = can afford to hold for bigger move
    if days_until_end is not None:
        if days_until_end > 14:
            pct *= 1.3
        elif days_until_end > 7:
            pct *= 1.15
        elif days_until_end < 2:
            pct *= 0.7
        elif days_until_end < 1:
            pct *= 0.5

    # Volatility factor: high range = expect bigger swings
    if range_pct > 30:
        pct *= 1.2
    elif range_pct > 20:
        pct *= 1.1
    elif range_pct < 10:
        pct *= 0.8

    # Hype factor: high hype = more likely to move up
    if hype_score >= 8:
        pct *= 1.15
    elif hype_score <= 3:
        pct *= 0.85

    # Spread factor: wide spread = harder to exit profitably
    if spread_pct is not None:
        if spread_pct > 10:
            pct *= 0.8
        elif spread_pct > 6:
            pct *= 0.9

    target = round(fill_price * (1 + pct), 3)

    if ai_max_price and ai_max_price >= 0.01:
        target = min(target, round(ai_max_price, 3))

    return round(max(0.01, min(0.99, target)), 3)
