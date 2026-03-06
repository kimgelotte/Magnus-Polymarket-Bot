# Magnus V4 – Scanner thread with Bouncer (Option A)
# Fetches events per strategy, filters, runs Bouncer; only PASS candidates go to queue.
import json
import logging
import time
import queue
import asyncio
import threading
import datetime as dt
from typing import Dict, Tuple, Any
import os

logger = logging.getLogger("magnus.scanner")

# Root path for imports when running from different directories
import sys
from pathlib import Path
_current = Path(__file__).resolve()
_root = _current.parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


class MarketScanner(threading.Thread):
    """
    Producer thread: fetches events (per strategy), applies same filters as trade.py,
    builds candidate, runs Bouncer (Grok); only PASS candidates go to queue.
    Deduplication so same (m_id, token_id) is not re-enqueued within TTL.
    """

    def __init__(
        self,
        candidate_queue: queue.Queue,
        trade_manager: Any,
        *,
        strategies: list[str] | None = None,
        event_limit: int = 1000,
        dedup_ttl_seconds: int = 300,
        sleep_between_rounds_seconds: int = 25,
        daemon: bool = True,
    ):
        super().__init__(daemon=daemon)
        self.candidate_queue = candidate_queue
        self.trade = trade_manager
        self.strategies = strategies or ["trending"]
        self.event_limit = event_limit
        self.dedup_ttl = dedup_ttl_seconds
        self.sleep_between_rounds = sleep_between_rounds_seconds
        self._dedup: Dict[Tuple[str, str], float] = {}
        self._stop = threading.Event()
        # Verbos logg (en 🔍‑rad per event) kan slås på med MAGNUS_VERBOSE_SCANNER=1.
        self.verbose = os.getenv("MAGNUS_VERBOSE_SCANNER", "0").strip().lower() in ("1", "true", "yes")
        # Relaxed mode: släpp igenom fler kandidater till War Room (mjukare krav på likviditet och tid kvar).
        self.relaxed_filters = os.getenv("MAGNUS_RELAX_SCANNER_FILTERS", "1").strip().lower() in ("1", "true", "yes")

    def stop(self):
        self._stop.set()

    def _prune_dedup(self):
        now = time.time()
        cutoff = now - self.dedup_ttl
        to_remove = [k for k, ts in self._dedup.items() if ts < cutoff]
        for k in to_remove:
            del self._dedup[k]

    def _is_duplicate(self, m_id: str, token_id: str) -> bool:
        key = (str(m_id), str(token_id))
        now = time.time()
        if key in self._dedup and (now - self._dedup[key]) < self.dedup_ttl:
            return True
        return False

    def _mark_enqueued(self, m_id: str, token_id: str):
        self._dedup[(str(m_id), str(token_id))] = time.time()

    def _build_event_markets_overview(self, markets: list, event_title: str) -> list:
        """Get price (and spread) for all markets in event. Använder Gamma outcomePrices (0–1, som webben) när tillgängligt, annars CLOB."""
        overview = []
        for market_data in markets:
            m_id = str(market_data.get("id"))
            t_ids_raw = market_data.get("clobTokenIds")
            if not t_ids_raw:
                continue
            t_ids = json.loads(t_ids_raw) if isinstance(t_ids_raw, str) else t_ids_raw
            group_title = market_data.get("groupItemTitle") or "Yes"
            outcome_prices = market_data.get("outcomePrices")
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except Exception:
                    outcome_prices = None
            for token_idx, token_id in enumerate(t_ids):
                outcome_label = (
                    "Yes"
                    if token_idx == 0 and len(t_ids) == 2
                    else ("No" if token_idx == 1 and len(t_ids) == 2 else f"Outcome{token_idx}")
                )
                try:
                    price = None
                    if outcome_prices and token_idx < len(outcome_prices):
                        try:
                            p = outcome_prices[token_idx]
                            price = float(p) if p is not None else None
                        except (TypeError, ValueError):
                            pass
                    if price is None or price == 0:
                        price = self.trade.polymarket.get_buy_price(token_id)
                    if price and price > 1.0:
                        price = price / 100.0
                    bid, ask, _liq = self.trade.polymarket.get_book(token_id)
                    if (price is None or price == 0) and bid is not None and ask is not None:
                        price = (float(bid) + float(ask)) / 2
                        if price and price > 1.0:
                            price = price / 100.0
                    spread_pct = None
                    if bid and ask and (float(bid) + float(ask)) > 0:
                        mid = (float(bid) + float(ask)) / 2
                        spread_pct = round((float(ask) - float(bid)) / mid * 100, 1)
                    overview.append({
                        "market_id": m_id,
                        "groupItemTitle": group_title,
                        "outcome": outcome_label,
                        "token_id": str(token_id),
                        "price": round(float(price), 3) if price else 0,
                        "spread_pct": spread_pct,
                        "bid": float(bid) if bid is not None else None,
                        "ask": float(ask) if ask is not None else None,
                        "bid_liquidity": float(_liq) if _liq is not None else 0.0,
                    })
                except Exception:
                    continue
        return overview

    def _format_event_markets_for_prompt(self, overview: list, event_title: str) -> str:
        """Format event overview to text for agent prompt."""
        if not overview:
            return ""
        lines = [f"Same event («{event_title[:60]}…»):"]
        for row in overview:
            title = row.get("groupItemTitle", "?")
            outcome = row.get("outcome", "?")
            price = row.get("price")
            spread = row.get("spread_pct")
            p = f"{price:.2f}" if isinstance(price, (int, float)) else "?"
            s = f" spread {spread}%" if spread is not None else ""
            lines.append(f"  • {title} ({outcome}): {p}{s}")
        return "\n".join(lines)

    def run(self):
        while not self._stop.is_set():
            try:
                self._run_one_round()
            except Exception as e:
                print(f"\n⚠️ [Scanner] Round error: {e}")
                logger.exception("Scanner round error")
                import traceback
                traceback.print_exc()
            self._prune_dedup()
            if self._stop.wait(timeout=self.sleep_between_rounds):
                break

    def _run_one_round(self):
        for strategy in self.strategies:
            if self._stop.is_set():
                return
            try:
                events = self.trade.polymarket.get_all_events(strategy=strategy, limit=self.event_limit)
            except Exception as e:
                print(f"\n⚠️ [Scanner] get_all_events({strategy}) error: {e}")
                continue
            if not events:
                continue

            from agents.polymarket.polymarket import Polymarket
            for ev in events:
                raw_cat = ev.get("category")
                # Gamma kan returnera en lista med taggar som "category" – normalisera till label.
                if not isinstance(raw_cat, str) or raw_cat in ("", "Unknown"):
                    ev["category"] = Polymarket.extract_category(ev)

            preferred = self.trade.preferred_categories
            events.sort(key=lambda e: 0 if e.get("category", "") in preferred else 1)

            n_events = len(events)
            print(f"\n📡 SCANNER ({strategy}): {n_events} events – bearbetar…", flush=True)
            enqueued_this_round = 0
            to_bouncer = 0
            bouncer_pass_count = 0
            skip_scan = 0
            skip_price = 0
            skip_liquidity = 0
            skip_days = 0
            skip_range = 0
            skip_spread = 0
            skip_dup = 0
            skip_queue_full = 0
            for count, event in enumerate(events, 1):
                if self._stop.is_set():
                    return
                if count == n_events or (n_events <= 50 and count % 5 == 0) or (n_events > 50 and count % 20 == 0):
                    print(f"   [Scanner] {count}/{n_events} events…", flush=True)
                try:
                    e_title = event.get("title", "Untitled")
                    e_category = event.get("category") or "Unknown"
                    if not isinstance(e_category, str):
                        e_category = "Unknown"

                    markets = event.get("markets", [])
                    if not markets:
                        continue
                    # Minsta rensning på event‑nivå: skippa enbart rena "up or down"-brusmarknader.
                    if "up or down" in e_title.lower():
                        continue

                    # Håll loggen ren: standard = ingen per‑event‑rad. Sätt MAGNUS_VERBOSE_SCANNER=1 för att visa.
                    if self.verbose:
                        cat_tag = f"[{e_category}]" if e_category and e_category != "Unknown" else ""
                        print(f"   🔍 {cat_tag} {e_title[:70]}")

                    # Research: all markets in event (prices) to find best profit potential
                    event_markets_overview = self._build_event_markets_overview(markets, e_title)
                    event_markets_summary_str = self._format_event_markets_for_prompt(event_markets_overview, e_title)
                    overview_by_token = {str(r["token_id"]): r for r in event_markets_overview}
                    event_id = str(event.get("id") or "")

                    for market_data in markets:
                        if self._stop.is_set():
                            return
                        m_id = str(market_data.get("id"))
                        if not self.trade._allow_market_scan(m_id):
                            skip_scan += 1
                            continue

                        t_ids_raw = market_data.get("clobTokenIds")
                        if not t_ids_raw:
                            continue
                        t_ids = json.loads(t_ids_raw) if isinstance(t_ids_raw, str) else t_ids_raw

                        for token_idx, token_id in enumerate(t_ids):
                            if self._stop.is_set():
                                return
                            try:
                                tid_str = str(token_id)
                                if tid_str in overview_by_token:
                                    row = overview_by_token[tid_str]
                                    current_price = float(row.get("price") or 0)
                                    bid = row.get("bid")
                                    ask = row.get("ask")
                                    bid_liquidity = float(row.get("bid_liquidity") or 0)
                                    if current_price == 0 and bid is not None and ask is not None:
                                        current_price = (float(bid) + float(ask)) / 2
                                    spread_pct = row.get("spread_pct")
                                else:
                                    current_price = None
                                    op = market_data.get("outcomePrices")
                                    if isinstance(op, str):
                                        try:
                                            op = json.loads(op)
                                        except Exception:
                                            op = None
                                    if op and token_idx < len(op):
                                        try:
                                            current_price = float(op[token_idx])
                                        except (TypeError, ValueError):
                                            pass
                                    if current_price is None:
                                        current_price = self.trade.polymarket.get_buy_price(token_id)
                                    if current_price > 1.0:
                                        current_price = current_price / 100.0
                                    bid, ask, bid_liquidity = self.trade.polymarket.get_book(token_id)
                                    if current_price == 0 and bid is not None and ask is not None:
                                        mid_p = (bid + ask) / 2
                                        current_price = mid_p / 100.0 if mid_p > 1.0 else mid_p
                                    spread_pct = None
                                    if bid and ask and (bid + ask) > 0:
                                        mid = (bid + ask) / 2
                                        spread_pct = round((ask - bid) / mid * 100, 1)
                                min_p = getattr(self.trade, "min_entry_price", 0.001)
                                max_p = getattr(self.trade, "max_entry_price", 0.999)
                                if current_price < min_p or current_price > max_p:
                                    skip_price += 1
                                    if self.verbose and skip_price <= 3:
                                        print(f"      [price skip] raw={current_price} (min={min_p}, max={max_p})", flush=True)
                                    continue
                                # Liquidity filter: thin orderbook → hard to sell without slippage.
                                # I relaxed-läge: tillåt allt med någon budlikviditet (> 0); annars använd konfigurerad gräns.
                                if self.relaxed_filters:
                                    if bid_liquidity <= 0:
                                        skip_liquidity += 1
                                        continue
                                else:
                                    if bid_liquidity < self.trade.min_bid_liquidity_usdc:
                                        skip_liquidity += 1
                                        continue
                                # Spread: Quant REJECT:ar nästan alltid vid >15–25%; filtrera tidigt så vi sparar War Room-anrop
                                max_spread = getattr(self.trade, "max_spread_pct", 95.0)
                                if spread_pct is not None and spread_pct > max_spread:
                                    skip_spread += 1
                                    continue

                                history = self.trade.polymarket.get_price_history(token_id)
                                stats = self.trade.war_room._process_history(history)
                                end_date_str = event.get("endDate") or "Unknown"
                                days_until_end, price_context = self.trade._price_and_time_context(
                                    current_price, stats, end_date_str
                                )
                                # Time left – något mjukare så fler når Bouncer/Quant
                                e_title_lower = e_title.lower()
                                is_price_event = any(
                                    kw in e_title_lower
                                    for kw in ["price of", "above $", "below $", "finish week", "finish the week"]
                                ) or "$" in e_title
                                min_days = 0.8
                                if is_price_event and e_category in self.trade.high_risk_categories:
                                    min_days = 1.2
                                elif e_category in self.trade.high_risk_categories:
                                    min_days = 1.0
                                # I relaxed-läge: släpp igenom fler – minst ~0.08 dagar (~2h); annars 0.2 dagar.
                                if days_until_end is not None:
                                    if self.relaxed_filters:
                                        min_days_relaxed = 0.08
                                        try:
                                            min_days_relaxed = float(os.getenv("MAGNUS_SCANNER_MIN_DAYS_RELAXED", "0.08"))
                                        except ValueError:
                                            pass
                                        if days_until_end < min_days_relaxed:
                                            skip_days += 1
                                            continue
                                    else:
                                        if days_until_end < min_days:
                                            skip_days += 1
                                            continue
                                range_pct = price_context.get("range_pct") or 0
                                if range_pct < self.trade.min_range_pct:
                                    skip_range += 1
                                    continue
                                # Price‑zon: konfigurerbar; av med MAGNUS_SCANNER_REF_PRICE_FILTER=0 för max antal kandidater.
                                ref_price_filter_on = os.getenv("MAGNUS_SCANNER_REF_PRICE_FILTER", "1").strip().lower() in ("1", "true", "yes")
                                if ref_price_filter_on:
                                    avg_price = float(stats.get("avg") or 0.0)
                                    ref_price = avg_price if avg_price > 0 else current_price
                                    try:
                                        min_ref = float(os.getenv("MAGNUS_SCANNER_MIN_PRICE", "0.01"))
                                        max_ref = float(os.getenv("MAGNUS_SCANNER_MAX_PRICE", "0.99"))
                                    except ValueError:
                                        min_ref, max_ref = 0.01, 0.99
                                    if not (min_ref <= ref_price <= max_ref):
                                        skip_price += 1
                                        continue
                                if self.trade.min_change_1h_pct > 0:
                                    change_1h = price_context.get("change_1h")
                                    if change_1h is None or abs(float(change_1h)) < self.trade.min_change_1h_pct:
                                        continue

                                outcome_label = (
                                    "Yes"
                                    if token_idx == 0 and len(t_ids) == 2
                                    else ("No" if token_idx == 1 and len(t_ids) == 2 else f"Outcome{token_idx}")
                                )
                                full_title = (
                                    f"{e_title} [{market_data.get('groupItemTitle', 'Yes')}] ({outcome_label})"
                                )

                                market_for_ai = {
                                    "question": full_title,
                                    "category": e_category,
                                    "end_date": end_date_str,
                                    "days_until_end": days_until_end,
                                    "price_context": price_context,
                                    "rules": event.get("description", "No rules provided."),
                                    "current_price": current_price,
                                    "stats": stats,
                                    "similar_analyses": "",
                                    "spread_pct": spread_pct,
                                    "bid": bid if bid else None,
                                    "ask": ask if ask else None,
                                    "uncertain_market": self.trade.uncertain_market,
                                    "event_markets_context": event_markets_summary_str,
                                }
                                candidate = {
                                    "market_for_ai": market_for_ai,
                                    "full_title": full_title,
                                    "e_category": e_category,
                                    "current_price": current_price,
                                    "price_context": price_context,
                                    "token_id": token_id,
                                    "m_id": m_id,
                                    "market_data": market_data,
                                    "spread_pct": spread_pct,
                                    "bid": bid,
                                    "ask": ask,
                                    "end_date_str": end_date_str,
                                    "strategy": strategy,
                                    "event_id": event_id,
                                }

                                # Bouncer (Option A): only PASS candidates go to queue (kan stängas av med MAGNUS_SKIP_BOUNCER_IN_SCANNER=1)
                                to_bouncer += 1
                                if self.trade.skip_bouncer_in_scanner:
                                    bouncer_ok = True
                                    bouncer_pass_count += 1
                                else:
                                    try:
                                        bouncer_ok = asyncio.run(
                                            self.trade.war_room._grok_bouncer(
                                                full_title, end_date_str, category=e_category
                                            )
                                        )
                                    except Exception as bouncer_err:
                                        print(f"\n⚠️ [Scanner] Bouncer error for {full_title[:40]}…: {(str(bouncer_err)[:80])}")
                                        bouncer_ok = False
                                    if not bouncer_ok:
                                        print(f"      ⛔ Bouncer FAIL: {full_title[:60]}")
                                        continue
                                    bouncer_pass_count += 1

                                if self._is_duplicate(m_id, token_id):
                                    skip_dup += 1
                                    continue
                                try:
                                    self.candidate_queue.put_nowait(candidate)
                                    self._mark_enqueued(m_id, token_id)
                                    enqueued_this_round += 1
                                    print(f"      → Kö: {full_title[:58]} @ {current_price:.2f}", flush=True)
                                except queue.Full:
                                    skip_queue_full += 1

                            except Exception as tok_err:
                                # Single token errors should not stop the whole round
                                continue

                except Exception as ev_err:
                    continue

            # Formaterad sammanfattning – alltid synlig efter varje runda (flush så det inte fastnar i buffer)
            parts = []
            if skip_scan: parts.append(f"scan={skip_scan}")
            if skip_price: parts.append(f"price={skip_price}")
            if skip_liquidity: parts.append(f"liq={skip_liquidity}")
            if skip_days: parts.append(f"days={skip_days}")
            if skip_range: parts.append(f"range={skip_range}")
            if skip_spread: parts.append(f"spread={skip_spread}")
            if skip_dup: parts.append(f"dup={skip_dup}")
            if skip_queue_full: parts.append(f"full={skip_queue_full}")

            # region agent log
            try:
                import json as _json  # lokal alias för debug-loggning
                with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                    _fdbg.write(
                        _json.dumps(
                            {
                                "sessionId": "ed1d60",
                                "runId": "pre-fix",
                                "hypothesisId": "H1",
                                "location": "scanner.py:_run_one_round",
                                "message": "scanner_round_summary",
                                "data": {
                                    "strategy": strategy,
                                    "n_events": n_events,
                                    "to_bouncer": to_bouncer,
                                    "enqueued": enqueued_this_round,
                                    "skip_scan": skip_scan,
                                    "skip_price": skip_price,
                                    "skip_liquidity": skip_liquidity,
                                    "skip_days": skip_days,
                                    "skip_range": skip_range,
                                    "skip_spread": skip_spread,
                                    "skip_dup": skip_dup,
                                    "skip_queue_full": skip_queue_full,
                                },
                                "timestamp": int(time.time() * 1000),
                            }
                        )
                        + "\n"
                    )
            except Exception:
                pass
            # endregion

            _f = lambda s: print(s, flush=True)
            _f("\n" + "─" * 60)
            _f("📡 SCANNER – resultat (" + strategy + ")")
            _f("   Hittade: " + str(n_events) + " events, " + str(to_bouncer) + " token(s) passerade pre-filter.")
            if enqueued_this_round > 0:
                _f("   → Till analys (kön): " + str(enqueued_this_round) + " kandidat(er).")
            else:
                why = []
                if skip_dup: why.append("dup")
                if skip_queue_full: why.append("kön full")
                _f("   → Till analys: 0" + (" (" + ", ".join(why) + ")" if why else "") + ".")
            if parts:
                _f("   Filtrerade bort: " + ", ".join(parts))
            if to_bouncer == 0 and skip_price > 0:
                _f("   Tip: många skippade på pris – kör med MAGNUS_VERBOSE_SCANNER=1 för att se exempel, eller sätt MIN/MAX_ENTRY_PRICE i .env.")
            _f("─" * 60)
            if self._stop.is_set():
                return
            # Single pause per round happens in run() via _stop.wait() – avoid double sleep
