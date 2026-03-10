#!/usr/bin/env python3
"""
Magnus – restore missing GTC sell orders.

If GTC sell orders have disappeared (heartbeat interrupted, restart, etc.)
you can run this to add back sell orders at target_price
for all open positions that have shares but no sell order.

Usage:
    python -m scripts.python.restore_sell_orders
"""

import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from agents.db_manager import DatabaseManager
from agents.polymarket.polymarket import Polymarket


def main() -> None:
    db = DatabaseManager()
    pm = Polymarket()

    positions = db.get_open_positions()
    if not positions:
        print("No open positions.")
        return

    balances = pm.get_all_token_balances()
    restored = 0

    for t in positions:
        t_id = str(t["token_id"])
        target_price = float(t.get("target_price") or 0)
        actual_balance = balances.get(t_id, 0.0)

        if actual_balance < 5.0 or target_price < 0.01:
            continue

        try:
            open_orders = pm.get_open_orders(asset_id=t_id)
            if open_orders is None:
                print(f"   ⚠️ Could not fetch orders for {t_id} – skipping")
                continue
            orders_list = (
                open_orders.get("data", open_orders)
                if isinstance(open_orders, dict)
                else (open_orders or [])
            )
            has_sell = any(
                str(
                    getattr(o, "side", o.get("side", "") if isinstance(o, dict) else "")
                ).upper()
                == "SELL"
                for o in (orders_list if isinstance(orders_list, list) else [])
            )

            if not has_sell:
                print(f"📤 Restoring GTC sell: {t['question'][:40]}… @ {target_price:.2f}")
                ok = pm.execute_sell_order(t_id, actual_balance, target_price)
                if ok:
                    restored += 1
                    print(f"   ✓ Placed.")
                else:
                    print(f"   ⚠️ Failed.")
        except Exception as e:
            print(f"   ❌ Error for {t_id}: {e}")

    print(f"\n✅ Restored {restored} sell order(s).")


if __name__ == "__main__":
    main()
