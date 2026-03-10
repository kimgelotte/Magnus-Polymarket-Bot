import threading
import time
from typing import Iterable, Optional


class MagnusObserver(threading.Thread):
    """
    Minimal observer/stub for real-time monitoring.

    Original used websockets against Polymarket; here we just track
    which token_ids are active and expose the same interface so
    `Trade.manage_active_trades` and sniper can run without error.
    """

    def __init__(self, token_ids: Iterable[str], trade_manager) -> None:
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self.trade_manager = trade_manager
        self._token_ids = {str(tid) for tid in token_ids if tid}

    # API used from Trade

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
        Sync internal token set from DB – we keep it simple and just re-fetch.
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

    # Thread loop: does nothing heavy; can be extended with websockets in future.

    def run(self) -> None:
        while not self._stop.is_set():
            time.sleep(5.0)

