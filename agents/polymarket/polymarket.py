import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("magnus.polymarket")
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    BookParams,
    OrderType,
    OrderArgs,
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
    # On‑chain USDC.e (Polymarket collateral) på Polygon
    USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

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

        # L2‑autentisering mot CLOB:
        # 1) Om USER_API_* finns i .env – använd dem direkt (ingen L1‑derivering behövs).
        # 2) Annars: skapa/derivera API‑creds från PRIVATE_KEY (standardvägen).
        self.api_creds: Optional[ApiCreds] = None

        user_key = os.getenv("USER_API_KEY", "").strip()
        user_secret = os.getenv("USER_API_SECRET", "").strip()
        user_pass = os.getenv("USER_API_PASSPHRASE", "").strip()

        try:
            if user_key and user_secret and user_pass:
                # Manuell User‑API – t.ex. om L1‑endpoints strular.
                # ApiCreds-fält enligt py_clob_client: api_key, api_secret, api_passphrase.
                manual = ApiCreds(api_key=user_key, api_secret=user_secret, api_passphrase=user_pass)
                self.client.set_api_creds(manual)
                self.api_creds = manual
                try:
                    print(f"[Polymarket] USER_API (env) aktiv – key suffix: {user_key[-4:]}")
                except Exception:
                    pass
            else:
                creds = self.client.create_or_derive_api_creds()
                if isinstance(creds, ApiCreds):
                    self.client.set_api_creds(creds)
                    self.api_creds = creds
                    try:
                        k = getattr(creds, "key", None) or getattr(creds, "api_key", None)
                        if isinstance(k, str) and k:
                            print(f"[Polymarket] USER_API (deriverad) aktiv – key suffix: {k[-4:]}")
                    except Exception:
                        pass
        except Exception:
            # Om detta misslyckas kan vi fortfarande läsa market‑data,
            # men orderläggning och balansfrågor kan ge PolyException senare.
            pass

        # Kortlivad cache för price/book/history (minskar CLOB-anrop inom samma runda).
        self._cache_ttl = float(os.getenv("MAGNUS_CACHE_TTL_SECONDS", "45"))
        self._cache_price: Dict[str, Tuple[float, float]] = {}
        self._cache_book: Dict[str, Tuple[Tuple[Optional[float], Optional[float], float], float]] = {}
        self._cache_history: Dict[str, Tuple[List[Dict[str, Any]], float]] = {}
        self._cache_lock = threading.Lock()
        # Vid get_balance_allowance-fel använd senast lyckade saldo så vi inte visar 0 och pausar onödigt.
        self._last_balance: Optional[float] = None

    def _get_cached(self, cache: Dict[str, Tuple[Any, float]], key: str) -> Any:
        with self._cache_lock:
            entry = cache.get(key)
        if not entry:
            return None
        val, expiry = entry
        if time.time() > expiry:
            with self._cache_lock:
                cache.pop(key, None)
            return None
        return val

    def _set_cached(self, cache: Dict[str, Tuple[Any, float]], key: str, val: Any) -> None:
        with self._cache_lock:
            cache[key] = (val, time.time() + self._cache_ttl)

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
        # Gamma tillåter större batch – 250 ger fler events per anrop (särskilt för trending/new)
        params: Dict[str, Any] = {
            "active": "true",
            "closed": "false",
            "limit": "250",
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

    def get_buy_price(self, token_id: str, use_cache: bool = True) -> float:
        """
        Returnerar bästa BUY‑pris (decimal 0–1) för ett token.
        Cachas kort (MAGNUS_CACHE_TTL_SECONDS) om use_cache=True.
        Sätt use_cache=False vid order för att alltid få färskt pris (undvik att skippa pga gammal cache).
        """
        key = str(token_id)
        if use_cache:
            cached = self._get_cached(self._cache_price, key)
            if cached is not None:
                return cached
        try:
            data = self.client.get_price(key, side="BUY")
        except Exception:
            return 0.0
        result = 0.0
        if isinstance(data, (int, float)):
            result = float(data)
        elif isinstance(data, dict):
            for k in ("price", "bid", "buy"):
                if k in data and data[k] is not None:
                    try:
                        result = float(data[k])
                        break
                    except (TypeError, ValueError):
                        continue
        # Om API returnerar 0–100 (cents) istället för 0–1, normalisera till 0–1
        if result > 1.0:
            result = result / 100.0
        if use_cache:
            self._set_cached(self._cache_price, key, result)
        return result

    def get_book(self, token_id: str) -> Tuple[Optional[float], Optional[float], float]:
        """
        Returnerar (bid, ask, bid_liquidity_usdc) för token_id.
        Cachas kort för att minska CLOB-anrop.
        """
        key = str(token_id)
        cached = self._get_cached(self._cache_book, key)
        if cached is not None:
            return cached
        try:
            book = self.client.get_order_book(key)
        except Exception:
            return None, None, 0.0
        bids = book.bids or []
        asks = book.asks or []
        def _norm(p):
            if p is None: return None
            v = float(p)
            return v / 100.0 if v > 1.0 else v
        best_bid = _norm(bids[0].price) if bids and bids[0].price is not None else None
        best_ask = _norm(asks[0].price) if asks and asks[0].price is not None else None
        bid_liquidity = 0.0
        for lvl in bids[:3]:
            try:
                p = float(lvl.price)
                if p > 1.0:
                    p = p / 100.0
                bid_liquidity += p * float(lvl.size)
            except (TypeError, ValueError):
                continue
        result = (best_bid, best_ask, bid_liquidity)
        self._set_cached(self._cache_book, key, result)
        return result

    def get_price_history(self, token_id: str) -> List[Dict[str, Any]]:
        """
        Hämtar riktig prishistorik från CLOB /prices-history så War Room får rätt high/low/avg.
        Fallback: om API saknar data, en punkt med nuvarande pris (så vi inte ljuger för Quant).
        Cachas enligt MAGNUS_CACHE_TTL_SECONDS.
        """
        key = str(token_id)
        cached = self._get_cached(self._cache_history, key)
        if cached is not None:
            return cached
        try:
            import time as _time
            end_ts = int(_time.time())
            start_ts = end_ts - 7 * 86400  # 7 dagar bakåt
            resp = httpx.get(
                f"{self.CLOB_HOST}/prices-history",
                params={"market": key, "interval": "1h", "startTs": start_ts, "endTs": end_ts},
                timeout=8.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                raw = (data or {}).get("history") or []
                if isinstance(raw, list) and raw:
                    result = []
                    for point in raw:
                        p = point.get("p") if isinstance(point, dict) else None
                        if p is not None:
                            try:
                                p = float(p)
                                if p > 1.0:
                                    p = p / 100.0
                                result.append({"p": p})
                            except (TypeError, ValueError):
                                continue
                    if result:
                        self._set_cached(self._cache_history, key, result)
                        return result
            # Fallback: en punkt (nuvarande pris) – Quant ska inte tro att high=low=current
            last_price = self.get_buy_price(token_id)
            if last_price <= 0:
                return []
            result = [{"p": last_price}]
            self._set_cached(self._cache_history, key, result)
            return result
        except Exception:
            last_price = self.get_buy_price(token_id)
            if last_price <= 0:
                return []
            result = [{"p": last_price}]
            self._set_cached(self._cache_history, key, result)
            return result

    # --- Balans ------------------------------------------------------------------

    def _get_onchain_usdce_balance(self) -> Tuple[Optional[float], Optional[str]]:
        """
        Läs USDC.e‑saldo on‑chain via Polygon RPC för funder‑adressen (Polymarkets Safe).
        Ger bara transparens; Magnus handlar fortfarande efter CLOB‑saldot.
        """
        rpc_url = os.getenv("POLYGON_CONFIG_MAINNET_RPC_URL", "").strip()
        addr = os.getenv("POLYMARKET_FUNDER_ADDRESS", "").strip()
        if not rpc_url or not addr or not addr.startswith("0x"):
            return None, None
        try:
            # balanceOf(address)
            method_id = "70a08231"
            padded_addr = addr.lower().replace("0x", "").rjust(64, "0")
            data = "0x" + method_id + padded_addr
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": self.USDC_E_ADDRESS, "data": data}, "latest"],
            }
            resp = httpx.post(rpc_url, json=payload, timeout=10.0)
            body = resp.json()
            result = body.get("result")
            if not isinstance(result, str) or not result.startswith("0x"):
                return None, addr
            raw = int(result, 16)
            # USDC.e har 6 decimals
            balance = raw / 1_000_000
            return float(balance), addr
        except Exception:
            return None, addr

    def get_usdc_balance(self) -> float:
        """
        Returnerar USDC‑saldo via CLOB balance/allowance‑endpoint.
        Kräver L2‑auth; vid fel returneras 0.
        """
        try:
            # Viktigt: signature_type måste matcha vår wallet‑typ (0=EOA, 1=magic/email, 2=proxy),
            # annars returnerar CLOB 0 även om saldo finns.
            sig_type = int(os.getenv("POLYGON_SIGNATURE_TYPE", "1"))
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
            data = self.client.get_balance_allowance(params)
        except Exception as e:
            # Vid fel: använd cache om vi har; logga bara när vi saknar cache (returnerar 0) så brus minskar.
            if self._last_balance is not None:
                return float(self._last_balance)
            logger.warning("get_balance_allowance error (no cache): %s", e)
            return 0.0

        if isinstance(data, dict):
            onchain, addr = self._get_onchain_usdce_balance()
            bal = data.get("balance") or data.get("collateral") or 0
            try:
                clob_balance = float(bal)
                # Fallback: om CLOB rapporterar 0 men det finns on‑chain USDC.e på funder‑adressen,
                # använd det on‑chain‑saldot som "Balance" internt så Magnus kan fortsätta arbeta.
                if clob_balance == 0.0 and onchain is not None and onchain > 0:
                    self._last_balance = float(onchain)
                    return self._last_balance
                self._last_balance = clob_balance
                return clob_balance
            except (TypeError, ValueError):
                print(f"⚠️ [Polymarket] Ogiltigt balance‑värde i svar: {(str(bal)[:80])!r}")
                return 0.0

        # Oväntat svar – logga för diagnos (trunkera så terminalen inte spammas).
        print(f"⚠️ [Polymarket] Oväntat balance‑svar: {(str(data)[:80])!r}")
        return 0.0

    def get_all_token_balances(self) -> Dict[str, float]:
        """
        Hämtar alla positioner en gång och returnerar token_id -> balance.
        Använd för att undvika upprepade get_positions()-anrop (t.ex. i manage_active_trades).
        """
        try:
            positions = self.client.get_positions()
        except Exception:
            return {}
        out: Dict[str, float] = {}
        for pos in positions or []:
            try:
                tid = str(pos.get("token_id") or "")
                if tid:
                    out[tid] = float(pos.get("balance") or 0)
            except (TypeError, ValueError):
                continue
        return out

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

    def _get_ask_liquidity_usdc(self, token_id: str, levels: int = 3) -> Tuple[float, Optional[float]]:
        """
        Grov uppskattning av hur mycket USDC vi realistiskt kan spendera direkt mot
        bokens ask-sida utan att trigga FOK-"no match", samt bästa ask-pris.

        Vi summerar pris * size för de första N ask-nivåerna och tolkar det som
        max "marknadsvärde" som faktiskt finns att slå emot just nu.
        """
        try:
            book = self.client.get_order_book(str(token_id))
        except Exception:
            return 0.0, None

        asks = getattr(book, "asks", None) or []
        total = 0.0
        best_ask: Optional[float] = None
        for idx, lvl in enumerate(asks[: max(1, int(levels))]):
            try:
                price = float(getattr(lvl, "price", 0) or 0)
                if price > 1.0:
                    price = price / 100.0
                size = float(getattr(lvl, "size", 0) or 0)
                if price <= 0 or size <= 0:
                    continue
                if idx == 0:
                    best_ask = price
                total += price * size
            except (TypeError, ValueError):
                continue
        return total, best_ask

    def execute_market_order(self, market_to_buy, amount_usdc: float, max_price: Optional[float] = None) -> Optional[str]:
        """
        Lägger en market‑BUY order i USDC på active_token_id.
        Returnerar orderId vid OK, annars None.

        Viktigt: om något går fel loggar vi orsaken så att Magnus‑loggarna
        visar *varför* ett köp inte gick igenom (t.ex. insufficient balance,
        auth‑fel eller CLOB‑validering).
        """
        token_id = str(getattr(market_to_buy, "active_token_id"))
        try:
            # FOK‑order måste kunna fylla *hela* beloppet direkt, annars returnerar CLOB
            # ett "no match" trots att det finns viss likviditet. Det är troligen det
            # du ser i loggen: vi ber om större USDC‑belopp än vad som finns på ask‑sidan.
            #
            # Lösning: clamp:a orderbeloppet mot faktisk ask‑likviditet (top N nivåer)
            # så att vi inte försöker köpa mer än som faktiskt kan fyllas.
            raw_amount = float(amount_usdc)
            ask_liq, best_ask = self._get_ask_liquidity_usdc(token_id)
            effective_amount = raw_amount
            # region agent log
            try:
                import json as _json
                with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                    _fdbg.write(
                        _json.dumps(
                            {
                                "sessionId": "ed1d60",
                                "runId": "pre-fix",
                                "hypothesisId": "H4",
                                "location": "polymarket.py:execute_market_order",
                                "message": "execute_market_order_entry",
                                "data": {
                                    "token_id": token_id,
                                    "raw_amount": raw_amount,
                                    "max_price": float(max_price) if max_price is not None else None,
                                    "ask_liq_usdc": float(ask_liq),
                                    "best_ask": float(best_ask) if best_ask else None,
                                },
                                "timestamp": int(time.time() * 1000),
                            }
                        )
                        + "\n"
                    )
            except Exception:
                pass
            # endregion

            # Fall 1: det finns asks → försök taker‑FOK mot befintlig likviditet.
            if ask_liq > 0:
                # Minimikrav: minst 1 USDC och minst 5 andelar på ask-sidan.
                min_amount = 1.0
                if best_ask and best_ask > 0:
                    min_amount = max(min_amount, best_ask * 5.0)

                # Om totala ask‑likviditeten inte ens räcker till 5 andelar → köp är omöjligt enligt våra regler.
                if ask_liq < min_amount:
                    print(
                        f"⚠️ [Polymarket] Ask liquidity {ask_liq:.4f} too low for min buy on token {token_id} "
                        f"(best_ask={best_ask}, min_amount={min_amount:.2f})."
                    )
                    # region agent log
                    try:
                        import json as _json
                        with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                            _fdbg.write(
                                _json.dumps(
                                    {
                                        "sessionId": "ed1d60",
                                        "runId": "pre-fix",
                                        "hypothesisId": "H4",
                                        "location": "polymarket.py:execute_market_order",
                                        "message": "execute_market_order_abort_low_ask_liq",
                                        "data": {"token_id": token_id, "ask_liq_usdc": float(ask_liq), "min_amount": float(min_amount), "best_ask": float(best_ask) if best_ask else None},
                                        "timestamp": int(time.time() * 1000),
                                    }
                                )
                                + "\n"
                            )
                    except Exception:
                        pass
                    # endregion
                    return None

                # Vi kan aldrig köpa mer än det som faktiskt finns på ask-sidan.
                max_safe = ask_liq
                if effective_amount > max_safe:
                    print(
                        f"ℹ️ [Polymarket] Clamping market BUY amount from {effective_amount:.2f} to "
                        f"{max_safe:.2f} USDC based on available ask liquidity."
                    )
                    effective_amount = max_safe

                if effective_amount < min_amount:
                    # Vår planerade size är mindre än vad som krävs för 5 andelar / 1 USDC – justera upp till min_amount
                    # om det ryms inom ask_liq.
                    if max_safe >= min_amount:
                        print(
                            f"ℹ️ [Polymarket] Raising market BUY amount from {effective_amount:.2f} to "
                            f"{min_amount:.2f} USDC to satisfy 5-shares/1-USDC constraint."
                        )
                        effective_amount = min_amount
                    else:
                        print(
                            f"⚠️ [Polymarket] Cannot satisfy min buy constraints for token {token_id} "
                            f"(effective_amount={effective_amount:.2f}, min_amount={min_amount:.2f}, ask_liq={ask_liq:.4f})."
                        )
                        return None

                args = MarketOrderArgs(
                    token_id=token_id,
                    amount=effective_amount,
                    side="BUY",
                    order_type=OrderType.FOK,
                )
                order = self.client.create_market_order(args)
                res = self.client.post_order(order)
                if not res:
                    print(f"⚠️ [Polymarket] post_order returned empty response for token {token_id} (amount {effective_amount}).")
                    # region agent log
                    try:
                        import json as _json
                        with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                            _fdbg.write(
                                _json.dumps(
                                    {
                                        "sessionId": "ed1d60",
                                        "runId": "pre-fix",
                                        "hypothesisId": "H4",
                                        "location": "polymarket.py:execute_market_order",
                                        "message": "execute_market_order_post_order_empty",
                                        "data": {"token_id": token_id, "effective_amount": float(effective_amount)},
                                        "timestamp": int(time.time() * 1000),
                                    }
                                )
                                + "\n"
                            )
                    except Exception:
                        pass
                    # endregion
                    return None
                if isinstance(res, dict) and res.get("error"):
                    err = res.get("error")
                    err_str = (err.get("message") if isinstance(err, dict) else str(err)) if err is not None else "Unknown error"
                    print(f"⚠️ [Polymarket] post_order error for token {token_id}: {err_str[:160]}")
                    # region agent log
                    try:
                        import json as _json
                        with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                            _fdbg.write(
                                _json.dumps(
                                    {
                                        "sessionId": "ed1d60",
                                        "runId": "pre-fix",
                                        "hypothesisId": "H4",
                                        "location": "polymarket.py:execute_market_order",
                                        "message": "execute_market_order_post_order_error",
                                        "data": {"token_id": token_id, "error": err_str[:200]},
                                        "timestamp": int(time.time() * 1000),
                                    }
                                )
                                + "\n"
                            )
                    except Exception:
                        pass
                    # endregion
                    return None
                order_id = None
                if isinstance(res, dict):
                    order_id = res.get("orderId") or res.get("order_id")
                if not order_id:
                    # Oväntad struktur – logga trunkerat svar.
                    print(f"⚠️ [Polymarket] post_order unexpected response for token {token_id}: {(str(res)[:200])!r}")
                    # region agent log
                    try:
                        import json as _json
                        with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                            _fdbg.write(
                                _json.dumps(
                                    {
                                        "sessionId": "ed1d60",
                                        "runId": "pre-fix",
                                        "hypothesisId": "H4",
                                        "location": "polymarket.py:execute_market_order",
                                        "message": "execute_market_order_unexpected_response",
                                        "data": {"token_id": token_id, "res": str(res)[:220]},
                                        "timestamp": int(time.time() * 1000),
                                    }
                                )
                                + "\n"
                            )
                    except Exception:
                        pass
                    # endregion
                    return None
                # region agent log
                try:
                    import json as _json
                    with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                        _fdbg.write(
                            _json.dumps(
                                {
                                    "sessionId": "ed1d60",
                                    "runId": "pre-fix",
                                    "hypothesisId": "H4",
                                    "location": "polymarket.py:execute_market_order",
                                    "message": "execute_market_order_success",
                                    "data": {"token_id": token_id, "order_id": str(order_id)},
                                    "timestamp": int(time.time() * 1000),
                                }
                            )
                            + "\n"
                        )
                except Exception:
                    pass
                # endregion
                return order_id

            # Fall 2: ingen ask‑likviditet → agera maker med limit‑BUY (GTC) om vi har ett takpris.
            if ask_liq <= 0:
                if max_price is None or max_price <= 0:
                    print(f"⚠️ [Polymarket] No ask liquidity and no MAX_PRICE for token {token_id}; skipping buy.")
                    return None

                # Minsta belopp: 1 USDC (Polymarket-krav på orderstorlek).
                if raw_amount < 1.0:
                    print(f"⚠️ [Polymarket] Amount {raw_amount:.2f} < 1.0 USDC for maker BUY on token {token_id}; skipping.")
                    return None

                # Vi vill både:
                #  - hålla oss under vårt takpris (max_price),
                #  - och kunna köpa minst 5 andelar med det belopp vi tänker riskera.
                #
                # För att göra det sätter vi ett initialt tak på priset,
                # och om det inte räcker till 5 shares justerar vi priset nedåt tills 5 shares blir möjliga.
                limit_price_cap = float(max(0.01, min(max_price, 0.99)))
                # Pris som gör att vi precis får 5 shares med raw_amount.
                price_for_five = raw_amount / 5.0
                # Slutligt limitpris = min(takkap, pris-för-5-shares)
                limit_price = max(0.01, min(limit_price_cap, price_for_five))
                est_shares = raw_amount / limit_price if limit_price > 0 else 0.0
                # region agent log
                try:
                    import json as _json
                    with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                        _fdbg.write(
                            _json.dumps(
                                {
                                    "sessionId": "ed1d60",
                                    "runId": "pre-fix",
                                    "hypothesisId": "H5",
                                    "location": "polymarket.py:execute_market_order",
                                    "message": "maker_price_and_size",
                                    "data": {
                                        "token_id": token_id,
                                        "raw_amount": float(raw_amount),
                                        "limit_price_cap": float(limit_price_cap),
                                        "price_for_five": float(price_for_five),
                                        "limit_price": float(limit_price),
                                        "est_shares": float(est_shares),
                                    },
                                    "timestamp": int(time.time() * 1000),
                                }
                            )
                            + "\n"
                        )
                except Exception:
                    pass
                # endregion
                if est_shares < 5.0:
                    print(
                        f"⚠️ [Polymarket] Even after price adjust, estimated shares {est_shares:.2f} < 5 for maker BUY on token {token_id} "
                        f"(amount={raw_amount:.2f}, price={limit_price:.3f}); skipping."
                    )
                    # region agent log
                    try:
                        import json as _json
                        with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                            _fdbg.write(
                                _json.dumps(
                                    {
                                        "sessionId": "ed1d60",
                                        "runId": "pre-fix",
                                        "hypothesisId": "H5",
                                        "location": "polymarket.py:execute_market_order",
                                        "message": "maker_est_shares_too_low",
                                        "data": {
                                            "token_id": token_id,
                                            "raw_amount": float(raw_amount),
                                            "limit_price": float(limit_price),
                                            "est_shares": float(est_shares),
                                        },
                                        "timestamp": int(time.time() * 1000),
                                    }
                                )
                                + "\n"
                            )
                    except Exception:
                        pass
                    # endregion
                    return None

                try:
                    order_args = OrderArgs(
                        token_id=token_id,
                        price=limit_price,
                        size=est_shares,
                        side="BUY",
                    )
                    signed = self.client.create_order(order_args)
                    res = self.client.post_order(signed, OrderType.GTC)
                    if not res:
                        print(f"⚠️ [Polymarket] post_order (maker BUY) returned empty response for token {token_id}.")
                        # region agent log
                        try:
                            import json as _json
                            with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                                _fdbg.write(
                                    _json.dumps(
                                        {
                                            "sessionId": "ed1d60",
                                            "runId": "pre-fix",
                                            "hypothesisId": "H5",
                                            "location": "polymarket.py:execute_market_order",
                                            "message": "maker_post_order_empty",
                                            "data": {
                                                "token_id": token_id,
                                                "limit_price": float(limit_price),
                                                "est_shares": float(est_shares),
                                            },
                                            "timestamp": int(time.time() * 1000),
                                        }
                                    )
                                    + "\n"
                                )
                        except Exception:
                            pass
                        # endregion
                        return None
                    if isinstance(res, dict) and res.get("error"):
                        err = res.get("error")
                        err_str = (err.get("message") if isinstance(err, dict) else str(err)) if err is not None else "Unknown error"
                        print(f"⚠️ [Polymarket] post_order (maker BUY) error for token {token_id}: {err_str[:160]}")
                        # region agent log
                        try:
                            import json as _json
                            with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                                _fdbg.write(
                                    _json.dumps(
                                        {
                                            "sessionId": "ed1d60",
                                            "runId": "pre-fix",
                                            "hypothesisId": "H5",
                                            "location": "polymarket.py:execute_market_order",
                                            "message": "maker_post_order_error",
                                            "data": {
                                                "token_id": token_id,
                                                "error": err_str[:200],
                                            },
                                            "timestamp": int(time.time() * 1000),
                                        }
                                    )
                                    + "\n"
                                )
                        except Exception:
                            pass
                        # endregion
                        return None
                    order_id = None
                    if isinstance(res, dict):
                        order_id = res.get("orderId") or res.get("order_id") or res.get("id")
                    if not order_id:
                        print(f"⚠️ [Polymarket] post_order (maker BUY) unexpected response for token {token_id}: {(str(res)[:200])!r}")
                        return None
                    print(
                        f"ℹ️ [Polymarket] Placed maker BUY (GTC) for token {token_id} at {limit_price:.3f} "
                        f"for ~{est_shares:.2f} shares."
                    )
                    return order_id
                except Exception as e:
                    logger.exception("execute_market_order maker BUY exception for token %s: %s", token_id, str(e)[:200])
                    return None
        except Exception as e:
            logger.exception("execute_market_order exception for token %s: %s", token_id, str(e)[:200])
            return None

    def execute_sell_order(self, token_id: str, shares: float, price: float) -> bool:
        """
        Lägger en limit‑SELL order för ett token vid givet pris.
        """
        try:
            # Limit‑SELL via OrderArgs → GTC‑order som vilar i boken tills matchad.
            limit_price = float(price)
            size = float(shares)
            if limit_price <= 0 or size <= 0:
                return False
            order_args = OrderArgs(
                token_id=str(token_id),
                price=limit_price,
                size=size,
                side="SELL",
            )
            signed = self.client.create_order(order_args)
            res = self.client.post_order(signed, OrderType.GTC)
            if not res:
                return False
            if isinstance(res, dict) and res.get("error"):
                return False
            return True
        except Exception:
            return False

