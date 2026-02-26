# Magnus V4 – Scanner thread with Bouncer (Option A)
# Fetches events per strategy, filters, runs Bouncer; only PASS candidates go to queue.
import json
import time
import queue
import asyncio
import threading
import datetime as dt
from typing import Dict, Tuple, Any

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
        """Get price (and spread) for all markets in event so War Room can compare and find best entry."""
        overview = []
        for market_data in markets:
            m_id = str(market_data.get("id"))
            t_ids_raw = market_data.get("clobTokenIds")
            if not t_ids_raw:
                continue
            t_ids = json.loads(t_ids_raw) if isinstance(t_ids_raw, str) else t_ids_raw
            group_title = market_data.get("groupItemTitle") or "Yes"
            for token_idx, token_id in enumerate(t_ids):
                outcome_label = (
                    "Yes"
                    if token_idx == 0 and len(t_ids) == 2
                    else ("No" if token_idx == 1 and len(t_ids) == 2 else f"Outcome{token_idx}")
                )
                try:
                    price = self.trade.polymarket.get_buy_price(token_id)
                    bid, ask, _liq = self.trade.polymarket.get_book(token_id)
                    if price == 0 and bid is not None and ask is not None:
                        price = (float(bid) + float(ask)) / 2
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
                if "category" not in ev or ev.get("category") in ("", "Unknown"):
                    ev["category"] = Polymarket.extract_category(ev)

            preferred = self.trade.preferred_categories
            events.sort(key=lambda e: 0 if e.get("category", "") in preferred else 1)

            n_events = len(events)
            print(f"\n   [Scanner] Processing {n_events} events ({strategy})…")
            enqueued_this_round = 0
            for count, event in enumerate(events, 1):
                if self._stop.is_set():
                    return
                if count % 100 == 0 or count == n_events:
                    print(f"\n   [Scanner] {count}/{n_events} events…")
                try:
                    e_title = event.get("title", "Untitled")
                    e_category = event.get("category") or "Unknown"

                    if "up or down" in e_title.lower():
                        continue
                    t_lower = e_title.lower()
                    if any(
                        term1 in t_lower and (term2 is None or term2 in t_lower)
                        for term1, term2 in self.trade.skip_title_patterns
                    ):
                        continue

                    markets = event.get("markets", [])
                    if not markets:
                        continue
                    start_str = event.get("startDate")
                    if start_str:
                        try:
                            start_time = dt.datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                            now = dt.datetime.now(dt.timezone.utc)
                            is_sport = any(
                                x in e_title.lower() for x in ["vs", "winner", "o/u", "points", "goals"]
                            )
                            if is_sport and now > start_time + dt.timedelta(hours=4):
                                continue
                        except Exception:
                            pass

                    cat_tag = f"[{e_category}]" if e_category and e_category != "Unknown" else ""
                    print(f"   🔍 {cat_tag} {e_title[:70]}")

                    # Research: all markets in event (prices) to find best profit potential
                    event_markets_overview = self._build_event_markets_overview(markets, e_title)
                    event_markets_summary_str = self._format_event_markets_for_prompt(event_markets_overview, e_title)
                    event_id = str(event.get("id") or "")

                    for market_data in markets:
                        if self._stop.is_set():
                            return
                        m_id = str(market_data.get("id"))
                        if self.trade.already_owns(m_id):
                            continue
                        if self.trade.db.has_ever_traded_market(m_id):
                            continue

                        t_ids_raw = market_data.get("clobTokenIds")
                        if not t_ids_raw:
                            continue
                        t_ids = json.loads(t_ids_raw) if isinstance(t_ids_raw, str) else t_ids_raw

                        for token_idx, token_id in enumerate(t_ids):
                            if self._stop.is_set():
                                return
                            try:
                                current_price = self.trade.polymarket.get_buy_price(token_id)
                                bid, ask, bid_liquidity = self.trade.polymarket.get_book(token_id)
                                if current_price == 0 and bid is not None and ask is not None:
                                    current_price = (bid + ask) / 2
                                if current_price < 0.10 or current_price > self.trade.max_entry_price:
                                    continue
                                # Liquidity filter: thin orderbook → hard to sell without slippage
                                if bid_liquidity < self.trade.min_bid_liquidity_usdc:
                                    continue

                                spread_pct = None
                                if bid and ask and (bid + ask) > 0:
                                    mid = (bid + ask) / 2
                                    spread_pct = round((ask - bid) / mid * 100, 1)

                                history = self.trade.polymarket.get_price_history(token_id)
                                stats = self.trade.war_room._process_history(history)
                                end_date_str = event.get("endDate") or "Unknown"
                                days_until_end, price_context = self.trade._price_and_time_context(
                                    current_price, stats, end_date_str
                                )
                                # Stricter time requirements for price/level markets (e.g. ETH/NVDA levels)
                                e_title_lower = e_title.lower()
                                is_price_event = any(
                                    kw in e_title_lower
                                    for kw in ["price of", "above $", "below $", "finish week", "finish the week"]
                                ) or "$" in e_title
                                min_days = 1.0
                                if e_category in self.trade.preferred_categories:
                                    # Sports/Elections: catalysts happen fast – allow shorter time left
                                    min_days = 0.5
                                elif is_price_event and e_category in self.trade.high_risk_categories:
                                    # Price markets in high-risk categories (incl. Geopolitics): require more time
                                    min_days = 1.5
                                elif e_category in self.trade.high_risk_categories:
                                    min_days = 1.2
                                if days_until_end is not None and days_until_end < min_days:
                                    continue
                                range_pct = price_context.get("range_pct") or 0
                                if range_pct < self.trade.min_range_pct:
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

                                # Bouncer (Option A): only PASS candidates go to queue
                                try:
                                    bouncer_ok = asyncio.run(
                                        self.trade.war_room._grok_bouncer(
                                            full_title, end_date_str, category=e_category
                                        )
                                    )
                                except Exception as bouncer_err:
                                    print(f"\n⚠️ [Scanner] Bouncer error for {full_title[:40]}…: {bouncer_err}")
                                    bouncer_ok = False
                                if not bouncer_ok:
                                    print(f"      ⛔ Bouncer FAIL: {full_title[:60]}")
                                    continue

                                if self._is_duplicate(m_id, token_id):
                                    continue
                                try:
                                    self.candidate_queue.put_nowait(candidate)
                                    self._mark_enqueued(m_id, token_id)
                                    enqueued_this_round += 1
                                    print(f"      ✅ Bouncer PASS → queue: {full_title[:60]} @ {current_price:.2f}")
                                except queue.Full:
                                    pass  # Queue full – skip, prioritize fresh candidates (bounded 500)

                            except Exception as tok_err:
                                # Single token errors should not stop the whole round
                                continue

                except Exception as ev_err:
                    continue

            if enqueued_this_round > 0:
                print(f"\n📥 [Scanner] {strategy}: {enqueued_this_round} candidate(s) queued (Bouncer PASS).")
            else:
                print(f"\n📋 [Scanner] {strategy}: round done, 0 candidates queued (all filtered or Bouncer FAIL).")
            if self._stop.is_set():
                return
            # Single pause per round happens in run() via _stop.wait() – avoid double sleep
