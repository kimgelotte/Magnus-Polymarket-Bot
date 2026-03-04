"""
Magnus V4 – Portfolio‑nivå risk.

Trackar:
- Peak‑saldo (USDC)
- Drawdown i % från peak
- Enkel korrelationskontroll per kategori för att undvika för många lika bets.
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
    def __init__(self, db_manager, polymarket) -> None:
        self.db = db_manager
        self.polymarket = polymarket
        self._peak_balance = 0.0
        self._last_balance_log = 0.0
        self._load_peak()

    def _load_peak(self) -> None:
        """Läs in historiskt peak‑saldo från loggfil, om den finns."""
        path = Path(BALANCE_LOG_PATH)
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                    except Exception:
                        continue
                    bal = float(entry.get("balance") or 0)
                    if bal > self._peak_balance:
                        self._peak_balance = bal
        except Exception:
            # Riskhantering får inte krascha botten.
            pass

    def log_balance(self, balance: float) -> None:
        """Logga saldo periodiskt (max en gång per 5 minuter)."""
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
            "balance": round(float(balance), 2),
            "peak": round(self._peak_balance, 2),
        }
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def check_drawdown(self, current_balance: float) -> tuple[bool, float]:
        """
        Returnerar (should_pause, drawdown_pct).

        Pausar nya trades om drawdown > MAX_DRAWDOWN_PCT.
        """
        if self._peak_balance <= 0:
            self._peak_balance = float(current_balance)
            return False, 0.0

        current_balance = float(current_balance)
        if current_balance > self._peak_balance:
            self._peak_balance = current_balance

        drawdown = ((self._peak_balance - current_balance) / self._peak_balance) * 100.0
        return drawdown >= MAX_DRAWDOWN_PCT, round(drawdown, 1)

    def check_correlation(self, new_event_title: str, new_category: str) -> bool:
        """
        True om en ny position skulle göra oss för tungt exponerade i samma kategori.

        Enkel heuristik:
        - Jämför titelord (>= 4 tecken) mellan ny kandidat och existerande trades i samma kategori.
        - Om vi hittar minst två "shared" ord för en trade räknas den som korrelerad.
        - Om antal korrelerade trades når MAX_CORRELATED_POSITIONS → blockera ny trade.
        """
        positions = self.db.get_open_positions()
        if not positions:
            return False

        new_lower = (new_event_title or "").lower()
        new_cat = (new_category or "").strip()

        tokens = [
            w
            for w in new_lower.replace("[", " ").replace("]", " ").split()
            if len(w) >= 4
        ]
        if not tokens:
            return False

        correlated_count = 0
        for p in positions:
            q = (p.get("question") or "").lower()
            cat = (p.get("category") or "").strip()
            if cat != new_cat:
                continue

            shared = sum(1 for t in tokens if t in q)
            if shared >= 2:
                correlated_count += 1
                if correlated_count >= MAX_CORRELATED_POSITIONS:
                    return True

        return False

