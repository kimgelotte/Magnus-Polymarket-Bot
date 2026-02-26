import sys
import os
import time
import json
import math
import queue
import traceback
import warnings
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from dotenv import load_dotenv, find_dotenv

# --- PATH FIX ---
current_file_path = Path(__file__).resolve()
root_path = current_file_path.parents[2] 
if str(root_path) not in sys.path:
    sys.path.append(str(root_path))

load_dotenv(find_dotenv(), override=True)

# Suppress false RuntimeWarning when passing coroutines to asyncio.gather
warnings.filterwarnings("ignore", message=".*was never awaited", category=RuntimeWarning)

from agents.db_manager import DatabaseManager
from agents.war_room import MagnusWarRoom
from agents.risk_manager import RiskManager
from agents.polymarket.polymarket import Polymarket
from agents.observer import MagnusObserver
from agents.application.scanner import MarketScanner
from agents.logging_config import setup_logging
from agents.portfolio_risk import PortfolioRiskManager
from agents.dynamic_target import compute_dynamic_target

import logging
logger = logging.getLogger("magnus.trade")
setup_logging()

class Trade:
    def __init__(self) -> None:
        self.db = DatabaseManager()
        self.war_room = MagnusWarRoom()
        self.risk = RiskManager()
        self.polymarket = Polymarket()
        self.portfolio_risk = PortfolioRiskManager(self.db, self.polymarket)
        
        # Core: buy cheap, sell high. Only buy when price < our value.
        self.profit_target = 0.07       # Baseline 7%; raised to 10% for cheap buys
        self.profit_target_high = 0.10  # Used when fill < price_high_threshold
        self.price_high_threshold = 0.30  # If fill < 0.30 use profit_target_high (larger upside)
        self.min_edge_to_enter = 0.03   # Min 3 cent edge (was 2)
        self.max_open_positions = 15
        self.max_bet_usdc = 100.0       # Smaller bets for diversification
        self.stop_loss_pct = 0.20
        self.max_entry_price = 0.75     # Don't buy above 0.75 (was 0.95)
        # Markets we historically lose on – skip (title match)
        self.skip_title_patterns = [
            ("elon musk", "tweet"),
            ("bitcoin", None),
        ]
        # Buy only when price below avg or in lower half of range
        self.require_below_avg = True
        self.allow_at_avg_if_hype_min = 8   # If hype_score >= this, allow buy at "near avg" too (0 = off)
        self.min_range_pct = max(self.profit_target, self.profit_target_high) * 100
        self.min_change_1h_pct = 0  # Skip if |1h change| < this % (0 = off)
        self.active_observer = None
        # Balanced events (sport): max 1 buy per event; others (ETH levels): multiple allowed
        self.balanced_event_categories = ("Sports", "Crypto", "Earnings")
        self.max_positions_per_event = 2
        self.high_risk_categories = ("Crypto", "Business", "Tech", "Economics", "Geopolitics")
        self.preferred_categories = ("Sports", "Elections", "Politics")
        # Min bid liquidity for buy allowed
        try:
            self.min_bid_liquidity_usdc = float(os.getenv("MAGNUS_MIN_BID_LIQUIDITY", "20.0"))
        except ValueError:
            self.min_bid_liquidity_usdc = 20.0
        # Min hold time before stop-loss activates (hours)
        try:
            self.min_hold_hours_before_sl = float(os.getenv("MAGNUS_MIN_HOLD_HOURS", "2.0"))
        except ValueError:
            self.min_hold_hours_before_sl = 2.0

        # Uncertain market: defensive regime (MAGNUS_UNCERTAIN_MARKET=1)
        self.uncertain_market = os.getenv("MAGNUS_UNCERTAIN_MARKET", "0").strip().lower() in ("1", "true", "yes")
        if self.uncertain_market:
            self.min_edge_to_enter = 0.035
            self.max_bet_usdc = 100.0
            self.max_open_positions = 10
            self.stop_loss_pct = 0.20

        # Exit shadow mode: recovery heuristics (logging only, no order impact)
        self.exit_shadow_mode = os.getenv("MAGNUS_EXIT_SHADOW_MODE", "1").strip().lower() in ("1", "true", "yes")
        try:
            self.recovery_high_min_days = float(os.getenv("MAGNUS_RECOVERY_HIGH_MIN_DAYS", "1.0"))
        except ValueError:
            self.recovery_high_min_days = 1.0
        try:
            self.recovery_high_min_range_pct = float(os.getenv("MAGNUS_RECOVERY_HIGH_MIN_RANGE", "15"))
        except ValueError:
            self.recovery_high_min_range_pct = 15.0
        try:
            self.recovery_low_max_days = float(os.getenv("MAGNUS_RECOVERY_LOW_MAX_DAYS", "0.5"))
        except ValueError:
            self.recovery_low_max_days = 0.5
        try:
            self.recovery_low_max_range_pct = float(os.getenv("MAGNUS_RECOVERY_LOW_MAX_RANGE", "10"))
        except ValueError:
            self.recovery_low_max_range_pct = 10.0

    def _log_to_live(self, msg):
        try:
            with open("magnus_live.log", "a", encoding="utf-8") as f:
                f.write(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}\n")
                f.flush()
        except Exception:
            pass

    def _price_and_time_context(self, current_price: float, stats: dict, end_date_str: str):
        """Compute time to end and price context (vs avg, range, historical low)."""
        days_until_end = None
        try:
            end_str = (end_date_str or "").replace("Z", "+00:00")
            if "+" in end_str or end_str.endswith("00:00"):
                end_dt = dt.datetime.fromisoformat(end_str)
            else:
                end_dt = dt.datetime.fromisoformat(end_str + "+00:00")
            now = dt.datetime.now(dt.timezone.utc)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=dt.timezone.utc)
            delta = end_dt - now
            days_until_end = max(0, round(delta.total_seconds() / 86400, 1))
        except Exception:
            pass

        high = float(stats.get("high") or 0)
        low = float(stats.get("low") or 0)
        avg = float(stats.get("avg") or 0)
        change_1h = float(stats.get("change_1h") or 0)
        p = current_price

        price_vs_avg = "unknown"
        if avg > 0:
            if p < avg * 0.92:
                price_vs_avg = "below average"
            elif p > avg * 1.08:
                price_vs_avg = "above average"
            else:
                price_vs_avg = "near average"

        mid = (high + low) / 2 if (high or low) else 0
        in_lower_half = (mid > 0 and p <= mid) or (low > 0 and p <= low * 1.08)
        near_historical_low = low > 0 and p <= low * 1.05
        range_pct = round((high - low) / avg * 100, 1) if avg > 0 and (high - low) > 0 else 0

        price_context = {
            "price_vs_avg": price_vs_avg,
            "in_lower_half": in_lower_half,
            "near_historical_low": near_historical_low,
            "range_pct": range_pct,
            "high": high,
            "low": low,
            "avg": avg,
            "change_1h": change_1h,
        }
        return days_until_end, price_context

    def _compute_recovery_potential(self, buy_price: float, current_price: float, stats: dict, end_date_str: str):
        """Heuristic: chance price can bounce given volatility and time left. Shadow mode only (logging)."""
        try:
            days_until_end, price_context = self._price_and_time_context(current_price, stats or {}, end_date_str or "")
        except Exception:
            days_until_end, price_context = None, stats or {}

        range_pct = float(price_context.get("range_pct") or 0)
        in_lower_half = bool(price_context.get("in_lower_half"))
        near_low = bool(price_context.get("near_historical_low"))

        potential = "MEDIUM"
        reason_parts = []

        if days_until_end is not None:
            # High potential: enough time, clear range, in lower half
            if (
                days_until_end > self.recovery_high_min_days
                and range_pct >= self.recovery_high_min_range_pct
                and (in_lower_half or near_low)
            ):
                potential = "HIGH"
                reason_parts.append(f"days_left>{self.recovery_high_min_days}")
                reason_parts.append(f"range>={self.recovery_high_min_range_pct}")
                if in_lower_half:
                    reason_parts.append("in_lower_half")
                if near_low:
                    reason_parts.append("near_historical_low")

            # Low potential: little time left, low range, below buy but not near low
            elif (
                days_until_end < self.recovery_low_max_days
                and range_pct <= self.recovery_low_max_range_pct
                and current_price < buy_price
                and not near_low
            ):
                potential = "LOW"
                reason_parts.append(f"days_left<{self.recovery_low_max_days}")
                reason_parts.append(f"range<={self.recovery_low_max_range_pct}")
                reason_parts.append("below_buy_not_near_low")

        meta = {
            "days_until_end": days_until_end,
            "price_context": price_context,
            "range_pct": range_pct,
            "in_lower_half": in_lower_half,
            "near_historical_low": near_low,
            "reason": ", ".join(reason_parts) if reason_parts else "",
        }
        return potential, meta

    def manage_active_trades(self):
        """Manage closing of finished trades. Sync observer to DB."""
        try:
            trades = self.db.get_open_positions()
            if self.active_observer:
                self.active_observer.sync_from_db()
            if not trades:
                return

            print(f"🧹 Verifying {len(trades)} positions on-chain...")
            for i, t in enumerate(trades):
                t_id = t['token_id']
                sys.stdout.write(f"\r   [{i+1}/{len(trades)}] Checking: {t['question'][:30]}...")
                sys.stdout.flush()

                actual_balance = self.polymarket.get_token_balance(t_id)
                if actual_balance < 0.01:
                    buy_price = float(t.get('buy_price') or 0)
                    target_price = float(t.get('target_price') or 0)
                    status = "CLOSED_PROFIT" if target_price >= buy_price * 1.01 else "CLOSED_LOSS"
                    self.db.update_trade_status(t_id, status, "Balance zero (sold via GTC)")
                    if self.active_observer:
                        self.active_observer.remove_token(t_id)
                    icon = "✅" if status == "CLOSED_PROFIT" else "❌"
                    print(f"\n{icon} Closed trade ({status}): {t['question'][:30]}")
                    continue

                # Trade age for arming delay
                trade_age_hours = None
                ts_str = (t.get("timestamp") or "").strip()
                if ts_str:
                    try:
                        opened = dt.datetime.strptime(ts_str.split(".", 1)[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
                        trade_age_hours = max(0.0, (dt.datetime.now(dt.timezone.utc) - opened).total_seconds() / 3600.0)
                    except Exception:
                        trade_age_hours = None

                # Sell price (bid) / buy price (ask) – stop-loss uses bid
                buy_price = float(t.get('buy_price') or 0)
                bid, ask, _bid_liq = self.polymarket.get_book(t_id) if actual_balance >= 5.0 else (None, None, 0.0)
                current_bid = float(bid) if bid is not None else None
                current_ask = float(ask) if ask is not None else (self.polymarket.get_buy_price(t_id) if actual_balance >= 5.0 else 0)
                price_for_sell_check = current_bid if current_bid is not None and current_bid > 0 else (current_ask if isinstance(current_ask, (int, float)) and current_ask > 0 else None)

                # Stop-loss: if bid < buy - stop_loss_pct, place sell at stop level
                young_trade = trade_age_hours is not None and trade_age_hours < self.min_hold_hours_before_sl
                if self.stop_loss_pct > 0 and buy_price > 0 and actual_balance >= 5.0 and price_for_sell_check is not None and not young_trade:
                    try:
                        threshold = buy_price * (1 - self.stop_loss_pct)
                        if price_for_sell_check < threshold:
                            stop_price = round(threshold, 3)
                            if stop_price >= 0.01:
                                print(f"\n🛑 Stop-loss: {t['question'][:35]}… at {stop_price:.2f} (bid {price_for_sell_check:.3f} < threshold {threshold:.3f})")
                                ok = self.polymarket.execute_sell_order(t_id, actual_balance, stop_price)
                                if ok:
                                    self.db.update_trade_status(t_id, "CLOSED_LOSS", f"Stop-loss at {stop_price:.3f}")
                    except Exception:
                        pass

                # Time-based exit: if < 2 days left and price flat, sell at break-even
                end_date_iso = (t.get("end_date_iso") or "").strip()
                _ask = current_ask if isinstance(current_ask, (int, float)) else 0
                if end_date_iso and buy_price > 0 and actual_balance >= 5.0 and _ask > 0:
                    try:
                        end_str = end_date_iso.replace("Z", "+00:00")
                        if "+" not in end_str and not end_str.endswith("00:00"):
                            end_str = end_str + "+00:00"
                        end_dt = dt.datetime.fromisoformat(end_str)
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=dt.timezone.utc)
                        delta = end_dt - dt.datetime.now(dt.timezone.utc)
                        days_until_end = max(0, delta.total_seconds() / 86400)
                        if days_until_end < 2 and _ask < buy_price * 1.02:
                            be_price = round(buy_price + 0.01, 3)
                            be_price = min(be_price, 0.99)
                            print(f"\n⏱️ Time-based exit (< 2d left, price below buy+2%): {t['question'][:35]}… selling at {be_price:.2f}")
                            self.polymarket.execute_sell_order(t_id, actual_balance, be_price)
                    except Exception:
                        pass

                # Shadow mode: simulate smarter exit (no actual orders)
                if self.exit_shadow_mode and buy_price > 0 and actual_balance >= 5.0:
                    try:
                        # Use same price as stop-loss check (bid or ask)
                        shadow_price = price_for_sell_check if isinstance(price_for_sell_check, (int, float)) and price_for_sell_check > 0 else _ask
                        if not shadow_price or shadow_price <= 0:
                            continue

                        history = self.polymarket.get_price_history(t_id)
                        stats = self.war_room._process_history(history)
                        potential, meta = self._compute_recovery_potential(
                            buy_price=buy_price,
                            current_price=shadow_price,
                            stats=stats,
                            end_date_str=end_date_iso,
                        )
                        days_left = meta.get("days_until_end")
                        range_pct = meta.get("range_pct")
                        reason = meta.get("reason") or "no specific rule"

                    except Exception:
                        pass

                if t.get('selling_in_progress') == 1 and t.get('order_active_in_book') == 0:
                    self.db.set_selling_flags(t_id, False, False)
            print()
        except Exception as e:
            print(f"\n⚠️ Trade management error: {e}")

    def already_owns(self, market_id: str) -> bool:
        trades = self.db.get_open_positions()
        return any(str(t['market_id']) == str(market_id) for t in trades)

    def already_has_position_in_event(self, event_id: str) -> bool:
        """True if we already have open position in this event."""
        if not event_id or not str(event_id).strip():
            return False
        trades = self.db.get_open_positions()
        return any(str(t.get("event_id") or "").strip() == str(event_id).strip() for t in trades)

    def count_open_positions_in_event(self, event_id: str) -> int:
        """Count of open positions in this event."""
        if not event_id or not str(event_id).strip():
            return 0
        trades = self.db.get_open_positions()
        return sum(1 for t in trades if str(t.get("event_id") or "").strip() == str(event_id).strip())

    def _allow_more_positions_in_event(self, event_id: str, category: str) -> bool:
        """False if we have enough positions in this event (balanced=max 1, others=max N)."""
        if not event_id or not str(event_id).strip():
            return True
        cat = (category or "").strip()
        if cat in self.balanced_event_categories:
            if self.already_has_position_in_event(event_id):
                return False  # Sport: max 1 per event
        else:
            if self.count_open_positions_in_event(event_id) >= self.max_positions_per_event:
                return False  # Others: max N per event
        return True

    def run_sniper_loop(self):
        print("🚀 Magnus V4 Sniper Mode [WAR ROOM ACTIVE]...")

        try:
            from agents.dashboard import start_dashboard_background
            start_dashboard_background(db=self.db, polymarket=self.polymarket)
        except Exception:
            pass

        import asyncio
        # Persistent event loop avoids "no current event loop" in thread
        try:
            _loop = asyncio.get_event_loop()
            if _loop.is_closed():
                _loop = asyncio.new_event_loop()
                asyncio.set_event_loop(_loop)
        except RuntimeError:
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)

        # 1. Start real-time monitoring
        open_trades = self.db.get_open_positions()
        token_ids = [str(t["token_id"]) for t in open_trades if t.get("token_id")]
        self.active_observer = MagnusObserver(token_ids, self)
        self.active_observer.start()

        # 2. Scanner queue (Bouncer in scanner – only PASS in queue)
        candidate_queue = queue.Queue(maxsize=500)
        scanner = MarketScanner(
            candidate_queue,
            self,
            strategies=["trending"],
            event_limit=1500,
            dedup_ttl_seconds=300,
            sleep_between_rounds_seconds=25,
        )
        scanner.start()
        print("📡 Scanner thread started (Bouncer in scanner).")

        while True:
            try:
                balance = self.polymarket.get_usdc_balance()
                print(f"\n💵 Balance: {balance:.2f} USDC")
                self._log_to_live(f"💓 Heartbeat | Balance: {balance:.2f} USDC")
                self.portfolio_risk.log_balance(balance)

                should_pause, drawdown = self.portfolio_risk.check_drawdown(balance)
                if should_pause:
                    print(f"🛑 Portfolio drawdown {drawdown}% exceeds limit. Pausing new trades for 5 min...")
                    self._log_to_live(f"🛑 Drawdown {drawdown}% — pausing new trades")
                    self.manage_active_trades()
                    time.sleep(300)
                    continue
                
                self.manage_active_trades()
                
                if balance < 2.0: 
                    print(f"💤 Balance < 2.0 USDC. Waiting 2 min...")
                    time.sleep(120); continue

                def run_batch(batch_list, balance, skip_bouncer=False):
                    """Run War Room for batch; returns (new_balance, skip_rest). skip_bouncer=True for scanner candidates."""
                    skip_rest = False
                    payloads = [c["market_for_ai"] for c in batch_list]
                    try:
                        if len(payloads) == 1:
                            raw = _loop.run_until_complete(self.war_room.evaluate_market(payloads[0], skip_bouncer=skip_bouncer))
                            results = [raw]
                        else:
                            tasks = [self.war_room.evaluate_market(m, skip_bouncer=skip_bouncer) for m in payloads]
                            results = _loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                    except Exception as war_err:
                        print(f"\n⚠️ War Room error (batch): {war_err}")
                        traceback.print_exc()
                        results = [{"action": "REJECT", "reason": f"Error: {war_err}", "max_price": 0.0, "hype_score": 0} for _ in batch_list]
                    for c, raw in zip(batch_list, results):
                        decision = raw if isinstance(raw, dict) else {"action": "REJECT", "reason": f"Error: {raw}", "max_price": 0.0, "hype_score": 0}
                        full_title = c["full_title"]
                        e_category = c["e_category"]
                        current_price = c["current_price"]
                        price_context = c["price_context"]
                        token_id = c["token_id"]
                        m_id = c["m_id"]
                        market_data = c["market_data"]
                        spread_pct = c["spread_pct"]
                        bid, ask = c["bid"], c["ask"]
                        end_date_str = c["end_date_str"]
                        self.db.log_analysis(
                            question=full_title, category=e_category,
                            action=decision.get("action", "REJECT"), reason=decision.get("reason", ""),
                            max_price=decision.get("max_price", 0), current_price=current_price,
                            hype_score=int(decision.get("hype_score", 0)),
                        )
                        if decision.get('action') == "BUY":
                            # Balanced events (sport): max 1 buy; others (ETH levels): max N
                            event_id = (c.get("event_id") or "").strip()
                            if event_id and not self._allow_more_positions_in_event(event_id, e_category):
                                n = self.count_open_positions_in_event(event_id)
                                if (e_category or "").strip() in self.balanced_event_categories:
                                    print(f"   ⏸️ Already have position in this event (balanced, max 1). Skipping buy.")
                                else:
                                    print(f"   ⏸️ Already {n} position(s) in this event (max {self.max_positions_per_event}). Skipping buy.")
                                continue
                            if self.portfolio_risk.check_correlation(full_title, e_category):
                                print(f"   ⏸️ Too many correlated positions in {e_category}. Skipping buy.")
                                continue
                            days_until_end = c.get("market_for_ai", {}).get("days_until_end")
                            if days_until_end is not None and isinstance(days_until_end, (int, float)) and days_until_end < 1.0:
                                print(f"   ⏸️ Too little time left (< 1 day). Skipping buy.")
                                continue
                            open_count = len(self.db.get_open_positions())
                            if open_count >= self.max_open_positions:
                                print(f"   ⏸️ Max open positions ({self.max_open_positions}). Skipping new buy.")
                                continue
                            is_price_market = False
                            if self.require_below_avg:
                                in_lower = price_context.get("in_lower_half")
                                under_avg = price_context.get("price_vs_avg") == "below average"
                                hype = int(decision.get("hype_score") or 0)
                                is_preferred = e_category in self.preferred_categories
                                hype_threshold = max(self.allow_at_avg_if_hype_min - 2, 1) if is_preferred else self.allow_at_avg_if_hype_min
                                allow_exception = hype_threshold and hype >= hype_threshold
                                full_title_lower = (full_title or "").lower()
                                is_price_market = any(
                                    kw in full_title_lower
                                    for kw in ["price of", "above $", "below $", "finish week", "finish the week"]
                                ) or "$" in (full_title or "")
                                if is_price_market and e_category in self.high_risk_categories:
                                    if not in_lower:
                                        print(f"   ⏸️ Skipping buy (price-market, high risk): not in lower half of range.")
                                        continue
                                elif not (in_lower or under_avg or allow_exception):
                                    print(f"   ⏸️ Skipping buy: price at/near average (no price edge).")
                                    continue
                            ai_max_price = decision.get('max_price', 0.0)
                            edge = ai_max_price - current_price
                            # Edge: stricter for price/high-risk, looser for preferred
                            edge_req = self.min_edge_to_enter
                            if is_price_market and e_category in self.high_risk_categories:
                                edge_req = max(edge_req, 0.04)  # min 4 cent edge for price cases
                            elif is_preferred:
                                edge_req = max(edge_req * 0.8, 0.01)  # 20% lower edge for Sports/Elections
                            if edge > edge_req and current_price <= ai_max_price:
                                # Kelly: more for preferred, less for high-risk
                                if e_category in self.high_risk_categories:
                                    kelly_frac = 0.25 if not self.uncertain_market else 0.125
                                elif is_preferred:
                                    kelly_frac = 0.35 if self.uncertain_market else 0.6  # more capital for Sports/Elections
                                else:
                                    kelly_frac = 0.25 if self.uncertain_market else 0.5
                                bet = self.risk.calculate_kelly_bet(ai_max_price, current_price, balance, kelly_fraction=kelly_frac)
                                bet = min(bet, self.max_bet_usdc)
                                if bet >= 2.0:
                                    price_now = self.polymarket.get_buy_price(token_id)
                                    if price_now > ai_max_price:
                                        print(f"   ⚠️ Price moved during analysis: {current_price:.2f} → {price_now:.2f}. Skipping buy.")
                                        continue
                                    if price_now < 0.10 or price_now > self.max_entry_price:
                                        print(f"   ⚠️ Price out of band ({price_now:.2f}). Skipping buy.")
                                        continue
                                    print(f"\n💎 Approved for buy. Price now: {price_now:.2f}. Placing order.")
                                    market_to_buy = SimpleNamespace(
                                        id=m_id, question=full_title, conditionId=market_data.get('conditionId'), active_token_id=token_id
                                    )
                                    order_id = self.polymarket.execute_market_order(market_to_buy, bet)
                                    if order_id:
                                        time.sleep(2)
                                        actual_shares = self.polymarket.get_token_balance(token_id)
                                        for _ in range(3):
                                            if actual_shares and actual_shares >= 5.0:
                                                break
                                            time.sleep(3)
                                            actual_shares = self.polymarket.get_token_balance(token_id)
                                        actual_fill_price = round(bet / actual_shares, 3) if actual_shares and actual_shares > 0 else price_now
                                        print(f"✅ Buy complete! Receipt: #{order_id} | Fill: {actual_fill_price:.3f} (shares: {actual_shares:.2f})")
                                        _d_end = c.get("market_for_ai", {}).get("days_until_end")
                                        _r_pct = price_context.get("range_pct", 0)
                                        _hype = int(decision.get("hype_score") or 0)
                                        target_price = compute_dynamic_target(
                                            fill_price=actual_fill_price,
                                            days_until_end=_d_end,
                                            range_pct=_r_pct,
                                            hype_score=_hype,
                                            spread_pct=spread_pct,
                                            ai_max_price=ai_max_price,
                                            base_target_pct=self.profit_target,
                                            high_target_pct=self.profit_target_high,
                                            price_high_threshold=self.price_high_threshold,
                                        )
                                        self.db.log_new_trade(
                                            token_id=token_id, market_id=m_id, question=full_title, buy_price=actual_fill_price,
                                            amount_usdc=bet, shares_bought=actual_shares, notes=decision.get('reason', 'Buy approved'),
                                            category=e_category, spread_pct=spread_pct, target_price=target_price, end_date_iso=end_date_str,
                                            event_id=event_id or None,
                                        )
                                        print("💾 Order saved to DB.")
                                        if actual_shares >= 5.0:
                                            sell_ok = self.polymarket.execute_sell_order(token_id, actual_shares, target_price)
                                            if not sell_ok:
                                                time.sleep(5)
                                                sell_ok = self.polymarket.execute_sell_order(token_id, self.polymarket.get_token_balance(token_id), target_price)
                                            if sell_ok:
                                                print(f"   📤 GTC sell order active: {target_price:.2f}")
                                            else:
                                                print(f"   ⚠️ GTC sell order failed – run restore_sell_orders.py at {target_price:.2f}")
                                        if self.active_observer:
                                            self.active_observer.add_token(token_id, actual_fill_price, full_title, target_price=target_price)
                                            print(f"📡 WebSocket monitoring active.")
                                        balance -= bet
                                    else:
                                        print("❌ Buy failed on-chain.")
                                else:
                                    print(f"   ⚠️ Bet too small ({bet:.2f} USDC).")
                            else:
                                print(f"   ⚠️ No edge (Price: {current_price} / AI Max: {ai_max_price}).")
                        elif decision.get('action') == "REJECT":
                            reason = decision.get('reason', '')
                            if "Gatekeeper" in reason or "Lawyer" in reason:
                                skip_rest = True
                    return (balance, skip_rest)

                # 3. Consumer: pull from scanner queue, run War Room (Bouncer already in scanner)
                WAR_ROOM_BATCH_SIZE = 2
                batch_list = []
                for _ in range(WAR_ROOM_BATCH_SIZE):
                    try:
                        c = candidate_queue.get(timeout=5)
                        batch_list.append(c)
                    except queue.Empty:
                        break
                if not batch_list:
                    self._consumer_empty_count = getattr(self, "_consumer_empty_count", 0) + 1
                    if self._consumer_empty_count == 1 or self._consumer_empty_count % 6 == 0:
                        print(f"\n⏳ No candidates in queue – waiting for scanner (Bouncer PASS)... ({self._consumer_empty_count}x timeout)")
                    time.sleep(5)
                    continue
                batch_list = [c for c in batch_list if c.get("market_for_ai") and c.get("full_title")]
                if not batch_list:
                    continue
                self._consumer_empty_count = 0
                for c in batch_list:
                    try:
                        import importlib, sys as _sys
                        _bcc = _sys.modules.get("build_trades_chroma")
                        if not _bcc:
                            _bcc_path = str(Path(__file__).resolve().parents[2] / "scripts" / "python")
                            if _bcc_path not in _sys.path:
                                _sys.path.insert(0, _bcc_path)
                            _bcc = importlib.import_module("build_trades_chroma")
                        c["market_for_ai"]["similar_analyses"] = _bcc.get_similar_analyses_context(c["full_title"], k=3)
                    except Exception:
                        pass
                print(f"\n🧠 War Room analyzing {len(batch_list)} candidate(s):")
                for c in batch_list:
                    cat = c.get("e_category") or "?"
                    price = c.get("current_price", 0)
                    print(f"   📊 [{cat}] {c['full_title'][:70]} @ {price:.2f}")
                try:
                    balance, _ = run_batch(batch_list, balance, skip_bouncer=True)
                except Exception as batch_err:
                    print(f"\n⚠️ Consumer run_batch error: {batch_err}")
                    traceback.print_exc()

            except Exception:
                traceback.print_exc()
                time.sleep(30)