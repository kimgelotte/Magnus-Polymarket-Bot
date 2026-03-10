import math


class RiskManager:
    """
    Simple risk module: computes Kelly bet given edge.

    `calculate_kelly_bet` is used in `Trade.run_sniper_loop` to compute
    how large the USDC bet should be, given:
      - ai_max_price (fair value from Quant)
      - current_price (market price)
      - balance (available USDC)
      - kelly_fraction (share of full Kelly we dare use)
    """

    def calculate_kelly_bet(
        self,
        fair_value: float,
        market_price: float,
        bankroll: float,
        kelly_fraction: float = 0.25,
    ) -> float:
        """
        Returns recommended bet size in USDC.

        fair_value and market_price are decimals 0–1 (e.g. 0.23 = 23¢).
        """
        try:
            v = float(fair_value)
            p = float(market_price)
            bankroll = float(bankroll)
            kelly_fraction = float(kelly_fraction)
        except (TypeError, ValueError):
            return 0.0

        if not (0 < p < 1) or not (0 < v < 1) or bankroll <= 0 or kelly_fraction <= 0:
            return 0.0
        if v <= p:
            return 0.0

        # Kelly for binary payoff ~1 on "win", 0 on "loss"
        # Interpret fair value as subjective probability.
        b = (1.0 - p) / p
        prob = v
        q = 1.0 - prob
        k_full = (b * prob - q) / b
        if k_full <= 0:
            return 0.0

        f = min(max(k_full * kelly_fraction, 0.0), 1.0)
        return round(bankroll * f, 2)

