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
    PartialCreateOrderOptions,
)


load_dotenv()


class OrphanPositionError(Exception):
    """Sell misslyckades med 'not enough balance/allowance' – position kan vara orphan (DB har den men CLOB ser inte token)."""


# Verifiering: logga exakt vad som skickas till CLOB när MAGNUS_VERIFY_CLOB_PAYLOAD=1
_VERIFY_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "verify-clob-payload.jsonl")


def _capture_and_log_clob_post(endpoint: str, headers, data):
    """Loggar CLOB POST för verifiering – anropas vid /order-anrop."""
    import json as _json
    try:
        h = dict(headers) if headers else {}
        poly_addr = h.get("POLY_ADDRESS", "?")
        body_str = data if isinstance(data, str) else _json.dumps(data) if data else ""
        maker = signer = sig_type = "?"
        try:
            parsed = _json.loads(body_str) if body_str else {}
            o = parsed.get("order") or {}
            maker = o.get("maker", "?")
            signer = o.get("signer", "?")
            sig_type = o.get("signatureType", "?")
        except Exception:
            pass
        msg = f"[CLOB VERIFY] POLY_ADDRESS={poly_addr} | maker={maker} | signer={signer} | signatureType={sig_type}"
        print(msg)
        logger.info("CLOB payload: %s", msg)
        try:
            with open(_VERIFY_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(_json.dumps({"ts": time.time(), "endpoint": endpoint, "POLY_ADDRESS": poly_addr, "maker": maker, "signer": signer, "signatureType": sig_type}, ensure_ascii=False) + "\n")
        except Exception:
            pass
    except Exception:
        pass


def _install_clob_verify_patch() -> None:
    """Patchar CLOB POST så vi loggar /order-anrop – client importerar post, så vi patchar client-modulen."""
    from py_clob_client import client as _client_mod
    _orig_post = _client_mod.post

    def _logged_post(endpoint: str, headers=None, data=None):
        if "/order" in endpoint:
            _capture_and_log_clob_post(endpoint, headers, data)
        return _orig_post(endpoint, headers, data)

    _client_mod.post = _logged_post


# Kör efter ClobClient-import
_install_clob_verify_patch()


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
    DATA_API_POSITIONS_URL = "https://data-api.polymarket.com/positions"
    GAMMA_EVENTS_ENDPOINT = "https://gamma-api.polymarket.com/events"
    GAMMA_MARKETS_ENDPOINT = "https://gamma-api.polymarket.com/markets"
    GAMMA_PUBLIC_PROFILE_URL = "https://gamma-api.polymarket.com/public-profile"
    # On‑chain USDC.e (Polymarket collateral) på Polygon
    USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

    @staticmethod
    def get_proxy_funder_from_api(wallet_address: str) -> Optional[str]:
        """
        Hämtar proxy/funder-adress för en wallet via Gamma API (public-profile).
        Använd EOA-adressen (t.ex. från PRIVATE_KEY); svaret innehåller proxyWallet som är
        den adress Polymarket använder för deposit/saldo (samma som på polymarket.com/settings).
        Returnerar None om profilen saknas eller inte har proxyWallet.
        """
        if not wallet_address or not str(wallet_address).strip().startswith("0x"):
            return None
        addr = str(wallet_address).strip()
        try:
            r = httpx.get(
                Polymarket.GAMMA_PUBLIC_PROFILE_URL,
                params={"address": addr},
                timeout=10.0,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            proxy = data.get("proxyWallet") or data.get("proxy_wallet")
            if proxy and isinstance(proxy, str) and proxy.strip().startswith("0x"):
                return proxy.strip()
            return None
        except Exception:
            return None

    def __init__(self) -> None:
        private_key = os.getenv("PRIVATE_KEY", "").strip()
        if not private_key:
            raise RuntimeError("PRIVATE_KEY saknas i .env – kan inte initiera Polymarket‑klient.")

        # Default 2 = GNOSIS_SAFE (MetaMask + deposit) – vanligast; 0 = ren EOA.
        signature_type = int(os.getenv("POLYGON_SIGNATURE_TYPE", "2"))
        funder_address = os.getenv("POLYMARKET_FUNDER_ADDRESS", "").strip() or None

        # EOA (type 0): maker = signer = adress från PRIVATE_KEY. Proxy (type 1/2): maker = funder (proxy), signer = EOA.
        # Ordern signeras alltid med PRIVATE_KEY (EOA); SDK sätter maker=funder, signer=EOA, signatureType i OrderData.
        # Det krävs INGEN extra signatur: en EIP-712-order signeras av EOA; signatureType 2 säger bara att maker är en Safe.
        if signature_type == 0:
            from eth_account import Account
            funder_address = Account.from_key(private_key).address
        elif signature_type in (1, 2):
            if not (funder_address and funder_address.startswith("0x")):
                # Försök hämta proxy (funder) från Gamma API – samma som visad på polymarket.com/settings.
                from eth_account import Account
                eoa = Account.from_key(private_key).address
                funder_address = Polymarket.get_proxy_funder_from_api(eoa)
                if funder_address:
                    try:
                        print(f"[Polymarket] Proxy (funder) hämtad från API: {funder_address[:10]}…{funder_address[-6:]}")
                    except Exception:
                        pass
                if not (funder_address and funder_address.startswith("0x")):
                    raise RuntimeError(
                        "POLYMARKET_FUNDER_ADDRESS krävs för signature type 1/2 (proxy), eller så hittades ingen "
                        "proxy för din EOA i Polymarket. Logga in på polymarket.com och gör en deposit så skapas "
                        "proxy; sätt sedan POLYMARKET_FUNDER_ADDRESS (finns på polymarket.com/settings)."
                    )

        self.client = ClobClient(
            self.CLOB_HOST,
            chain_id=137,
            key=private_key,
            signature_type=signature_type,
            funder=funder_address,
        )

        # För proxy (type 1/2): balance-endpoint kräver POLY_ADDRESS=funder; post_order kräver POLY_ADDRESS=signer
        # (API-nyckeln är bunden till signer/EOA – docs: "POLY_ADDRESS = Polygon signer address").
        # Vi patchar endast under get_usdc_balance; post_order använder default (signer).
        self._l2_funder_for_balance = (funder_address if signature_type in (1, 2) and funder_address else None)

        # L2‑autentisering mot CLOB:
        # 1) Om POLYMARKET_FORCE_NEW_API_KEY=1 – skapa ny API‑nyckel med nonce från chain (test mot invalid signature).
        # 2) Om USER_API_* finns i .env – använd dem direkt.
        # 3) Annars: skapa/derivera API‑creds från PRIVATE_KEY (standardvägen).
        self.api_creds: Optional[ApiCreds] = None

        force_new = os.getenv("POLYMARKET_FORCE_NEW_API_KEY", "").strip() in ("1", "true", "yes")
        user_key = os.getenv("USER_API_KEY", "").strip()
        user_secret = os.getenv("USER_API_SECRET", "").strip()
        user_pass = os.getenv("USER_API_PASSPHRASE", "").strip()

        try:
            if force_new:
                # Ny nonce från chain → create_api_key ger färska credentials (GitHub #79).
                from eth_account import Account
                eoa = Account.from_key(private_key).address
                rpc = os.getenv("POLYGON_CONFIG_MAINNET_RPC_URL", "").strip()
                nonce = 0
                if rpc:
                    try:
                        payload = {
                            "jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionCount",
                            "params": [eoa, "latest"],
                        }
                        r = httpx.post(rpc, json=payload, timeout=10.0)
                        res = r.json().get("result")
                        if isinstance(res, str) and res.startswith("0x"):
                            nonce = int(res, 16)
                    except Exception:
                        pass
                creds = self.client.create_api_key(nonce)
                if isinstance(creds, ApiCreds):
                    self.client.set_api_creds(creds)
                    self.api_creds = creds
                    print(
                        f"[Polymarket] NY API‑nyckel skapad (nonce={nonce}). Kopiera till .env:\n"
                        f"  USER_API_KEY={getattr(creds, 'api_key', '')}\n"
                        f"  USER_API_SECRET={getattr(creds, 'api_secret', '')}\n"
                        f"  USER_API_PASSPHRASE={getattr(creds, 'api_passphrase', '')}\n"
                        f"Ta bort POLYMARKET_FORCE_NEW_API_KEY efter att du sparat nycklarna."
                    )
            elif user_key and user_secret and user_pass:
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
            pass

        # Kortlivad cache för price/book/history (minskar CLOB-anrop inom samma runda).
        self._cache_ttl = float(os.getenv("MAGNUS_CACHE_TTL_SECONDS", "45"))
        self._cache_price: Dict[str, Tuple[float, float]] = {}
        self._cache_book: Dict[str, Tuple[Tuple[Optional[float], Optional[float], float], float]] = {}
        self._cache_history: Dict[str, Tuple[List[Dict[str, Any]], float]] = {}
        self._cache_lock = threading.Lock()
        # Vid get_balance_allowance-fel använd senast lyckade saldo så vi inte visar 0 och pausar onödigt.
        self._last_balance: Optional[float] = None

        # CLOB heartbeat: utan heartbeat inom ~10s avbryts alla öppna ordrar (Polymarket docs).
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None

    def _heartbeat_loop(self) -> None:
        """Bakgrundstråd: skickar heartbeat var 5:e sekund så GTC-ordrar inte avbryts.
        Vid 400 Invalid Heartbeat ID: servern returnerar rätt ID – extrahera och retry. Annars reset till ""."""
        heartbeat_id = ""
        ok_count = 0
        last_400_id: Optional[str] = None
        while not self._heartbeat_stop.wait(timeout=5.0):
            try:
                resp = self.client.post_heartbeat(heartbeat_id)
                if isinstance(resp, dict):
                    new_id = resp.get("heartbeat_id")
                    if new_id is not None:
                        heartbeat_id = str(new_id)
                    ok_count += 1
                    if ok_count > 0 and ok_count % 12 == 0:
                        logger.debug("CLOB heartbeat OK (%d)", ok_count)
            except Exception as e:
                # Polymarket docs: vid 400 "Invalid Heartbeat ID" – använd rätt ID från svar eller starta om med ""
                try:
                    from py_clob_client.exceptions import PolyApiException
                    if isinstance(e, PolyApiException):
                        if getattr(e, "status_code", None) == 400:
                            err = getattr(e, "error_msg", None)
                            if isinstance(err, dict) and "Invalid Heartbeat ID" in str(err.get("error", "")):
                                correct_id = err.get("heartbeat_id")
                                if correct_id and correct_id != last_400_id:
                                    heartbeat_id = str(correct_id)
                                    last_400_id = correct_id
                                    logger.debug("Heartbeat: använder ID från 400-svar.")
                                else:
                                    heartbeat_id = ""
                                    last_400_id = None
                                    logger.debug("Heartbeat: startar om med tom ID.")
                                continue
                        # status_code=None = nätverksfel (Request exception!) – behåll ID, retry nästa runda
                        if getattr(e, "status_code", None) is None:
                            logger.debug("Heartbeat: tillfälligt nätverksfel, retry om 5s.")
                            continue
                except Exception:
                    pass
                logger.warning("CLOB heartbeat misslyckades: %s", e)
                ok_count = 0
                heartbeat_id = ""
                last_400_id = None

    def start_heartbeat(self) -> None:
        """Startar heartbeat-tråd – krävs för att GTC-ordrar ska ligga kvar på boken."""
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        logger.info("CLOB heartbeat startad – GTC-ordrar hålls levande.")
        print("[Polymarket] Heartbeat aktiv – öppna ordrar hålls kvar på boken.")

    def stop_heartbeat(self) -> None:
        """Stoppar heartbeat-tråden."""
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=6.0)
            self._heartbeat_thread = None

    def get_open_orders(self, asset_id: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """Hämtar öppna ordrar från CLOB. asset_id = token_id för att filtrera.
        För proxy (type 2) patchar vi POLY_ADDRESS till funder så vi får ordrar för rätt konto (samma som polymarket.com visar).
        Returnerar None vid fel (t.ex. Request exception) – anroparen ska då inte anta has_sell=False."""
        for attempt in range(3):
            try:
                from py_clob_client.clob_types import OpenOrderParams
                from py_clob_client.headers import headers as _l2_headers
                params = OpenOrderParams(asset_id=asset_id) if asset_id else None
                orig_l2 = _l2_headers.create_level_2_headers
                if self._l2_funder_for_balance:
                    def _l2_with_funder(signer, creds, request_args):
                        h = orig_l2(signer, creds, request_args)
                        h[_l2_headers.POLY_ADDRESS] = self._l2_funder_for_balance
                        return h
                    _l2_headers.create_level_2_headers = _l2_with_funder
                try:
                    return self.client.get_orders(params) or []
                finally:
                    if self._l2_funder_for_balance:
                        _l2_headers.create_level_2_headers = orig_l2
            except Exception as e:
                err_str = str(e).lower()
                is_retryable = "request exception" in err_str or "timeout" in err_str or "connection" in err_str
                logger.warning("get_open_orders failed (attempt %d/3): %s", attempt + 1, e)
                if attempt < 2 and is_retryable:
                    time.sleep(1.0 + attempt)
                else:
                    return None
        return None

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
          - 'trending'     → order=volume_24hr (redan populära – ofta pumpade)
          - 'featured'     → featured=true (curated)
          - 'new'          → order=id (nyaste först)
          - 'liquid'       → order=liquidity (mest likviditet – lättare att köpa, mindre slippage)
          - 'undiscovered' → order=volume_24hr ascending (lägst volym – mindre upptäckta, kan ha edge)
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
        elif s == "liquid":
            params["order"] = "liquidity"
            params["ascending"] = "false"
        elif s == "undiscovered":
            params["order"] = "volume_24hr"
            params["ascending"] = "true"

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

    def get_market_info_by_token_id(self, token_id: str) -> Optional[Dict[str, Any]]:
        """
        Hittar marknadsinfo för ett token_id via Gamma API.
        Itererar events (som innehåller markets) tills token_id matchar clobTokenIds.
        Returnerar dict med market_id, question, groupItemTitle, outcome, end_date_iso, category, event_id.
        """
        import json as _json
        token_str = str(token_id)
        # Först: prova markets-endpoint med paginering (snabbare för aktiva marknader)
        try:
            for offset in range(0, 5000, 100):
                resp = httpx.get(
                    self.GAMMA_MARKETS_ENDPOINT,
                    params={"active": "true", "closed": "false", "limit": "100", "offset": str(offset)},
                    timeout=15.0,
                )
                if resp.status_code != 200:
                    break
                batch = resp.json()
                if not isinstance(batch, list) or not batch:
                    break
                for m in batch:
                    t_ids_raw = m.get("clobTokenIds")
                    if not t_ids_raw:
                        continue
                    t_ids = _json.loads(t_ids_raw) if isinstance(t_ids_raw, str) else (t_ids_raw if isinstance(t_ids_raw, list) else [])
                    if token_str not in [str(t) for t in t_ids]:
                        continue
                    token_idx = next((i for i, t in enumerate(t_ids) if str(t) == token_str), 0)
                    outcomes_raw = m.get("outcomes") or "[\"Yes\", \"No\"]"
                    outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or ["Yes", "No"])
                    outcome = outcomes[token_idx] if token_idx < len(outcomes) else ("Yes" if token_idx == 0 else "No")
                    events_arr = m.get("events") or []
                    ev = events_arr[0] if events_arr else {}
                    return {
                        "market_id": str(m.get("id", "")),
                        "question": str(m.get("question", "")),
                        "groupItemTitle": str(m.get("groupItemTitle") or outcome),
                        "outcome": outcome,
                        "end_date_iso": m.get("endDate") or ev.get("endDate") or "",
                        "category": self.extract_category(m) or self.extract_category(ev) or "Unknown",
                        "event_id": str(ev.get("id", "")),
                    }
                if len(batch) < 100:
                    break
        except Exception as e:
            logger.warning("get_market_info_by_token_id markets: %s", str(e)[:80])
        # Fallback: iterera events (inkluderar stängda marknader)
        try:
            for offset in range(0, 3000, 100):
                resp = httpx.get(
                    self.GAMMA_EVENTS_ENDPOINT,
                    params={"limit": "100", "offset": str(offset)},
                    timeout=15.0,
                )
                if resp.status_code != 200:
                    break
                events = resp.json()
                if not isinstance(events, list) or not events:
                    break
                for ev in events:
                    for m in (ev.get("markets") or []):
                        t_ids_raw = m.get("clobTokenIds")
                        if not t_ids_raw:
                            continue
                        t_ids = _json.loads(t_ids_raw) if isinstance(t_ids_raw, str) else (t_ids_raw if isinstance(t_ids_raw, list) else [])
                        if token_str not in [str(t) for t in t_ids]:
                            continue
                        token_idx = next((i for i, t in enumerate(t_ids) if str(t) == token_str), 0)
                        outcomes_raw = m.get("outcomes") or "[\"Yes\", \"No\"]"
                        outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or ["Yes", "No"])
                        outcome = outcomes[token_idx] if token_idx < len(outcomes) else ("Yes" if token_idx == 0 else "No")
                        return {
                            "market_id": str(m.get("id", "")),
                            "question": str(m.get("question", "")),
                            "groupItemTitle": str(m.get("groupItemTitle") or outcome),
                            "outcome": outcome,
                            "end_date_iso": m.get("endDate") or ev.get("endDate") or "",
                            "category": self.extract_category(ev) or self.extract_category(m) or "Unknown",
                            "event_id": str(ev.get("id", "")),
                        }
                if len(events) < 100:
                    break
        except Exception as e:
            logger.warning("get_market_info_by_token_id events: %s", str(e)[:80])
        return None

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
        För proxy (type 1/2) patchar vi POLY_ADDRESS till funder endast här så post_order använder signer.
        """
        from py_clob_client.headers import headers as _l2_headers
        orig_l2 = _l2_headers.create_level_2_headers
        if self._l2_funder_for_balance:
            def _l2_with_funder(signer, creds, request_args):
                h = orig_l2(signer, creds, request_args)
                h[_l2_headers.POLY_ADDRESS] = self._l2_funder_for_balance
                return h
            _l2_headers.create_level_2_headers = _l2_with_funder
        try:
            sig_type = int(os.getenv("POLYGON_SIGNATURE_TYPE", "2"))
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
            data = self.client.get_balance_allowance(params)
        except Exception as e:
            if self._last_balance is not None:
                return float(self._last_balance)
            logger.warning("get_balance_allowance error (no cache): %s", e)
            return 0.0
        finally:
            if self._l2_funder_for_balance:
                _l2_headers.create_level_2_headers = orig_l2

        if isinstance(data, dict):
            onchain, addr = self._get_onchain_usdce_balance()
            bal = data.get("balance") or data.get("collateral") or 0
            try:
                # CLOB returnerar USDC‑saldo i "wei" (base units, 10^6).
                # Konvertera till riktiga USDC.e‑enheter så allt internt är i samma skala.
                clob_raw = float(bal)
                clob_balance = clob_raw / 1_000_000.0
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

    def _get_positions_from_data_api(self) -> List[Dict[str, Any]]:
        """
        Hämtar positioner via Data API (kräver ingen auth).
        Används som fallback för proxy-wallets där CLOB get_positions returnerar tomt.
        Returnerar full metadata: asset (token_id), size, title, avgPrice, endDate, eventId, outcome, etc.
        """
        addr = self._l2_funder_for_balance or os.getenv("POLYMARKET_FUNDER_ADDRESS", "").strip()
        if not addr or not addr.startswith("0x"):
            return []
        try:
            resp = httpx.get(
                self.DATA_API_POSITIONS_URL,
                params={"user": addr, "limit": "500"},
                timeout=15.0,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning("Data API positions: %s", str(e)[:80])
            return []

    def get_positions_with_metadata(self) -> List[Dict[str, Any]]:
        """
        Returnerar positioner med full metadata (title, avgPrice, endDate, etc.).
        För proxy: använder Data API som har allt. För EOA: CLOB + tom metadata (kräver get_market_info_by_token_id).
        """
        if self._l2_funder_for_balance:
            return self._get_positions_from_data_api()
        positions = []
        try:
            raw = self.client.get_positions() or []
            for pos in raw:
                asset = pos.get("asset") or {}
                tid = str(asset.get("token_id") or pos.get("token_id") or "")
                size = float(pos.get("size") or pos.get("balance") or 0)
                if tid:
                    positions.append({"asset": tid, "size": size})
        except Exception:
            pass
        return positions

    def get_all_token_balances(self) -> Dict[str, float]:
        """
        Hämtar alla positioner en gång och returnerar token_id -> balance.
        För proxy: CLOB get_positions returnerar ofta tomt – fallback till Data API.
        """
        out: Dict[str, float] = {}
        positions: List[Dict[str, Any]] = []

        # 1. Försök CLOB (fungerar för EOA)
        from py_clob_client.headers import headers as _l2_headers
        orig_l2 = _l2_headers.create_level_2_headers
        if self._l2_funder_for_balance:
            def _l2_with_funder(signer, creds, request_args):
                h = orig_l2(signer, creds, request_args)
                h[_l2_headers.POLY_ADDRESS] = self._l2_funder_for_balance
                return h
            _l2_headers.create_level_2_headers = _l2_with_funder
        try:
            positions = self.client.get_positions() or []
        except Exception:
            pass
        finally:
            if self._l2_funder_for_balance:
                _l2_headers.create_level_2_headers = orig_l2

        # 2. Fallback: Data API för proxy när CLOB returnerar tomt
        if not positions and self._l2_funder_for_balance:
            positions = self._get_positions_from_data_api()
            # Data API: asset = token_id (str), size = shares
            for pos in positions:
                try:
                    tid = str(pos.get("asset") or "")
                    size = pos.get("size") or 0
                    if tid:
                        out[tid] = float(size)
                except (TypeError, ValueError):
                    continue
            return out

        for pos in positions:
            try:
                asset = pos.get("asset") or {}
                tid = str(asset.get("token_id") or pos.get("token_id") or "")
                size = pos.get("size") or pos.get("balance") or 0
                if tid:
                    out[tid] = float(size)
            except (TypeError, ValueError):
                continue
        return out

    def get_token_balance(self, token_id: str) -> float:
        """
        Returnerar antal shares för givet token_id.
        För proxy använder get_all_token_balances (som har Data API-fallback).
        """
        if self._l2_funder_for_balance:
            return self.get_all_token_balances().get(str(token_id), 0.0)
        from py_clob_client.headers import headers as _l2_headers
        orig_l2 = _l2_headers.create_level_2_headers
        try:
            positions = self.client.get_positions() or []
        except Exception:
            return 0.0
        for pos in positions:
            try:
                asset = pos.get("asset") or {}
                tid = str(asset.get("token_id") or pos.get("token_id") or "")
                if tid == str(token_id):
                    return float(pos.get("size") or pos.get("balance") or 0)
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

    # Giltiga tick sizes enligt https://docs.polymarket.com/trading/orders/create (Order Options)
    _VALID_TICK_SIZES = ("0.1", "0.01", "0.001", "0.0001")

    def _get_order_options(self, token_id: str, condition_id: Optional[str] = None) -> PartialCreateOrderOptions:
        """
        Hämtar tick_size och neg_risk för ordrar (enligt docs: options krävs för create_order/create_market_order).
        Försöker get_market(condition_id) om condition_id finns, annars get_tick_size/get_neg_risk per token_id.
        tick_size normaliseras till ett av API:ets tillåtna värden (0.1, 0.01, 0.001, 0.0001).
        """
        def _norm_tick(s: str) -> str:
            if s in self._VALID_TICK_SIZES:
                return s
            try:
                f = float(s)
                for v in ("0.0001", "0.001", "0.01", "0.1"):
                    if f >= float(v):
                        return v
            except (TypeError, ValueError):
                pass
            return "0.01"

        try:
            if condition_id:
                market = self.client.get_market(condition_id)
                if isinstance(market, dict):
                    raw = market.get("minimum_tick_size", "0.01")
                    tick_size = _norm_tick(str(raw))
                    neg_risk = bool(market.get("neg_risk", False))
                    return PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        except Exception:
            pass
        tick_size = self.client.get_tick_size(token_id)
        neg_risk = self.client.get_neg_risk(token_id)
        ts = str(tick_size) if tick_size else "0.01"
        return PartialCreateOrderOptions(tick_size=_norm_tick(ts), neg_risk=bool(neg_risk))

    def execute_market_order(self, market_to_buy, amount_usdc: float, max_price: Optional[float] = None) -> Optional[str]:
        """
        Lägger en market‑BUY order i USDC på active_token_id.
        Returnerar orderId vid OK, annars None.

        API: https://docs.polymarket.com/trading/orders/create
        - När det finns ask‑likviditet: FOK BUY via create_market_order(amount=USDC, price=worst‑price) + post_order(..., OrderType.FOK).
        - När det saknas ask: limit BUY via create_order(OrderArgs(price, size=shares)) + post_order(..., OrderType.GTC).
        Options (tick_size, neg_risk) hämtas per marknad och skickas till create_order/create_market_order.
        Signering: SDK bygger OrderData (maker=funder, signer=EOA) och signerar med PRIVATE_KEY.
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

                # Enligt https://docs.polymarket.com/trading/orders/create: market BUY = amount (USDC), price = worst-price limit (slippage).
                # post_order måste få OrderType.FOK för FOK-orders. Skicka tick_size + neg_risk i options.
                worst_price = float(max_price) if max_price is not None and max_price > 0 else (float(best_ask) if best_ask and best_ask > 0 else 0.99)
                args = MarketOrderArgs(
                    token_id=token_id,
                    amount=effective_amount,
                    side="BUY",
                    order_type=OrderType.FOK,
                    price=worst_price,
                )
                options = self._get_order_options(token_id, getattr(market_to_buy, "conditionId", None))
                fok_last_err: Optional[Exception] = None
                for _attempt in range(3):
                    try:
                        order = self.client.create_market_order(args, options)
                        res = self.client.post_order(order, OrderType.FOK)
                        break
                    except Exception as e:
                        fok_last_err = e
                        _err_str = str(e)
                        if _attempt < 2 and ("Request exception" in _err_str or "Server disconnected" in _err_str or "RemoteProtocolError" in _err_str):
                            time.sleep(2.0 + _attempt)
                            continue
                        raise
                else:
                    if fok_last_err is not None:
                        raise fok_last_err
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
                    if "FOK" in err_str.upper() or "NOT_FILLED" in err_str.upper():
                        print(f"⚠️ [Polymarket] FOK misslyckad – ingen likviditet till vårt maxpris. Best ask för högt?")
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
                    # API-svaret använder "orderID" (docs.polymarket.com/trading/orders/create)
                    order_id = res.get("orderID") or res.get("orderId") or res.get("order_id")
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

            # Fall 2: ingen ask‑likviditet → maker BUY (GTC). MAGNUS_BUY_FOK_ONLY=1: skippa, köp ENDAST vid FOK.
            fok_only = os.getenv("MAGNUS_BUY_FOK_ONLY", "1").strip().lower() in ("1", "true", "yes")
            if ask_liq <= 0:
                if fok_only:
                    print(f"ℹ️ [Polymarket] No ask liquidity – skippar (MAGNUS_BUY_FOK_ONLY=1). Endast FOK-köp, ingen GTC.")
                    return None
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
                    options = self._get_order_options(token_id, getattr(market_to_buy, "conditionId", None))
                    last_err: Optional[Exception] = None
                    for attempt in range(3):
                        try:
                            signed = self.client.create_order(order_args, options)
                            res = self.client.post_order(signed, OrderType.GTC)
                            break
                        except Exception as e:
                            last_err = e
                            err_str = str(e)
                            if attempt < 2 and ("Request exception" in err_str or "Server disconnected" in err_str or "RemoteProtocolError" in err_str):
                                time.sleep(2.0 + attempt)
                                continue
                            raise
                    else:
                        if last_err is not None:
                            raise last_err
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
                        # API-svaret använder "orderID" (docs.polymarket.com/trading/orders/create)
                        order_id = res.get("orderID") or res.get("orderId") or res.get("order_id") or res.get("id")
                    if not order_id:
                        print(f"⚠️ [Polymarket] post_order (maker BUY) unexpected response for token {token_id}: {(str(res)[:200])!r}")
                        return None
                    print(
                        f"ℹ️ [Polymarket] Placed maker BUY (GTC) for token {token_id} at {limit_price:.3f} "
                        f"for ~{est_shares:.2f} shares."
                    )
                    # Verifiera att ordern syns i CLOB (heartbeat håller den levande)
                    try:
                        open_orders = self.get_open_orders(asset_id=str(token_id))
                        n = len(open_orders) if open_orders else 0
                        if n > 0:
                            maker = (open_orders[0].get("maker_address") or open_orders[0].get("maker") or "?")
                            print(f"   ✓ CLOB har {n} öppen order (maker={maker[:10]}…{maker[-6:]}) – polymarket.com/portfolio?tab=open")
                        else:
                            print(f"   ⚠️ CLOB returnerar 0 öppna ordrar – kontrollera polymarket.com. Heartbeat måste köra.")
                    except Exception:
                        pass
                    return order_id
                except Exception as e:
                    err_str = str(e)
                    logger.exception("execute_market_order maker BUY exception for token %s: %s", token_id, err_str[:200])
                    if "invalid signature" in err_str.lower():
                        print(
                            "   💡 Invalid signature: Om du använder MetaMask och har depositat: sätt POLYGON_SIGNATURE_TYPE=2 "
                            "och POLYMARKET_FUNDER_ADDRESS till proxy-adressen (polymarket.com/settings). "
                            "Om du nyligen börjat använda deposit: kör create_polymarket_api_creds.py igen för att om-derivera API-nycklar."
                        )
                    return None
        except Exception as e:
            err_str = str(e)
            logger.exception("execute_market_order exception for token %s: %s", token_id, err_str[:200])
            if "invalid signature" in err_str.lower():
                print(
                    "   💡 Invalid signature: Om du använder MetaMask och har depositat: sätt POLYGON_SIGNATURE_TYPE=2 "
                    "och POLYMARKET_FUNDER_ADDRESS till proxy-adressen (polymarket.com/settings). "
                    "Om du nyligen börjat använda deposit: kör create_polymarket_api_creds.py igen för att om-derivera API-nycklar."
                )
            return None

    def execute_sell_order(self, token_id: str, shares: float, price: float) -> bool:
        """
        Lägger en limit‑SELL order för ett token vid givet pris.
        API: create_order(OrderArgs(token_id, price, size=shares, side=SELL), options) + post_order(..., OrderType.GTC).
        För proxy (type 1/2): CLOB kollar balance mot POLY_ADDRESS – tokenen ligger hos funder.
        Vi patchar POLY_ADDRESS till funder (samma som get_open_orders, get_usdc_balance) så balance-check lyckas.
        """
        try:
            limit_price = float(price)
            size = float(shares)
            if limit_price <= 0 or size <= 0:
                logger.warning("execute_sell_order: invalid price=%s or size=%s", limit_price, size)
                return False
            order_args = OrderArgs(
                token_id=str(token_id),
                price=limit_price,
                size=size,
                side="SELL",
            )
            options = self._get_order_options(str(token_id))
            signed = self.client.create_order(order_args, options)
            # Proxy: patch POLY_ADDRESS till funder så CLOB kollar balance på rätt konto (token ligger hos funder).
            from py_clob_client.headers import headers as _l2_headers
            orig_l2 = _l2_headers.create_level_2_headers
            if self._l2_funder_for_balance:
                def _l2_with_funder(signer, creds, request_args):
                    h = orig_l2(signer, creds, request_args)
                    h[_l2_headers.POLY_ADDRESS] = self._l2_funder_for_balance
                    return h
                _l2_headers.create_level_2_headers = _l2_with_funder
            try:
                res = self.client.post_order(signed, OrderType.GTC)
            finally:
                if self._l2_funder_for_balance:
                    _l2_headers.create_level_2_headers = orig_l2
            if not res:
                logger.warning("execute_sell_order: post_order returned empty for token %s", token_id)
                return False
            if isinstance(res, dict) and res.get("error"):
                err = res.get("error")
                err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                logger.warning("execute_sell_order: post_order error for token %s: %s", token_id, err_msg[:120])
                return False
            return True
        except Exception as e:
            err_str = str(e).lower()
            # Orphan: endast när vi INTE använder proxy. Med proxy ger POLY_ADDRESS=EOA "not enough balance"
            # pga token ligger hos funder – då ska vi INTE markera som orphan.
            if not self._l2_funder_for_balance and ("not enough balance" in err_str or "allowance" in err_str):
                raise OrphanPositionError(f"Token {token_id}: {e}") from e
            if self._l2_funder_for_balance and ("not enough balance" in err_str or "allowance" in err_str):
                logger.warning(
                    "execute_sell_order: proxy – lägg GTC-sälj manuellt på polymarket.com"
                )
                return False  # Ingen traceback för känt proxy-fel
            logger.exception("execute_sell_order failed for token %s at %.3f: %s", token_id, float(price), str(e)[:100])
            return False

