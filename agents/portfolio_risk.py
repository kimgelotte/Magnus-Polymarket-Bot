"""
Magnus V4 Portfolio-Level Risk Manager.

Provides drawdown tracking, correlation checks, and daily P&L monitoring
to complement per-trade risk controls.
"""

import os
import time
import json
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

BALANCE_LOG_PATH = os.getenv("MAGNUS_BALANCE_LOG", "data/balance_history.jsonl")
MAX_DRAWDOWN_PCT = float(os.getenv("MAGNUS_MAX_DRAWDOWN_PCT", "30"))
MAX_CORRELATED_POSITIONS = int(os.getenv("MAGNUS_MAX_CORRELATED", "3"))


class PortfolioRiskManager:

    def __init__(self, db_manager, polymarket):
        self.db = db_manager
        self.polymarket = polymarket
        self._peak_balance = 0.0
        self._last_balance_log = 0.0
        self._load_peak()

    def _load_peak(self):
        """Load peak balance from history file."""
        path = Path(BALANCE_LOG_PATH)
        if not path.exists():
            return
        try:
            with open(path, "r") as f:
                for line in f:
                    entry = json.loads(line.strip())
                    bal = entry.get("balance", 0)
                    if bal > self._peak_balance:
                        self._peak_balance = bal
        except Exception:
            pass

    def log_balance(self, balance: float):
        """Log balance periodically (max once per 5 minutes)."""
        now = time.time()
        if now - self._last_balance_log < 300:
            return
        self._last_balance_log = now
        if balance > self._peak_balance:
            self._peak_balance = balance

        path = Path(BALANCE_LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "balance": round(balance, 2),
            "peak": round(self._peak_balance, 2),
        }
        try:
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def check_drawdown(self, current_balance: float) -> tuple[bool, float]:
        """Returns (should_pause, drawdown_pct). Pause if drawdown > MAX_DRAWDOWN_PCT."""
        if self._peak_balance <= 0:
            self._peak_balance = current_balance
            return False, 0.0
        if current_balance > self._peak_balance:
            self._peak_balance = current_balance
        drawdown = ((self._peak_balance - current_balance) / self._peak_balance) * 100
        return drawdown >= MAX_DRAWDOWN_PCT, round(drawdown, 1)

    def check_correlation(self, new_event_title: str, new_category: str) -> bool:
        """Returns True if adding this position would create too many correlated positions."""
        positions = self.db.get_open_positions()
        if not positions:
            return False

        new_lower = (new_event_title or "").lower()
        new_cat = (new_category or "").strip()

        correlated_count = 0
        for p in positions:
            q = (p.get("question") or "").lower()
            cat = (p.get("category") or "").strip()

            if cat != new_cat:
                continue

            shared_keywords = 0
            new_words = set(w for w in new_lower.split() if len(w) > 3)
            existing_words = set(w for w in q.split() if len(w) > 3)
            shared_keywords = len(new_words & existing_words)

            if shared_keywords >= 2:
                correlated_count += 1

        return correlated_count >= MAX_CORRELATED_POSITIONS

    def get_daily_pnl(self) -> dict:
        """Calculate aggregate P&L for open positions."""
        positions = self.db.get_open_positions()
        total_invested = 0.0
        total_current = 0.0
        by_category = {}

        for p in positions:
            buy_p = float(p.get("buy_price") or 0)
            shares = float(p.get("shares_bought") or 0)
            cat = p.get("category") or "Unknown"
            invested = float(p.get("amount_usdc") or 0)
            total_invested += invested

            try:
                current_price = self.polymarket.get_buy_price(p["token_id"])
                current_value = current_price * shares if current_price else 0
            except Exception:
                current_value = invested

            total_current += current_value

            if cat not in by_category:
                by_category[cat] = {"invested": 0, "current": 0, "count": 0}
            by_category[cat]["invested"] += invested
            by_category[cat]["current"] += current_value
            by_category[cat]["count"] += 1

        pnl = total_current - total_invested
        pnl_pct = (pnl / total_invested * 100) if total_invested > 0 else 0

        return {
            "total_invested": round(total_invested, 2),
            "total_current": round(total_current, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 1),
            "position_count": len(positions),
            "by_category": by_category,
        }
