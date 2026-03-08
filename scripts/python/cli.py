"""
Magnus CLI – huvudentrépunkt.

Vanligaste kommandot:

    python -m scripts.python.cli run-autonomous-trader
"""

import argparse

from agents.application.trade import Trade
from agents.db_manager import DatabaseManager
from agents.polymarket.polymarket import Polymarket


def check_orders_cmd(argv: list[str] | None = None) -> None:
    """Lista öppna ordrar från CLOB – kanonisk källa även om polymarket.com/portfolio inte visar dem."""
    import argparse
    parser = argparse.ArgumentParser(prog="magnus check-orders", description="Lista öppna ordrar från CLOB")
    args = parser.parse_args(argv or [])

    pm = Polymarket()
    orders = pm.get_open_orders()
    funder = getattr(pm, "_l2_funder_for_balance", None) or "?"

    print(f"Proxy (POLYMARKET_FUNDER_ADDRESS): {funder}")
    print(f"Öppna ordrar (CLOB): {len(orders)}")
    print("─" * 50)
    for o in orders[:15]:
        side = o.get("side", "?")
        price = o.get("price", "?")
        orig = o.get("original_size") or o.get("size") or "?"
        try:
            sz = float(orig) / 1e6 if isinstance(orig, str) and orig.isdigit() else orig
        except Exception:
            sz = orig
        asset = str(o.get("asset_id", "?"))[:24]
        print(f"  {side} @ {price}  size≈{sz}  token={asset}...")
    if not orders:
        print("  (Inga öppna ordrar)")
    else:
        print("─" * 50)
        print("Om polymarket.com/portfolio?tab=Open+orders är tom: CLOB har ordrarna. De är aktiva.")


def run_autonomous_trader() -> None:
    """Startar Magnus V4 Sniper‑loopen."""
    trade = Trade()
    trade.run_sniper_loop()


def delete_trade_cmd(argv: list[str] | None = None) -> None:
    """Ta bort en phantom-trade (order loggad men innehavet fanns aldrig)."""
    parser = argparse.ArgumentParser(prog="magnus delete-trade", description="Ta bort trade från DB")
    parser.add_argument("--id", type=int, help="Trade-id att ta bort")
    parser.add_argument("--token-id", type=str, help="Token-id att ta bort")
    parser.add_argument("--list-open", action="store_true", help="Lista öppna positioner")
    parser.add_argument("--list-all", type=int, nargs="?", metavar="N", const=15, help="Lista senaste N trades (default 15)")
    args = parser.parse_args(argv or [])

    db = DatabaseManager()
    if args.list_all is not None:
        trades = db.get_all_trades(limit=args.list_all)
        if not trades:
            print("Inga trades.")
        else:
            for t in trades:
                print(f"  id={t['id']} status={t['status']} {str(t.get('question',''))[:50]}...")
        return
    if args.list_open:
        positions = db.get_open_positions()
        if not positions:
            print("Inga öppna positioner.")
        else:
            for p in positions:
                print(f"  id={p['id']} token_id={p['token_id'][:24]}... {p.get('question','')[:50]}...")
        return

    if args.id is not None:
        ok = db.delete_trade(trade_id=args.id)
    elif args.token_id:
        ok = db.delete_trade(token_id=args.token_id)
    else:
        print("Ange --id N eller --token-id XXX. Använd --list-open för att se öppna.")
        raise SystemExit(1)

    if ok:
        print(f"✅ Trade borttagen.")
    else:
        print("❌ Kunde inte ta bort (id/token hittades inte).")
        raise SystemExit(1)


def main() -> None:
    import sys
    parser = argparse.ArgumentParser(prog="magnus", description="Magnus Polymarket Sniper CLI")
    parser.add_argument(
        "command",
        nargs="?",
        default="run-autonomous-trader",
        help="Vilket kommando som ska köras (default: run-autonomous-trader)",
    )
    args, remaining = parser.parse_known_args()

    cmd = args.command
    try:
        if cmd == "run-autonomous-trader":
            run_autonomous_trader()
        elif cmd == "delete-trade":
            delete_trade_cmd(remaining)
        elif cmd == "check-orders":
            check_orders_cmd(remaining)
        else:
            raise SystemExit(f"Okänt kommando: {cmd}")
    except KeyboardInterrupt:
        # Snyggt avbrott vid Ctrl+C utan full traceback.
        print("\n👋 Magnus avbruten med Ctrl+C – stänger ner.")


if __name__ == "__main__":
    main()

