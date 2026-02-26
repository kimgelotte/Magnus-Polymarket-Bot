"""
Magnus V4 Dashboard — FastAPI status endpoint.

Run standalone:  python -m agents.dashboard
Or import and start in background from trade.py.
"""

import os
import sys
import threading
from pathlib import Path
from datetime import datetime, timezone

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from agents.db_manager import DatabaseManager

try:
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    import uvicorn
except ImportError:
    FastAPI = None  # type: ignore


def _build_app(db: DatabaseManager | None = None, polymarket=None):
    if FastAPI is None:
        raise ImportError("fastapi and uvicorn are required: pip install fastapi uvicorn")

    app = FastAPI(title="Magnus V4 Dashboard", version="1.0")
    _db = db or DatabaseManager()

    @app.get("/health")
    def health():
        return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}

    @app.get("/balance")
    def balance():
        if polymarket:
            bal = polymarket.get_usdc_balance()
            return {"usdc": round(bal, 2)}
        return {"usdc": None, "error": "Polymarket not connected"}

    @app.get("/positions")
    def positions():
        trades = _db.get_open_positions()
        result = []
        for t in trades:
            buy_p = float(t.get("buy_price") or 0)
            current_price = None
            pnl_pct = None
            if polymarket and t.get("token_id"):
                try:
                    current_price = polymarket.get_buy_price(t["token_id"])
                    if current_price and buy_p > 0:
                        pnl_pct = round(((current_price - buy_p) / buy_p) * 100, 1)
                except Exception:
                    pass
            result.append({
                "question": t.get("question", "")[:80],
                "category": t.get("category", "Unknown"),
                "buy_price": buy_p,
                "current_price": current_price,
                "pnl_pct": pnl_pct,
                "amount_usdc": t.get("amount_usdc"),
                "target_price": t.get("target_price"),
                "opened": t.get("timestamp"),
            })
        return {"count": len(result), "positions": result}

    @app.get("/stats")
    def stats():
        all_trades = _db.get_all_trades()
        if not all_trades:
            return {"total": 0}

        closed = [t for t in all_trades if t.get("status", "").startswith("CLOSED")]
        open_trades = [t for t in all_trades if t.get("status") == "OPEN"]
        wins = [t for t in closed if t.get("status") == "CLOSED_PROFIT"]

        categories = {}
        for t in all_trades:
            cat = t.get("category") or "Unknown"
            if cat not in categories:
                categories[cat] = {"total": 0, "wins": 0, "open": 0}
            categories[cat]["total"] += 1
            if t.get("status") == "CLOSED_PROFIT":
                categories[cat]["wins"] += 1
            if t.get("status") == "OPEN":
                categories[cat]["open"] += 1

        return {
            "total": len(all_trades),
            "open": len(open_trades),
            "closed": len(closed),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "by_category": categories,
        }

    @app.get("/analyses")
    def analyses(limit: int = 50):
        rows = _db.get_all_analyses(limit=limit)
        buys = sum(1 for r in rows if r.get("action") == "BUY")
        rejects = sum(1 for r in rows if r.get("action") == "REJECT")
        by_cat = {}
        for r in rows:
            cat = r.get("category") or "Unknown"
            if cat not in by_cat:
                by_cat[cat] = {"BUY": 0, "REJECT": 0}
            act = r.get("action", "REJECT")
            by_cat[cat][act] = by_cat[cat].get(act, 0) + 1

        return {
            "total": len(rows),
            "buys": buys,
            "rejects": rejects,
            "buy_rate": round(buys / len(rows) * 100, 1) if rows else 0,
            "by_category": by_cat,
            "recent": rows[:10],
        }

    return app


def start_dashboard_background(db=None, polymarket=None, port: int = 8877):
    """Start dashboard in a background thread (non-blocking)."""
    if FastAPI is None:
        print("⚠️ Dashboard skipped: pip install fastapi uvicorn")
        return None

    app = _build_app(db=db, polymarket=polymarket)

    def _run():
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print(f"📊 Dashboard running at http://localhost:{port}")
    return t


if __name__ == "__main__":
    port = int(os.getenv("MAGNUS_DASHBOARD_PORT", "8877"))
    app = _build_app()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)
