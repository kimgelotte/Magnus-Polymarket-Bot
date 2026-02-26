"""
Magnus V4 Post-Trade Analysis.

Evaluates closed trades against AI predictions to identify which categories,
hype scores, and price contexts yield the best edge.

Usage: python -m scripts.python.post_trade_analysis [--limit N]
"""

import sys
import os
import argparse
from pathlib import Path
from collections import defaultdict

sys.path.append(str(Path(__file__).resolve().parents[2]))

from agents.db_manager import DatabaseManager


def analyze_trades(limit: int | None = None):
    db = DatabaseManager()
    trades = db.get_all_trades(limit=limit)
    analyses = db.get_all_analyses(limit=limit)

    if not trades:
        print("No trades found.")
        return

    closed = [t for t in trades if t.get("status", "").startswith("CLOSED")]
    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    wins = [t for t in closed if t.get("status") == "CLOSED_PROFIT"]
    losses = [t for t in closed if t.get("status") == "CLOSED_LOSS"]

    print("=" * 70)
    print("MAGNUS V4 — POST-TRADE ANALYSIS")
    print("=" * 70)

    print(f"\nTotal trades: {len(trades)}")
    print(f"  Open: {len(open_trades)}")
    print(f"  Closed: {len(closed)}")
    print(f"  Wins: {len(wins)}")
    print(f"  Losses: {len(losses)}")
    if closed:
        print(f"  Win rate: {len(wins) / len(closed) * 100:.1f}%")

    # Per-category breakdown
    print("\n" + "-" * 50)
    print("BY CATEGORY:")
    cat_stats = defaultdict(lambda: {"total": 0, "wins": 0, "losses": 0, "open": 0})
    for t in trades:
        cat = t.get("category") or "Unknown"
        cat_stats[cat]["total"] += 1
        if t.get("status") == "CLOSED_PROFIT":
            cat_stats[cat]["wins"] += 1
        elif t.get("status") == "CLOSED_LOSS":
            cat_stats[cat]["losses"] += 1
        elif t.get("status") == "OPEN":
            cat_stats[cat]["open"] += 1

    for cat in sorted(cat_stats, key=lambda c: cat_stats[c]["total"], reverse=True):
        s = cat_stats[cat]
        closed_n = s["wins"] + s["losses"]
        wr = f"{s['wins'] / closed_n * 100:.0f}%" if closed_n > 0 else "N/A"
        print(f"  {cat:20s}: {s['total']:3d} total | {s['wins']:2d}W {s['losses']:2d}L {s['open']:2d}O | WR: {wr}")

    # Analysis accuracy: compare BUY decisions to outcomes
    print("\n" + "-" * 50)
    print("AI DECISION ACCURACY:")

    if analyses:
        buy_analyses = [a for a in analyses if a.get("action") == "BUY"]
        reject_analyses = [a for a in analyses if a.get("action") == "REJECT"]
        print(f"  Total analyses: {len(analyses)}")
        print(f"  BUY decisions: {len(buy_analyses)} ({len(buy_analyses) / len(analyses) * 100:.1f}%)")
        print(f"  REJECT decisions: {len(reject_analyses)} ({len(reject_analyses) / len(analyses) * 100:.1f}%)")

        # Hype score distribution for BUY vs REJECT
        buy_hype = [a.get("hype_score", 0) for a in buy_analyses if a.get("hype_score")]
        reject_hype = [a.get("hype_score", 0) for a in reject_analyses if a.get("hype_score")]
        if buy_hype:
            print(f"  Avg hype (BUY): {sum(buy_hype) / len(buy_hype):.1f}")
        if reject_hype:
            print(f"  Avg hype (REJECT): {sum(reject_hype) / len(reject_hype):.1f}")

        # BUY/REJECT ratio per category
        print("\n  BUY/REJECT per category:")
        cat_decisions = defaultdict(lambda: {"BUY": 0, "REJECT": 0})
        for a in analyses:
            cat = a.get("category") or "Unknown"
            act = a.get("action", "REJECT")
            cat_decisions[cat][act] += 1

        for cat in sorted(cat_decisions, key=lambda c: cat_decisions[c]["BUY"] + cat_decisions[c]["REJECT"], reverse=True):
            d = cat_decisions[cat]
            total = d["BUY"] + d["REJECT"]
            br = f"{d['BUY'] / total * 100:.0f}%" if total > 0 else "N/A"
            print(f"    {cat:20s}: {d['BUY']:3d} BUY / {d['REJECT']:3d} REJECT (buy rate: {br})")

    # Suggested Kelly adjustments
    print("\n" + "-" * 50)
    print("SUGGESTED ADJUSTMENTS:")
    for cat, s in sorted(cat_stats.items(), key=lambda x: x[1]["total"], reverse=True):
        closed_n = s["wins"] + s["losses"]
        if closed_n < 3:
            continue
        wr = s["wins"] / closed_n
        if wr >= 0.7:
            print(f"  {cat}: WR {wr * 100:.0f}% — consider INCREASING Kelly fraction")
        elif wr <= 0.3:
            print(f"  {cat}: WR {wr * 100:.0f}% — consider REDUCING Kelly fraction or tightening filters")
        else:
            print(f"  {cat}: WR {wr * 100:.0f}% — current parameters seem balanced")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Magnus V4 Post-Trade Analysis")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    analyze_trades(limit=args.limit)
