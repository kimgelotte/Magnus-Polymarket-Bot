#!/usr/bin/env python3
"""
Magnus – registrera orphan-positioner i DB.

Hittar positioner vi äger on-chain (Polymarket) som inte finns i databasen,
hämtar marknadsinfo via Gamma API, och loggar dem som trades med GTC-sälj.

Användning:
    python -m scripts.python.register_orphans              # Dry-run (visa endast)
    python -m scripts.python.register_orphans --apply       # Logga till DB + placera GTC-sälj
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from agents.db_manager import DatabaseManager
from agents.polymarket.polymarket import Polymarket
from agents.dynamic_target import compute_dynamic_target


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="register_orphans",
        description="Hitta och registrera orphan-positioner (on-chain men inte i DB)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Logga till DB och placera GTC-sälj. Utan flagga: dry-run (visa endast).",
    )
    parser.add_argument(
        "--min-shares",
        type=float,
        default=5.0,
        help="Minsta andelar för att räknas som position (default: 5, Polymarket-krav)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Visa diagnostik: antal positioner, öppna trades, filterlogik",
    )
    args = parser.parse_args()

    db = DatabaseManager()
    pm = Polymarket()

    open_trades = db.get_open_positions()
    open_token_ids = {str(t["token_id"]) for t in open_trades}

    # För proxy: använd Data API som har full metadata (title, avgPrice, endDate)
    # För EOA: använd get_all_token_balances + get_market_info_by_token_id
    has_funder = bool(getattr(pm, "_l2_funder_for_balance", None))
    positions_with_meta = pm.get_positions_with_metadata() if has_funder else []

    if has_funder and positions_with_meta:
        positions = {str(p.get("asset", "")): float(p.get("size") or 0) for p in positions_with_meta if p.get("asset")}
    else:
        positions = pm.get_all_token_balances()

    if args.debug:
        funder = getattr(pm, "_l2_funder_for_balance", None) or "ej satt (EOA)"
        print(f"DEBUG: POLYMARKET_FUNDER_ADDRESS = {funder}")
        print(f"DEBUG: {len(positions)} positioner")
        for tid, bal in list(positions.items())[:10]:
            in_db = "i DB" if tid in open_token_ids else "ej i DB"
            print(f"  {tid[:28]}… bal={bal:.2f} {in_db}")
        if len(positions) > 10:
            print(f"  … och {len(positions) - 10} till")
        print(f"DEBUG: {len(open_trades)} öppna trades i DB")
        print("-" * 60)

    orphans = []
    for token_id, balance in positions.items():
        if balance < args.min_shares or token_id in open_token_ids:
            continue
        # Hitta metadata: från Data API (positions_with_meta) eller Gamma
        meta = next((p for p in positions_with_meta if str(p.get("asset")) == str(token_id)), None)
        orphans.append((token_id, balance, meta))

    if not orphans:
        print("Inga orphan-positioner hittades.")
        if args.debug and positions:
            print("  (Positioner finns men alla är antingen < min_shares eller redan i DB)")
        return

    print(f"Hittade {len(orphans)} orphan(s):")
    print("-" * 60)

    for token_id, balance, meta in orphans:
        if meta and meta.get("title"):
            # Data API: full metadata (title, avgPrice, endDate, etc.)
            full_title = str(meta.get("title", ""))
            buy_price = float(meta.get("avgPrice") or 0.25)
            end_iso = str(meta.get("endDate", ""))
            if end_iso and len(end_iso) == 10:
                end_iso = end_iso + "T23:59:59Z"
            info = {
                "market_id": str(meta.get("conditionId", "")),
                "question": full_title,
                "end_date_iso": end_iso,
                "category": "Sports",
                "event_id": str(meta.get("eventId", "")),
            }
        else:
            info = pm.get_market_info_by_token_id(token_id)
            if not info:
                print(f"  ⚠️ {token_id[:24]}… ({balance:.1f} shares) – kunde inte hämta marknadsinfo")
                continue
            full_title = info.get("question", "")
            group = info.get("groupItemTitle", "")
            full_title = f"{full_title} [{group}]" if group else full_title
            end_iso = info.get("end_date_iso", "")

        bid, ask, _ = pm.get_book(token_id)
        if not meta:
            buy_price = float(bid) if bid and bid > 0 else (float(ask) * 0.95 if ask and ask > 0 else 0.25)
        if buy_price <= 0:
            buy_price = 0.25

        days_until_end = None
        if end_iso:
            try:
                end_str = end_iso.replace("Z", "+00:00")
                if "+" not in end_str and not end_str.endswith("00:00"):
                    end_str = end_str + "+00:00"
                end_dt = dt.datetime.fromisoformat(end_str)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=dt.timezone.utc)
                delta = end_dt - dt.datetime.now(dt.timezone.utc)
                days_until_end = max(0, delta.total_seconds() / 86400)
            except Exception:
                pass

        spread_pct = 10.0
        if bid and ask and (float(bid) + float(ask)) > 0:
            mid = (float(bid) + float(ask)) / 2
            spread_pct = round((float(ask) - float(bid)) / mid * 100, 1)

        target_price = compute_dynamic_target(
            fill_price=buy_price,
            days_until_end=days_until_end,
            range_pct=15.0,
            hype_score=0,
            spread_pct=spread_pct,
            ai_max_price=0.99,
            base_target_pct=0.07,
            high_target_pct=0.10,
            price_high_threshold=0.30,
        )
        amount_usdc = balance * buy_price

        print(f"  • {full_title[:55]}…")
        print(f"    token_id={token_id[:28]}… | {balance:.1f} shares | ~{buy_price:.2f} fill | target={target_price:.2f}")

        if args.apply:
            try:
                db.log_new_trade(
                    token_id=token_id,
                    market_id=info.get("market_id", ""),
                    question=full_title,
                    buy_price=buy_price,
                    amount_usdc=amount_usdc,
                    shares_bought=balance,
                    notes="Orphan – registrerad via register_orphans.py",
                    category=info.get("category", ""),
                    target_price=target_price,
                    end_date_iso=info.get("end_date_iso", ""),
                    event_id=info.get("event_id"),
                )
                ok = pm.execute_sell_order(token_id, balance, target_price)
                if ok:
                    print(f"    ✅ Loggad + GTC-sälj placerad @ {target_price:.2f}")
                else:
                    print(f"    ⚠️ Loggad men GTC-sälj misslyckades – kör restore-sell-orders")
            except Exception as e:
                print(f"    ❌ Fel: {e}")

    if orphans and not args.apply:
        print("-" * 60)
        print("Kör med --apply för att logga till DB och placera GTC-säljordrar.")


if __name__ == "__main__":
    main()
