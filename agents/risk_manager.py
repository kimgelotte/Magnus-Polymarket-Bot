import math


class RiskManager:
    """
    Simpel risk‑modul: beräknar Kelly‑insats givet edge.

    `calculate_kelly_bet` används i `Trade.run_sniper_loop` för att räkna ut
    hur stor USDC‑beten ska vara, givet:
      - ai_max_price (fair value från Quant)
      - current_price (marknadspris)
      - balance (tillgänglig USDC)
      - kelly_fraction (andel av full Kelly vi vågar använda)
    """

    def calculate_kelly_bet(
        self,
        fair_value: float,
        market_price: float,
        bankroll: float,
        kelly_fraction: float = 0.25,
    ) -> float:
        """
        Returnerar rekommenderad bet‑storlek i USDC.

        fair_value och market_price är decimaler 0–1 (t.ex. 0.23 = 23¢).
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

        # Kelly för binär payoff ~1 vid "vinst", 0 vid "förlust"
        # Tolka fair value som subjektiv sannolikhet.
        b = (1.0 - p) / p
        prob = v
        q = 1.0 - prob
        k_full = (b * prob - q) / b
        if k_full <= 0:
            return 0.0

        f = min(max(k_full * kelly_fraction, 0.0), 1.0)
        return round(bankroll * f, 2)

