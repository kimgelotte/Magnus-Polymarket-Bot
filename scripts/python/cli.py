"""
Magnus CLI – main entry point.

Most common command:

    python -m scripts.python.cli run-autonomous-trader
"""

import argparse

from agents.application.trade import Trade
from agents.db_manager import DatabaseManager
from agents.polymarket.polymarket import Polymarket


def check_orders_cmd(argv: list[str] | None = None) -> None:
    """List open orders from CLOB – canonical source even if polymarket.com/portfolio doesn't show them."""
    import argparse
    parser = argparse.ArgumentParser(prog="magnus check-orders", description="List open orders from CLOB")
    args = parser.parse_args(argv or [])

    pm = Polymarket()
    orders = pm.get_open_orders()
    funder = getattr(pm, "_l2_funder_for_balance", None) or "?"

    print(f"Proxy (POLYMARKET_FUNDER_ADDRESS): {funder}")
    if orders is None:
        print("Open orders (CLOB): Could not fetch (API error)")
        orders = []
    else:
        print(f"Open orders (CLOB): {len(orders)}")
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
        print("  (No open orders)")
    else:
        print("─" * 50)
        print("If polymarket.com/portfolio?tab=Open+orders is empty: CLOB has the orders. They are active.")


def restore_sell_orders_cmd() -> None:
    """Restore missing GTC sell orders for open positions."""
    from scripts.python.restore_sell_orders import main as restore_main
    restore_main()


def mark_sell_active_cmd() -> None:
    """Mark all open positions as having GTC sell orders (e.g. placed manually on polymarket.com)."""
    db = DatabaseManager()
    n = db.mark_open_positions_sell_active()
    print(f"✅ Marked {n} open position(s) as having GTC sell orders on book.")


def run_autonomous_trader() -> None:
    """Starts Magnus V4 Sniper loop."""
    trade = Trade()
    trade.run_sniper_loop()


def delete_trade_cmd(argv: list[str] | None = None) -> None:
    """Remove a phantom trade (order logged but inventory never existed)."""
    parser = argparse.ArgumentParser(prog="magnus delete-trade", description="Remove trade from DB")
        parser.add_argument("--id", type=int, help="Trade id to remove")
        parser.add_argument("--token-id", type=str, help="Token id to remove")
        parser.add_argument("--list-open", action="store_true", help="List open positions")
        parser.add_argument("--list-all", type=int, nargs="?", metavar="N", const=15, help="List last N trades (default 15)")
    args = parser.parse_args(argv or [])

    db = DatabaseManager()
    if args.list_all is not None:
        trades = db.get_all_trades(limit=args.list_all)
        if not trades:
            print("No trades.")
        else:
            for t in trades:
                print(f"  id={t['id']} status={t['status']} {str(t.get('question',''))[:50]}...")
        return
    if args.list_open:
        positions = db.get_open_positions()
        if not positions:
            print("No open positions.")
        else:
            for p in positions:
                print(f"  id={p['id']} token_id={p['token_id'][:24]}... {p.get('question','')[:50]}...")
        return

    if args.id is not None:
        ok = db.delete_trade(trade_id=args.id)
    elif args.token_id:
        ok = db.delete_trade(token_id=args.token_id)
    else:
        print("Specify --id N or --token-id XXX. Use --list-open to see open.")
        raise SystemExit(1)

    if ok:
        print(f"✅ Trade removed.")
    else:
        print("❌ Could not remove (id/token not found).")
        raise SystemExit(1)


def main() -> None:
    import sys
    parser = argparse.ArgumentParser(prog="magnus", description="Magnus Polymarket Sniper CLI")
    parser.add_argument(
        "command",
        nargs="?",
        default="run-autonomous-trader",
        help="Which command to run (default: run-autonomous-trader)",
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
        elif cmd == "restore-sell-orders":
            restore_sell_orders_cmd()
        elif cmd == "mark-sell-active":
            mark_sell_active_cmd()
        elif cmd == "register-orphans":
            import sys as _sys
            _sys.argv = ["register_orphans"] + remaining
            from scripts.python.register_orphans import main as _reg_main
            _reg_main()
        else:
            raise SystemExit(f"Unknown command: {cmd}")
    except KeyboardInterrupt:
        # Snyggt avbrott vid Ctrl+C utan full traceback.
        print("\n👋 Magnus interrupted with Ctrl+C – shutting down.")


if __name__ == "__main__":
    main()

