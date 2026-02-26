import sys
import os
import typer
from devtools import pprint

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from agents.polymarket.polymarket import Polymarket
from agents.application.trade import Trade
from agents.db_manager import DatabaseManager

app = typer.Typer()
polymarket = Polymarket()

def get_active_token_ids():
    db = DatabaseManager()
    positions = db.get_open_positions()
    return list({pos["token_id"] for pos in positions if pos.get("token_id")})

@app.command()
def get_all_events(limit: int = 5, strategy: str = "trending") -> None:
    """Query Polymarket events"""
    events = polymarket.get_all_events(strategy=strategy, limit=limit)
    pprint(events[:limit])

@app.command()
def run_autonomous_trader() -> None:
    """Start Magnus V4 autonomous trading loop."""
    print("🚀 Initializing Magnus Sniper Mode V4 (Fully Autonomous)...")
    trader = Trade()
    
    try:
        trader.run_sniper_loop()
    except KeyboardInterrupt:
        print("\n👋 Magnus shutting down...")
    except Exception as e:
        print(f"\n[CRITICAL ERROR] Bot stopped: {e}")

if __name__ == "__main__":
    app()