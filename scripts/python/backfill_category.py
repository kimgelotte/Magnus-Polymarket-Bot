#!/usr/bin/env python3
"""Backfill missing/unknown category for trades by inferring from question text."""
import sys
import re
from pathlib import Path

root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from agents.db_manager import DatabaseManager

def infer_category(question: str) -> str:
    """Infer category from question text."""
    if not question:
        return "Unknown"
    q = question.lower()
    if any(x in q for x in ["vs", "winner", "match", "game ", "goals", "points", "o/u", "esports", "lec ", "cct ", "bo3", "bo5", "nba", "nfl"]):
        return "Sports"
    if any(x in q for x in ["bitcoin", "crypto", "eth ", "sol ", "token"]):
        return "Crypto"
    if any(x in q for x in ["temperature", "°c", "weather", "wellington", "highest temp"]):
        return "Science"  # eller skapa "Weather"
    if any(x in q for x in ["strike", "iran", "election", "president", "congress", "supreme leader"]):
        return "Politics"
    if any(x in q for x in ["war", "military", "nato", "ukraine", "russia"]):
        return "Geopolitics"
    return "Unknown"

def main():
    db = DatabaseManager()
    trades = db.get_all_trades(limit=None)
    updated = 0
    with db._get_connection() as conn:
        cursor = conn.cursor()
        for t in trades:
            cat = (t.get("category") or "").strip()
            if cat and cat != "Unknown":
                continue
            new_cat = infer_category(t.get("question") or "")
            if new_cat == "Unknown":
                continue
            cursor.execute("UPDATE trades SET category = ? WHERE id = ?", (new_cat, t["id"]))
            updated += 1
        conn.commit()
    print(f"✅ Updated category for {updated} trades.")

if __name__ == "__main__":
    main()
