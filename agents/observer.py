import threading
import time
from typing import Iterable, Optional


class MagnusObserver(threading.Thread):
    """
    Minimal observer/stub för realtidsövervakning.

    Originalet använde websockets mot Polymarket; här håller vi bara koll på
    vilka token_ids som är aktiva och exponerar samma gränssnitt så att
    `Trade.manage_active_trades` och snipern kan köras utan fel.
    """

    def __init__(self, token_ids: Iterable[str], trade_manager) -> None:
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self.trade_manager = trade_manager
        self._token_ids = {str(tid) for tid in token_ids if tid}

    # API som används från Trade

    def add_token(
        self,
        token_id: str,
        buy_price: float,
        question: str,
        target_price: Optional[float] = None,
    ) -> None:
        self._token_ids.add(str(token_id))

    def remove_token(self, token_id: str) -> None:
        self._token_ids.discard(str(token_id))

    def sync_from_db(self) -> None:
        """
        Synka internt token‑set från DB – vi gör det enkelt och hämtar bara om.
        """
        try:
            trades = self.trade_manager.db.get_open_positions()
            self._token_ids = {
                str(t["token_id"]) for t in trades if t.get("token_id")
            }
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()

    # Thread‑loop: gör inget tungt; kan utökas med websockets i framtiden.

    def run(self) -> None:
        while not self._stop.is_set():
            time.sleep(5.0)

