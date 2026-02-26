#!/usr/bin/env python3
"""Mark all open trades as CLOSED_PROFIT (manual close)."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from agents.db_manager import DatabaseManager

NOTE = "Manually closed (all positions)"

def main():
    db = DatabaseManager()
    open_trades = db.get_open_positions()
    if not open_trades:
        print("No open trades in database.")
        return
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE trades SET status = ?, notes = IFNULL(notes, '') || ' | ' || ? WHERE status = 'OPEN'",
            ("CLOSED_PROFIT", NOTE),
        )
        n = cursor.rowcount
        conn.commit()
    print(f"✅ {n} trade(s) markerade som CLOSED_PROFIT ({NOTE}).")
    print("Run build_trades_chroma.py to update Chroma with new statuses.")

if __name__ == "__main__":
    main()
