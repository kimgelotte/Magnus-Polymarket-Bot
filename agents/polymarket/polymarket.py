# core polymarket api
import os
import time
import math
import threading
import httpx
from types import SimpleNamespace
from datetime import datetime, timezone
from dotenv import load_dotenv
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from web3 import Web3
from web3.constants import MAX_INT
from web3.middleware import geth_poa_middleware
import py_clob_client.http_helpers.helpers as http_helpers
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType
from py_clob_client.constants import POLYGON
try:
    from py_clob_client.exceptions import PolyApiException
except ImportError:
    PolyApiException = Exception  # fallback if module structure differs
from py_order_utils.signer import Signer
from py_clob_client.order_builder.constants import BUY, SELL


load_dotenv()

class Polymarket:
    def __init__(self) -> None:
        self.gamma_url = "https://gamma-api.polymarket.com"
        self.gamma_markets_endpoint = self.gamma_url + "/markets"
        self.gamma_events_endpoint = self.gamma_url + "/events"

        self.clob_url = "https://clob.polymarket.com"
        self.chain_id = 137 
        
        self.private_key = os.getenv("PRIVATE_KEY")
        env_rpc = os.getenv("POLYGON_CONFIG_MAINNET_RPC_URL")
        self.polygon_rpc = env_rpc if env_rpc else "https://polygon-rpc.com"
        
        self.web3 = Web3(Web3.HTTPProvider(self.polygon_rpc))
        self.web3.middleware_onion.inject(geth_poa_middleware, layer=0)
        self.w3 = self.web3

        # Contract addresses
        self.exchange_address = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
        self.usdc_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        self.ctf_address = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        self.proxy_address = os.getenv("POLYMARKET_FUNDER_ADDRESS", "").strip()
        if not self.proxy_address:
            raise ValueError("POLYMARKET_FUNDER_ADDRESS not set in .env")
        
        self.client = None
        
        self.erc20_abi = '[{"inputs":[{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"approve","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]'
        self.ctf_abi = '[{"inputs":[{"internalType":"address","name":"account","type":"address"},{"internalType":"uint256","name":"id","type":"uint256"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]'

        self.usdc = self.web3.eth.contract(address=self.web3.to_checksum_address(self.usdc_address), abi=self.erc20_abi)
        self.ctf = self.web3.eth.contract(address=self.web3.to_checksum_address(self.ctf_address), abi=self.ctf_abi)

        self._order_lock = threading.Lock()
        self._http = httpx.Client(timeout=15.0, headers={"User-Agent": "Magnus-Sniper-V4"})

        self._init_api_keys()

    def _init_api_keys(self) -> None:
        try:
            user_creds = SimpleNamespace(
                api_key=os.getenv("USER_API_KEY"),
                api_secret=os.getenv("USER_SECRET"),
                api_passphrase=os.getenv("USER_PASSPHRASE")
            )
            self.client = ClobClient(
                host=self.clob_url,
                key=self.private_key,
                chain_id=self.chain_id,
                creds=user_creds,
                signature_type=1, 
                funder=self.proxy_address
            )
            print(f"✅ Magnus full-access active! Proxy: {self.proxy_address[:10]}")
        except Exception as e:
            print(f"❌ Init error: {e}")

    def get_token_balance(self, token_id: str) -> float:
        """Returns balance from on-chain."""
        try:
            tid_int = int(token_id)
            proxy_addr = self.web3.to_checksum_address(self.proxy_address)
            raw_balance = self.ctf.functions.balanceOf(proxy_addr, tid_int).call()
            return float(raw_balance) / 1_000_000
        except Exception as e:
            print(f"⚠️ On-chain balance check failed: {e}")
            return 0.0

    GAMMA_EVENTS_PAGE_SIZE = 500  # Gamma API max ~500 per call; pagination via offset

    def get_all_events(self, strategy: str = "new", limit: int = 1000) -> list:
        """Fetches events by strategy. Paginates via offset. Strategies: 'new', 'trending', 'featured'."""
        headers = {"User-Agent": "Magnus-Sniper-V4"}
        all_events = []

        if strategy == "featured":
            try:
                print(f"\n📡 Contacting Gamma API (FEATURED)...")
                params = {
                    "active": "true",
                    "closed": "false",
                    "featured": "true",
                    "limit": "100",
                }
                res = self._http.get(self.gamma_events_endpoint, params=params, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    all_events = data if isinstance(data, list) else []
                    print(f"✅ API responded: fetched {len(all_events)} featured events.")
                else:
                    print(f"❌ API error (featured): status {res.status_code}")
            except Exception as e:
                print(f"❌ Network error (Gamma featured): {e}")
            return all_events

        if strategy == "new":
            order_field = "id"
            is_ascending = "false"
        else:
            order_field = "volume24hr"
            is_ascending = "false"

        page_size = min(self.GAMMA_EVENTS_PAGE_SIZE, limit)

        try:
            print(f"\n📡 Contacting Gamma API ({strategy.upper()})...")
            offset = 0
            while len(all_events) < limit:
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit": str(page_size),
                    "offset": str(offset),
                    "order": order_field,
                    "ascending": is_ascending,
                }
                res = self._http.get(
                    self.gamma_events_endpoint,
                    params=params,
                    headers=headers,
                )
                if res.status_code != 200:
                    print(f"❌ API error: status {res.status_code}")
                    break
                data = res.json()
                chunk = data if isinstance(data, list) else []
                if not chunk:
                    break
                all_events.extend(chunk)
                if len(chunk) < page_size:
                    break
                offset += len(chunk)
                if offset >= limit:
                    break
            if all_events:
                print(f"✅ API responded: fetched {len(all_events)} events (pagination offset 0–{offset}).")
            return all_events[:limit]
        except Exception as e:
            print(f"❌ Network error (Gamma): {e}")
            return all_events[:limit] if all_events else []

    CATEGORY_TAG_MAP = {
        "sports": "Sports",
        "soccer": "Sports",
        "nfl": "Sports",
        "nba": "Sports",
        "mlb": "Sports",
        "nhl": "Sports",
        "tennis": "Sports",
        "mma": "Sports",
        "boxing": "Sports",
        "cricket": "Sports",
        "formula 1": "Sports",
        "golf": "Sports",
        "crypto": "Crypto",
        "bitcoin": "Crypto",
        "ethereum": "Crypto",
        "solana": "Crypto",
        "defi": "Crypto",
        "politics": "Politics",
        "elections": "Elections",
        "trump": "Trump",
        "trump presidency": "Trump",
        "geopolitics": "Geopolitics",
        "ukraine": "Geopolitics",
        "china": "Geopolitics",
        "iran": "Geopolitics",
        "russia": "Geopolitics",
        "economics": "Economics",
        "fed": "Economics",
        "fed rates": "Economics",
        "inflation": "Economics",
        "business": "Business",
        "earnings": "Earnings",
        "tech": "Tech",
        "ai": "Tech",
        "pop culture": "Pop Culture",
        "entertainment": "Culture",
        "music": "Culture",
        "movies": "Culture",
        "weather": "Weather",
        "world": "World",
    }

    CATEGORY_PRIORITY = [
        "Sports", "Crypto", "Elections", "Politics", "Trump",
        "Geopolitics", "Economics", "Business", "Earnings", "Tech",
        "Weather", "Pop Culture", "Culture", "World",
    ]

    @classmethod
    def extract_category(cls, event: dict) -> str:
        """Extract category from event tags array. Falls back to title keyword matching."""
        tags = event.get("tags") or []
        found = set()
        for tag in tags:
            label = (tag.get("label") or tag.get("slug") or "").lower().strip()
            mapped = cls.CATEGORY_TAG_MAP.get(label)
            if mapped:
                found.add(mapped)

        if not found:
            title = (event.get("title") or "").lower()
            for keyword, cat in cls.CATEGORY_TAG_MAP.items():
                if keyword in title:
                    found.add(cat)
                    break

        if not found:
            return "Unknown"

        for priority_cat in cls.CATEGORY_PRIORITY:
            if priority_cat in found:
                return priority_cat
        return next(iter(found))

    def map_api_to_event(self, event) -> dict:
        end_date = event.get("endDate") or event.get("end") or "2026-12-31T23:59:59Z"
        return {
            "id": int(event.get("id", 0)),
            "ticker": event.get("ticker", "UNKNOWN"),
            "slug": event.get("slug", ""),
            "title": event.get("question") or event.get("title") or "Untitled",
            "description": event.get("description", ""),
            "active": event.get("active", True),
            "closed": event.get("closed", False),
            "archived": event.get("archived", False),
            "end": end_date,
            "markets": str(event.get("id")),
            "restricted": event.get("restricted", False),
            "new": event.get("new", False),
            "featured": event.get("featured", False)
        }

    def get_market(self, market_id: str) -> dict:
        try:
            res = self._http.get(f"{self.gamma_url}/markets/{market_id}")
            return res.json() if res.status_code == 200 else {}
        except Exception:
            return {}

    def execute_market_order(self, market_obj, amount_usdc) -> str:
        with self._order_lock:
            return self._execute_market_order_unsafe(market_obj, amount_usdc)

    def _execute_market_order_unsafe(self, market_obj, amount_usdc) -> str:
        original_post = None
        try:
            # Cancel only orders for this token, not other positions' GTC sells
            target_token_id = getattr(market_obj, 'active_token_id', None)
            try:
                if target_token_id:
                    self.client.cancel_market_orders(asset_id=target_token_id)
            except Exception:
                pass

            original_post = http_helpers.post
            def patched_post(endpoint, headers, data, **kwargs):
                if endpoint.endswith("/orders") and isinstance(data, dict):
                    order_part = data.get("order", {})
                    if order_part:
                        for k, v in order_part.items():
                            if k in ["feeRateBps", "side", "signatureType"]: order_part[k] = int(v) if not hasattr(v, 'value') else int(v.value)
                            else: order_part[k] = str(v) if not hasattr(v, 'value') else str(v.value)
                        data["order"] = order_part
                return original_post(endpoint, headers, data, **kwargs)
            
            http_helpers.post = patched_post

            clean_amount = float(Decimal(str(amount_usdc)).quantize(Decimal("0.00"), rounding=ROUND_DOWN))
            
            current_ask = self.get_buy_price(target_token_id)
            if not current_ask or current_ask < 0.01:
                print(f"🔥 Buy aborted: no ask or liquidity (token {target_token_id[:12]}…).")
                return ""
            # Buy at ask (no premium) – optimized for low entry
            limit_price = round(current_ask, 3)
            if limit_price >= 1.0: limit_price = 0.99

            raw_shares = float(clean_amount) / limit_price
            shares = float(Decimal(str(max(raw_shares, 5.05))).quantize(Decimal("0.01"), rounding=ROUND_UP))
            if shares < 5.0:
                print(f"🔥 Buy aborted: too few shares ({shares}) – market requires min 5.")
                return ""

            order_args = OrderArgs(price=limit_price, size=shares, side="BUY", token_id=target_token_id)
            max_buy_attempts = 5
            for buy_attempt in range(max_buy_attempts):
                try:
                    signed_order = self.client.create_order(order_args)
                    signed_order.funder = self.proxy_address
                    resp = self.client.post_order(signed_order, OrderType.GTC)
                    if not resp or not isinstance(resp, dict):
                        print(f"🔥 Buy failed: no response from API (post_order).")
                        return ""
                    if not resp.get("success"):
                        err = resp.get("errorMsg") or resp.get("message") or resp.get("error") or str(resp)[:200]
                        print(f"🔥 Buy rejected by CLOB: {err}")
                        return ""
                    break
                except PolyApiException as e:
                    err_str = str(e).lower()
                    is_transient = "service not ready" in err_str or "too early" in err_str or "request exception" in err_str
                    if is_transient and buy_attempt < max_buy_attempts - 1:
                        wait = 3 * (buy_attempt + 1)
                        print(f"   ⏳ CLOB transient error – retry {buy_attempt + 2}/{max_buy_attempts} in {wait}s…")
                        time.sleep(wait)
                        continue
                    print(f"🔥 Buy error: {e}")
                    return ""
            else:
                print(f"🔥 Buy failed after {max_buy_attempts} attempts.")
                return ""
            order_id = resp.get("orderID")
            time.sleep(4)
            for attempt in range(10):
                status_resp = self.client.get_order(order_id)
                if not status_resp:
                    time.sleep(3)
                    continue
                status = str(status_resp.get("status", "")).lower()
                if status in ["filled", "matched"]:
                    return order_id
                if status in ["live", "open", "pending"]:
                    time.sleep(3)
                    continue
                print(f"🔥 Buy: unexpected order status {status} for {order_id}. Response: {status_resp}")
                return ""
            print(f"🔥 Buy: order {order_id} still live after 30s – not counting as filled.")
            return ""
        except Exception as e:
            print(f"🔥 Critical error in execute_market_order: {e}")
            import traceback
            traceback.print_exc()
            return "" 
        finally:
            if original_post: http_helpers.post = original_post

    def execute_sell_order(self, token_id: str, amount: float, price: float):
        with self._order_lock:
            return self._execute_sell_order_unsafe(token_id, amount, price)

    def _execute_sell_order_unsafe(self, token_id: str, amount: float, price: float):
        original_post = http_helpers.post
        try:
            if float(amount) < 5.0: return "BALANCE_ERROR"
            # Update CLOB balance/allowance for outcome token (required for sell)
            try:
                self.client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id))
            except Exception as e:
                print(f"⚠️ update_balance_allowance (token {str(token_id)[:12]}…): {e}")
            def patched_post(endpoint, headers, data, **kwargs):
                if endpoint.endswith("/orders") and isinstance(data, dict):
                    order_part = data.get("order", {})
                    if order_part:
                        for k, v in order_part.items():
                            if k in ["feeRateBps", "side", "signatureType"]: order_part[k] = int(v) if not hasattr(v, 'value') else int(v.value)
                            else: order_part[k] = str(v) if not hasattr(v, 'value') else str(v.value)
                return original_post(endpoint, headers, data, **kwargs)
            http_helpers.post = patched_post

            try: tick_size = self.get_tick_size(token_id)
            except Exception: tick_size = 0.001
            
            valid_price = math.floor(price / tick_size) * tick_size
            valid_size = float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))

            # Cancel only orders for this token, not other positions' sell orders
            try:
                self.client.cancel_market_orders(asset_id=token_id)
            except Exception:
                pass

            if valid_price < 0.01:
                print(f"🔥 Sell aborted: invalid price after tick (token {token_id[:12]}…): {valid_price}")
                return False
            order_args = OrderArgs(price=valid_price, size=valid_size, side="SELL", token_id=token_id)
            max_sell_attempts = 5
            last_poly_exc = None
            for sell_attempt in range(max_sell_attempts):
                try:
                    signed_order = self.client.create_order(order_args)
                    signed_order.funder = self.proxy_address
                    resp = self.client.post_order(signed_order, OrderType.GTC)
                    if not resp or not isinstance(resp, dict):
                        print(f"🔥 Sell failed: no response from API (post_order).")
                        return False
                    if not resp.get("success"):
                        err = resp.get("errorMsg") or resp.get("message") or resp.get("error") or str(resp)[:200]
                        print(f"🔥 Sell rejected by CLOB: {err}")
                        return False
                    order_id = resp.get("orderID")
                    time.sleep(2)
                    status_resp = self.client.get_order(order_id)
                    status = str(status_resp.get("status", "")).lower() if status_resp else ""
                    if status == "filled":
                        return True
                    if status in ["live", "open", "pending"]:
                        return True  # Order in book, counted as success
                    return True  # Has order_id = accepted
                except PolyApiException as e:
                    last_poly_exc = e
                    err_str = str(e).lower()
                    if "not enough balance" in err_str or "allowance" in err_str:
                        print(f"🔥 Sell aborted (token {str(token_id)[:12]}…): insufficient balance/allowance.")
                        return False
                    is_transient = (
                        "service not ready" in err_str
                        or "too early" in err_str
                        or "request exception" in err_str
                        or (e.__cause__ and type(e.__cause__).__name__ in (
                            "RemoteProtocolError", "TimeoutException", "ConnectError",
                            "ReadTimeout", "WriteTimeout", "PoolTimeout",
                        ))
                    )
                    if is_transient and sell_attempt < max_sell_attempts - 1:
                        wait = 3 * (sell_attempt + 1)
                        print(f"   ⏳ CLOB transient error – retry {sell_attempt + 2}/{max_sell_attempts} in {wait}s…")
                        time.sleep(wait)
                        continue
                    print(f"🔥 Sell error (token {str(token_id)[:12]}…): {e}")
                    return False
            if last_poly_exc:
                print(f"🔥 Sell error after {max_sell_attempts} attempts (token {str(token_id)[:12]}…): {last_poly_exc}")
            return False
        except PolyApiException as e:
            err_str = str(e).lower()
            if "not enough balance" in err_str or "allowance" in err_str:
                print(f"🔥 Sell aborted (token {str(token_id)[:12]}…): insufficient balance/allowance.")
                return False
            print(f"🔥 Sell error (token {str(token_id)[:12]}…): {e}")
            import traceback
            traceback.print_exc()
            return False
        except Exception as e:
            print(f"🔥 Sell error (token {str(token_id)[:12]}…): {e}")
            import traceback
            traceback.print_exc()
            return False
        finally: http_helpers.post = original_post

    def get_usdc_balance(self) -> float:
        try:
            balance_res = self.usdc.functions.balanceOf(
                self.web3.to_checksum_address(self.proxy_address)
            ).call()
            return float(balance_res / 1_000_000)
        except Exception:
            return 0.0

    def get_book(self, token_id: str) -> tuple:
        """Returns (best_bid, best_ask, bid_liquidity_usdc) from CLOB. bid_liquidity = sum(price*size) for bids."""
        try:
            resp = self._http.get(f"{self.clob_url}/book?token_id={token_id}", timeout=5)
            if resp.status_code != 200:
                return (None, None, 0.0)
            data = resp.json()
            bids = sorted(data.get("bids", []), key=lambda x: -float(x["price"]))
            asks = sorted(data.get("asks", []), key=lambda x: float(x["price"]))
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            bid_liquidity = sum(float(b["price"]) * float(b["size"]) for b in bids)
            return (best_bid, best_ask, bid_liquidity)
        except Exception:
            return (None, None, 0.0)

    def get_buy_price(self, token_id: str) -> float:
        try:
            resp = self._http.get(f"{self.clob_url}/book?token_id={token_id}", timeout=5)
            if resp.status_code == 200:
                asks = sorted(resp.json().get("asks", []), key=lambda x: float(x['price']))
                for ask in asks:
                    if (float(ask['price']) * float(ask['size'])) >= 2.0: return float(ask['price'])
                if asks: return float(asks[0]['price'])
            return 0.0
        except Exception:
            return 0.0

    def get_tick_size(self, token_id: str) -> float:
        try:
            resp = self._http.get(f"{self.clob_url}/markets/{token_id}", timeout=2)
            if resp.status_code == 200:
                return float(resp.json().get("minimum_tick_size", 0.001))
        except Exception:
            pass
        return 0.001

    def get_price_history(self, token_id: str, interval: str = "6h", fidelity: int = 5) -> list:
        try:
            params = {"market": token_id, "interval": interval, "fidelity": fidelity}
            res = self._http.get(f"{self.clob_url}/prices-history", params=params, timeout=10)
            if res.status_code == 200:
                return res.json().get("history", [])
            return []
        except Exception:
            return []