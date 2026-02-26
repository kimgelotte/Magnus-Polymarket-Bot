#!/usr/bin/env python3
"""Mark a trade as sold (CLOSED_PROFIT) by matching question text."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from agents.db_manager import DatabaseManager

SEARCH = "Young Ninjas vs Phantom"

def main():
    search = sys.argv[1] if len(sys.argv) > 1 else SEARCH
    db = DatabaseManager()
    trades = db.get_all_trades(limit=None)
    open_matches = [t for t in trades if (t.get("status") == "OPEN" and search.lower() in (t.get("question") or "").lower())]
    if not open_matches:
        print(f"No open trades found matching: {search!r}")
        return
    for t in open_matches:
        token_id = t["token_id"]
        question = (t.get("question") or "")[:60]
        ok = db.update_trade_status(token_id, "CLOSED_PROFIT", "Manually sold (mark_trade_sold.py)")
        if ok:
            print(f"✅ Marked as sold: {question}... (token_id={token_id})")
        else:
            print(f"❌ Could not update: {question}")
    print(f"Done. {len(open_matches)} trade(s) marked as CLOSED_PROFIT.")

if __name__ == "__main__":
    main()
