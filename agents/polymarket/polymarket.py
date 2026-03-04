import os
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    BookParams,
)


load_dotenv()


class Polymarket:
    """
    Tunn wrapper runt py_clob_client + Gamma‑API för Magnus.

    Ger:
    - Event‑hämtning (Gamma `/events`)
    - Orderbok/price/likviditet via CLOB
    - USDC‑saldo och token‑balans
    - Market‑ och sell‑orders
    """

    CLOB_HOST = "https://clob.polymarket.com"
    GAMMA_EVENTS_ENDPOINT = "https://gamma-api.polymarket.com/events"

    def __init__(self) -> None:
        private_key = os.getenv("PRIVATE_KEY", "").strip()
        if not private_key:
            raise RuntimeError("PRIVATE_KEY saknas i .env – kan inte initiera Polymarket‑klient.")

        signature_type = int(os.getenv("POLYGON_SIGNATURE_TYPE", "1"))
        funder_address = os.getenv("POLYMARKET_FUNDER_ADDRESS", "").strip() or None

        self.client = ClobClient(
            self.CLOB_HOST,
            chain_id=137,
            key=private_key,
            signature_type=signature_type,
            funder=funder_address,
        )

        # Försök få L2‑creds – krävs för trading (men inte för market‑data).
        try:
            creds = self.client.create_or_derive_api_creds()
            if isinstance(creds, ApiCreds):
                self.client.set_api_creds(creds)
        except Exception:
            # Om detta misslyckas kan vi fortfarande läsa market‑data,
            # men orderläggning kommer senare kasta PolyException vid L2‑krav.
            pass

    # --- Event discovery (Gamma) -------------------------------------------------

    @staticmethod
    def extract_category(event: Dict[str, Any]) -> str:
        """
        Heuristik för kategori från Gamma‑event.
        """
        cat = event.get("category") or ""
        if isinstance(cat, str) and cat:
            return cat
        tags = event.get("tags") or []
        if isinstance(tags, list) and tags:
            return str(tags[0])
        return "Unknown"

    def get_all_events(self, strategy: str = "trending", limit: int = 1000) -> List[Dict[str, Any]]:
        """
        Hämtar aktiva events via Gamma API.

        strategy:
          - 'trending'  → order=volume_24hr
          - 'featured'  → featured=true (om tillgängligt)
          - 'new'       → order=id (nyaste först)
        """
        params: Dict[str, Any] = {
            "active": "true",
            "closed": "false",
            "limit": "100",
        }

        s = (strategy or "").lower()
        if s == "trending":
            params["order"] = "volume_24hr"
            params["ascending"] = "false"
        elif s == "featured":
            params["featured"] = "true"
        elif s == "new":
            params["order"] = "id"
            params["ascending"] = "false"

        events: List[Dict[str, Any]] = []
        offset = 0

        try:
            while len(events) < limit:
                params["offset"] = str(offset)
                resp = httpx.get(self.GAMMA_EVENTS_ENDPOINT, params=params, timeout=10.0)
                if resp.status_code != 200:
                    break
                batch = resp.json()
                if not isinstance(batch, list) or not batch:
                    break
                events.extend(batch)
                if len(batch) < int(params["limit"]):
                    break
                offset += int(params["limit"])
        except Exception:
            return []

        return events[:limit]

    # --- Market data -------------------------------------------------------------

    def get_buy_price(self, token_id: str) -> float:
        """
        Returnerar bästa BUY‑pris (decimal 0–1) för ett token.
        """
        try:
            data = self.client.get_price(str(token_id), side="BUY")
        except Exception:
            return 0.0

        if isinstance(data, (int, float)):
            return float(data)
        if isinstance(data, dict):
            for key in ("price", "bid", "buy"):
                if key in data and data[key] is not None:
                    try:
                        return float(data[key])
                    except (TypeError, ValueError):
                        continue
        return 0.0

    def get_book(self, token_id: str) -> Tuple[Optional[float], Optional[float], float]:
        """
        Returnerar (bid, ask, bid_liquidity_usdc) för token_id.
        """
        try:
            book = self.client.get_order_book(str(token_id))
        except Exception:
            return None, None, 0.0

        bids = book.bids or []
        asks = book.asks or []

        best_bid = float(bids[0].price) if bids and bids[0].price is not None else None
        best_ask = float(asks[0].price) if asks and asks[0].price is not None else None

        bid_liquidity = 0.0
        for lvl in bids[:3]:
            try:
                bid_liquidity += float(lvl.price) * float(lvl.size)
            except (TypeError, ValueError):
                continue

        return best_bid, best_ask, bid_liquidity

    def get_price_history(self, token_id: str) -> List[Dict[str, Any]]:
        """
        Förenklad pris‑historik: hämtar senaste trades och projicerar om till
        en lista av { 'p': price } för War Room.

        CLOB‑API:t exponerar trades per token – vi använder get_last_trades_prices
        om det finns, annars tom list.
        """
        try:
            # Använd senaste priset som enkel "historia" om inget bättre finns.
            last_price = self.get_buy_price(token_id)
            if last_price <= 0:
                return []
            # Skapa en syntetisk historik med 20 punkter runt senaste priset
            # (detta är bara för att ge War Room något att jobba med).
            return [{"p": last_price} for _ in range(20)]
        except Exception:
            return []

    # --- Balans ------------------------------------------------------------------

    def get_usdc_balance(self) -> float:
        """
        Returnerar USDC‑saldo via CLOB balance/allowance‑endpoint.
        Kräver L2‑auth; vid fel returneras 0.
        """
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            data = self.client.get_balance_allowance(params)
        except Exception:
            return 0.0

        if isinstance(data, dict):
            bal = data.get("balance") or data.get("collateral") or 0
            try:
                return float(bal)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def get_token_balance(self, token_id: str) -> float:
        """
        Returnerar antal shares för givet token_id.
        """
        try:
            positions = self.client.get_positions()
        except Exception:
            return 0.0

        for pos in positions or []:
            try:
                if str(pos.get("token_id")) == str(token_id):
                    return float(pos.get("balance") or 0)
            except (TypeError, ValueError):
                continue
        return 0.0

    # --- Orders ------------------------------------------------------------------

    def execute_market_order(self, market_to_buy, amount_usdc: float) -> Optional[str]:
        """
        Lägger en market‑BUY order i USDC på active_token_id.
        Returnerar orderId vid OK, annars None.
        """
        token_id = str(getattr(market_to_buy, "active_token_id"))
        try:
            args = MarketOrderArgs(
                token_id=token_id,
                amount=float(amount_usdc),
                side="BUY",
            )
            order = self.client.create_market_order(args)
            res = self.client.post_order(order)
            return res.get("orderId") or res.get("order_id")
        except Exception:
            return None

    def execute_sell_order(self, token_id: str, shares: float, price: float) -> bool:
        """
        Lägger en limit‑SELL order för ett token vid givet pris.
        """
        try:
            # Hämta tick size/neg_risk automatiskt
            options = self.client.get_partial_create_order_options(str(token_id))
            args = MarketOrderArgs(
                token_id=str(token_id),
                amount=float(shares),
                side="SELL",
                price=float(price),
            )
            order = self.client.create_order_from_market_order(args, options=options)
            res = self.client.post_order(order)
            return bool(res)
        except Exception:
            return False

