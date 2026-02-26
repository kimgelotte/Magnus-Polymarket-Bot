#!/usr/bin/env python3
"""Re-place GTC sell orders for all open positions in DB."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from agents.db_manager import DatabaseManager
from agents.polymarket.polymarket import Polymarket

# Dynamic target: 10% if buy < 0.30, else 7%
PRICE_HIGH_THRESHOLD = 0.30
PROFIT_TARGET = 0.07
PROFIT_TARGET_HIGH = 0.10

def main():
    db = DatabaseManager()
    pm = Polymarket()
    if not pm.client:
        print("❌ Polymarket client not initialized (check .env).")
        return

    positions = db.get_open_positions()
    if not positions:
        print("No open positions in database.")
        return

    print(f"📋 Found {len(positions)} open positions. Re-placing GTC sell orders (dynamic target: 10% if buy<0.30, else 7%)…\n")

    for i, t in enumerate(positions, 1):
        token_id = str(t["token_id"])
        question = (t.get("question") or "?")[:50]
        buy_price = float(t.get("buy_price") or 0)
        if buy_price <= 0:
            print(f"   [{i}] Skipping {token_id}: missing buy_price in DB.")
            continue

        actual_shares = pm.get_token_balance(token_id)
        if actual_shares < 0.01:
            print(f"   [{i}] {question}… → Balance 0 (already sold?). Skipping.")
            continue
        if actual_shares < 5.0:
            print(f"   [{i}] {question}… → Too few shares ({actual_shares:.2f}, min 5). Skipping.")
            continue

        stored_target = t.get("target_price")
        if stored_target is not None and float(stored_target) >= 0.01:
            target_price = round(float(stored_target), 3)
        else:
            pct = PROFIT_TARGET_HIGH if buy_price < PRICE_HIGH_THRESHOLD else PROFIT_TARGET
            target_price = round(buy_price * (1 + pct), 3)
        print(f"   [{i}] {question}…")
        print(f"       Shares: {actual_shares:.2f} | Buy: {buy_price:.2f} → Sell target: {target_price:.2f}")

        result = pm.execute_sell_order(token_id, actual_shares, target_price)
        if result is True:
            print(f"       ✅ Sell order filled immediately.")
        elif result == "BALANCE_ERROR":
            print(f"       ⚠️ BALANCE_ERROR (should not trigger here).")
        else:
            print(f"       📤 GTC sell order placed in book (waiting for {target_price:.2f}).")

    print("\n✅ Done.")

if __name__ == "__main__":
    main()
