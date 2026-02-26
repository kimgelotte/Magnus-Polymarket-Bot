import math
import re

class RiskManager:
    @staticmethod
    def calculate_kelly_bet(win_probability: float, market_price: float, current_balance: float, kelly_fraction: float = 0.5) -> float:
        """Kelly Criterion bet sizing. Ensures min ~5 shares (Polymarket limit order minimum)."""
        if win_probability <= market_price: 
            return 0.0
            
        b = (1 - market_price) / market_price
        f_star = (win_probability - ((1 - win_probability) / b))
        
        if f_star > 0:
            bet_amount = current_balance * f_star * kelly_fraction
            
            min_usdc_required = 5.5 * market_price
            safe_min_bet = max(2.0, min_usdc_required)
            
            if bet_amount < safe_min_bet and current_balance >= safe_min_bet: 
                return round(safe_min_bet, 2)
            
            # Cap at 70% of balance per trade
            return round(min(bet_amount, current_balance * 0.7), 2)
            
        return 0.0

    @staticmethod
    def parse_old_ai_output(ai_output: str) -> float:
        """Parse probability from legacy agent text output."""
        match = re.search(r"(?:likelihood|probability|sannolikhet)[:\s]*`?([0-9]*\.[0-9]+)`?", ai_output, re.IGNORECASE)
        if match:
            return float(match.group(1))
        return 0.0