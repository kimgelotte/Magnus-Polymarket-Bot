#!/usr/bin/env python3
"""
Magnus – återställ saknade GTC-säljordrar.

Om GTC-säljordrar har försvunnit (heartbeat avbruten, restart, etc.)
kan du köra detta för att lägga tillbaka säljordrar vid target_price
för alla öppna positioner som har andelar men ingen säljorder.

Användning:
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
        print("Inga öppna positioner.")
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
                print(f"   ⚠️ Kunde inte hämta ordrar för {t_id} – skippar")
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
                print(f"📤 Återställer GTC-sälj: {t['question'][:40]}… @ {target_price:.2f}")
                ok = pm.execute_sell_order(t_id, actual_balance, target_price)
                if ok:
                    restored += 1
                    print(f"   ✓ Placerad.")
                else:
                    print(f"   ⚠️ Misslyckades.")
        except Exception as e:
            print(f"   ❌ Fel för {t_id}: {e}")

    print(f"\n✅ Återställde {restored} säljordrar.")


if __name__ == "__main__":
    main()
