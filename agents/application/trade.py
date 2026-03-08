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
        try:
            self.min_edge_to_enter = float(os.getenv("MAGNUS_MIN_EDGE", "0.018"))
        except ValueError:
            self.min_edge_to_enter = 0.018  # ~1.8 cent – fler köp; sätt 0.025 för strängare
        self.max_open_positions = 15
        self.max_bet_usdc = 100.0       # Smaller bets for diversification
        # Polymarket krav: minst $1 per köp, och minst 5 andelar för att kunna sälja (sälj-minimum 5)
        try:
            self.min_bet_usdc = float(os.getenv("MAGNUS_MIN_BET_USDC", "1.0"))
        except ValueError:
            self.min_bet_usdc = 1.0
        self.min_shares_to_buy = 5.0   # Polymarket: minst 5 andelar vid sälj – vi måste köpa minst så många
        try:
            self.price_move_tolerance = float(os.getenv("MAGNUS_PRICE_MOVE_TOLERANCE", "0.02"))
        except ValueError:
            self.price_move_tolerance = 0.02  # Max 2¢ rörelse under analys – snabbare skippa vid prisstigning
        # Min andel av saldo per godkänt köp – Kelly kan bli väldigt liten vid låg fair value; då använd minst denna andel
        try:
            self.min_bet_pct_balance = float(os.getenv("MAGNUS_MIN_BET_PCT_BALANCE", "0.08"))
        except ValueError:
            self.min_bet_pct_balance = 0.08   # 8% av saldo som minst vid godkänt köp (36 USDC → ~2.9 USDC)
        # Minsta brutto‑profit per trade (USDC). Sänk (t.ex. 0.05) så fler godkända BUY blir faktiska ordrar.
        try:
            self.min_gross_profit_usdc = float(os.getenv("MAGNUS_MIN_GROSS_PROFIT_USDC", "0.05"))
        except ValueError:
            self.min_gross_profit_usdc = 0.05
        # Tighter stop-loss to improve risk/reward
        self.stop_loss_pct = 0.15
        # Bred band så nästan-avgjorda (0.1¢–99.9¢) når analys; Quant avvisar om ingen edge
        try:
            self.min_entry_price = float(os.getenv("MAGNUS_MIN_ENTRY_PRICE", "0.001"))
        except ValueError:
            self.min_entry_price = 0.001
        try:
            self.max_entry_price = float(os.getenv("MAGNUS_MAX_ENTRY_PRICE", "0.999"))
        except ValueError:
            self.max_entry_price = 0.999
        # Markets we historically lose on – skip (title match).
        # Elon Musk tweet-räknare = ren brus; de ger edge dåligt och skippas alltid.
        # Bitcoin-marknader: default = TILLÅT (MAGNUS_SKIP_BITCOIN=0). Sätt MAGNUS_SKIP_BITCOIN=1 i .env om du vill blocka dem.
        self.skip_title_patterns = [("elon musk", "tweet")]
        if os.getenv("MAGNUS_SKIP_BITCOIN", "0").strip().lower() in ("1", "true", "yes"):
            self.skip_title_patterns.append(("bitcoin", None))
        # 0 = köp även om pris inte är under snitt (rekommenderat så Quant-BUY faktiskt blir köp)
        self.require_below_avg = os.getenv("MAGNUS_REQUIRE_BELOW_AVG", "0").strip().lower() not in ("0", "false", "no")
        self.allow_at_avg_if_hype_min = 6   # If hype_score >= this, allow buy at "near avg" too (0 = off)
        try:
            self.min_range_pct = float(os.getenv("MAGNUS_MIN_RANGE_PCT", "0"))
        except ValueError:
            self.min_range_pct = 0  # 0 = kräv inte volatilitet i scannern (fler kandidater)
        self.min_change_1h_pct = 0  # Skip if |1h change| < this % (0 = off)
        self.active_observer = None
        # Balanced events (sport): max 1 buy per event; others (ETH levels): multiple allowed
        self.balanced_event_categories = ("Sports", "Crypto", "Earnings")
        self.max_positions_per_event = 2
        self.high_risk_categories = ("Crypto", "Business", "Tech", "Economics", "Geopolitics")
        # Alla kategorier i scope – ingen kategoriprioritering; edge avgör.
        self.preferred_categories = ()
        # Min bid liquidity for buy allowed (lågt = fler in i analys; Quant kan avvisa illikvida)
        try:
            self.min_bid_liquidity_usdc = float(os.getenv("MAGNUS_MIN_BID_LIQUIDITY", "3.0"))
        except ValueError:
            self.min_bid_liquidity_usdc = 3.0
        # Max spread % (bid-ask). 95 = ta in nästan allt; Quant avvisar illikvida. Sänk till 50–70 för tightare.
        try:
            self.max_spread_pct = float(os.getenv("MAGNUS_MAX_SPREAD_PCT", "95.0"))
        except ValueError:
            self.max_spread_pct = 95.0
        # Min dagar kvar innan vi tillåter köp (default 0.2 ≈ 5 h). Sätt 0 för att inte blockera på tid när Quant säger BUY.
        try:
            self.min_days_to_buy = float(os.getenv("MAGNUS_MIN_DAYS_TO_BUY", "0.2"))
        except ValueError:
            self.min_days_to_buy = 0.2
        # Återhandla marknad efter N dagar (0 = aldrig). Fler kandidater om poolen "torkat".
        try:
            self.allow_retrade_after_days = int(os.getenv("MAGNUS_ALLOW_RETRADE_AFTER_DAYS", "0"))
        except ValueError:
            self.allow_retrade_after_days = 0
        # Default 1: skippa Bouncer i scannern – alla som passerar pre-filters går till kön (Lawyer+Quant filtrerar). Sätt 0 för att köra Bouncer.
        self.skip_bouncer_in_scanner = os.getenv("MAGNUS_SKIP_BOUNCER_IN_SCANNER", "1").strip().lower() in ("1", "true", "yes")
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
            self.stop_loss_pct = 0.15

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
            # En anrop för alla positioner istället för N anrop (get_positions per token).
            positions_map = self.polymarket.get_all_token_balances()
            for i, t in enumerate(trades):
                t_id = t['token_id']
                sys.stdout.write(f"\r   [{i+1}/{len(trades)}] Checking: {t['question'][:30]}...")
                sys.stdout.flush()

                actual_balance = positions_map.get(str(t_id), 0.0)
                target_price = float(t.get('target_price') or 0)
                # Saknad GTC-sälj: om vi har andelar men ingen säljorder, lägg en
                if actual_balance >= 5.0 and target_price >= 0.01:
                    try:
                        open_orders = self.polymarket.get_open_orders(asset_id=str(t_id))
                        orders_list = open_orders.get("data", open_orders) if isinstance(open_orders, dict) else (open_orders or [])
                        has_sell = any(
                            str(getattr(o, "side", o.get("side", "") if isinstance(o, dict) else "")).upper() == "SELL"
                            for o in (orders_list if isinstance(orders_list, list) else [])
                        )
                        if not has_sell:
                            print(f"\n📤 Saknad GTC-sälj – lägger target {target_price:.2f} för {t['question'][:35]}…")
                            ok = self.polymarket.execute_sell_order(t_id, actual_balance, target_price)
                            if ok:
                                print(f"   ✓ GTC sell order placerad.")
                            else:
                                print(f"   ⚠️ GTC sell misslyckades – se logg.")
                    except Exception as e:
                        logger.warning("Saknad GTC-sälj check failed for %s: %s", t_id, str(e)[:80])
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
            print(f"\n⚠️ Trade management error: {(str(e)[:100])}")

    def already_owns(self, market_id: str) -> bool:
        trades = self.db.get_open_positions()
        return any(str(t['market_id']) == str(market_id) for t in trades)

    def _allow_market_scan(self, market_id: str) -> bool:
        """True om marknaden ska få komma in i scannern. Blockerar bara om vi har öppen position (already_owns).
        Tidigare handlade marknader får komma in igen så att analyspoolen inte krymper; allow_retrade_after_days
        används vid köpbeslut, inte här."""
        return not self.already_owns(market_id)

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

        # 1b. CLOB heartbeat – utan detta avbryts GTC-ordrar inom ~10s (Polymarket krav)
        self.polymarket.start_heartbeat()

        # 2. Scanner queue (Bouncer in scanner – only PASS in queue)
        candidate_queue = queue.Queue(maxsize=500)
        # Scan cadence and dedup TTL can be tuned via env
        try:
            scan_interval = int(os.getenv("MAGNUS_SCAN_INTERVAL_SECONDS", "900"))  # default: 15 min – fler scannerrundor, fler kandidater
        except ValueError:
            scan_interval = 900
        try:
            dedup_ttl = int(os.getenv("MAGNUS_DEDUP_TTL_SECONDS", "3600"))  # default: 1 hour – fler når Quant
        except ValueError:
            dedup_ttl = 3600
        try:
            event_limit = int(os.getenv("MAGNUS_SCANNER_EVENT_LIMIT", "2500"))  # events per strategy; höj för fler kandidater
        except ValueError:
            event_limit = 2500
        _strat_env = os.getenv("MAGNUS_SCANNER_STRATEGIES", "").strip()
        strategies = [s.strip() for s in _strat_env.split(",") if s.strip()] if _strat_env else ["liquid", "undiscovered", "new"]
        scanner = MarketScanner(
            candidate_queue,
            self,
            strategies=strategies,
            event_limit=event_limit,
            dedup_ttl_seconds=dedup_ttl,
            sleep_between_rounds_seconds=scan_interval,
        )
        scanner.start()
        print("📡 Scanner thread started (Bouncer in scanner).")

        while True:
            try:
                balance = self.polymarket.get_usdc_balance()
                self.portfolio_risk.log_balance(balance)

                should_pause, drawdown = self.portfolio_risk.check_drawdown(balance)
                if should_pause:
                    print(f"\n💵 Balance: {balance:.2f} USDC")
                    print(f"🛑 Portfolio drawdown {drawdown}% exceeds limit. Pausing new trades for 5 min...")
                    self._log_to_live(f"🛑 Drawdown {drawdown}% — pausing new trades")
                    self.manage_active_trades()
                    time.sleep(300)
                    continue
                
                self.manage_active_trades()
                
                if balance < 2.0:
                    print(f"\n💵 Balance: {balance:.2f} USDC")
                    print(f"💤 Balance < 2.0 USDC. Waiting 2 min...")
                    time.sleep(120); continue

                def run_batch(batch_list, balance, skip_bouncer=False):
                    """Run War Room for batch; returns (new_balance, skip_rest). skip_bouncer=True for scanner candidates."""
                    skip_rest = False
                    payloads = [c["market_for_ai"] for c in batch_list]
                    def _process_one(c, raw, bal, skip):
                        """Bearbeta ett War Room-resultat; returnerar (ny_balance, ny_skip_rest)."""
                        # Normalisera beslut så vi alltid har konsekvent struktur (ingen tyst default-risk).
                        decision = raw if isinstance(raw, dict) else {"action": "REJECT", "reason": f"Error: {raw}", "max_price": 0.0, "hype_score": 0}
                        decision["action"] = (decision.get("action") or "REJECT").strip().upper()
                        if decision["action"] != "BUY":
                            decision["action"] = "REJECT"
                        decision["max_price"] = float(decision.get("max_price") or 0)
                        decision["reason"] = (str(decision.get("reason") or ""))[:500]
                        decision["hype_score"] = int(decision.get("hype_score") or 0)

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

                        # Fallback‑heuristik: om Quant är överdrivet försiktig men vi ser tydlig edge
                        # (hög hype, pris i nedre delen av range, tillräcklig volatilitet och tid kvar),
                        # tvinga fram ett BUY med konservativ MAX_PRICE. Detta är en "second opinion"
                        # ovanpå DeepSeek, inte ett extra filter.
                        if decision["action"] == "REJECT":
                            hype = decision["hype_score"]
                            pc = price_context or {}
                            days_until_end = c.get("market_for_ai", {}).get("days_until_end")
                            spread_here = spread_pct
                            range_pct = float(pc.get("range_pct") or 0.0)
                            in_lower_half = bool(pc.get("in_lower_half"))
                            near_low = bool(pc.get("near_historical_low"))
                            # Lätta: hype 6+, range 3%+, 0.5+ dagar kvar – fler REJECT blir BUY när edge finns.
                            if (
                                isinstance(hype, int) and hype >= 6
                                and (in_lower_half or near_low)
                                and range_pct >= 3.0
                                and (days_until_end is None or days_until_end >= 0.5)
                                and (spread_here is None or spread_here <= self.max_spread_pct)
                            ):
                                edge_floor = getattr(self, "min_edge_to_enter", 0.015)
                                # Konservativt maxpris: lite över nuvarande, med tak per riskkategori.
                                safe_cap = 0.85 if e_category in self.high_risk_categories else 0.92
                                max_price_auto = min(current_price + edge_floor, safe_cap)
                                decision["action"] = "BUY"
                                decision["max_price"] = max_price_auto
                                auto_reason = (
                                    f"Auto-BUY by fallback heuristic (hype={hype}, "
                                    f"range={range_pct}%, lower_range={in_lower_half or near_low}). "
                                    f"MAX_PRICE set to {max_price_auto:.3f}."
                                )
                                # Behåll ursprunglig Quant‑reason för transparens i DB, men logga auto‑heuristik separat.
                                self._log_to_live(f"🤖 Fallback BUY override: {auto_reason[:180]}")
                        # region agent log
                        try:
                            import json as _json  # lokal alias för debug-loggning
                            with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                                _fdbg.write(
                                    _json.dumps(
                                        {
                                            "sessionId": "ed1d60",
                                            "runId": "pre-fix",
                                            "hypothesisId": "H2",
                                            "location": "trade.py:run_batch",
                                            "message": "war_room_decision",
                                            "data": {
                                                "question": (full_title or "")[:120],
                                                "category": e_category,
                                                "action": decision.get("action", "REJECT"),
                                                "max_price": decision.get("max_price", 0),
                                                "current_price": current_price,
                                                "hype": int(decision.get("hype_score", 0)),
                                            },
                                            "timestamp": int(time.time() * 1000),
                                        }
                                    )
                                    + "\n"
                                )
                        except Exception:
                            pass
                        # endregion
                        self.db.log_analysis(
                            question=full_title, category=e_category,
                            action=decision.get("action", "REJECT"), reason=decision.get("reason", ""),
                            max_price=decision.get("max_price", 0), current_price=current_price,
                            hype_score=int(decision.get("hype_score", 0)),
                        )
                        title_short = (full_title or "")[:48]
                        if decision.get("action") == "REJECT":
                            reason = (decision.get("reason") or "").strip()[:70]
                            print(f"   → REJECT: {title_short} | {reason}", flush=True)
                            self._log_to_live(f"❌ REJECT: {decision.get('reason', '')[:80]}")
                        else:
                            print(f"   → APPROVED: {title_short} | max {decision.get('max_price', 0):.2f}", flush=True)
                        if decision.get('action') == "BUY":
                            print(f"   🔍 Processing BUY: {title_short[:40]}... (max {decision.get('max_price', 0):.2f})", flush=True)
                            # region agent log
                            try:
                                import json as _json  # lokal alias för debug-loggning
                                with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                                    _fdbg.write(
                                        _json.dumps(
                                            {
                                                "sessionId": "ed1d60",
                                                "runId": "pre-fix",
                                                "hypothesisId": "H3",
                                                "location": "trade.py:BUY_block",
                                                "message": "buy_block_enter",
                                                "data": {
                                                    "question": (full_title or "")[:120],
                                                    "category": e_category,
                                                    "ai_max_price": decision.get("max_price", 0),
                                                    "current_price": current_price,
                                                },
                                                "timestamp": int(time.time() * 1000),
                                            }
                                        )
                                        + "\n"
                                    )
                            except Exception:
                                pass
                            # endregion
                            # Balanced events (sport): max 1 buy; others (ETH levels): max N
                            event_id = (c.get("event_id") or "").strip()
                            if event_id and not self._allow_more_positions_in_event(event_id, e_category):
                                n = self.count_open_positions_in_event(event_id)
                                if (e_category or "").strip() in self.balanced_event_categories:
                                    msg = "Already have position in this event (balanced, max 1). Skipping buy."
                                    print(f"   ⏸️ {msg}")
                                    self._log_to_live(f"⏸️ {msg}")
                                else:
                                    msg = f"Already {n} position(s) in this event (max {self.max_positions_per_event}). Skipping buy."
                                    print(f"   ⏸️ {msg}")
                                    self._log_to_live(f"⏸️ {msg}")
                                return (bal, skip)
                            if self.portfolio_risk.check_correlation(full_title, e_category):
                                msg = f"Too many correlated positions in {e_category}. Skipping buy."
                                print(f"   ⏸️ {msg}")
                                self._log_to_live(f"⏸️ {msg}")
                                return (bal, skip)
                            days_until_end = c.get("market_for_ai", {}).get("days_until_end")
                            change_1h = price_context.get("change_1h")
                            # Avoid catching falling knives in high-risk categories
                            if (
                                change_1h is not None
                                and e_category in self.high_risk_categories
                                and isinstance(change_1h, (int, float))
                                and change_1h < -5.0
                            ):
                                print(f"   ⏸️ Short-term momentum too negative ({change_1h}%). Skipping buy.")
                                return (bal, skip)
                            min_days = getattr(self, "min_days_to_buy", 0.2)
                            if days_until_end is not None and isinstance(days_until_end, (int, float)) and min_days > 0 and days_until_end < min_days:
                                self._log_to_live(f"⏸️ Too little time left (< {min_days} day). Skipping buy.")
                                print(f"   ⏸️ Too little time left (< {min_days} day). Skipping buy.")
                                return (bal, skip)
                            open_count = len(self.db.get_open_positions())
                            if open_count >= self.max_open_positions:
                                self._log_to_live(f"⏸️ Max open positions ({self.max_open_positions}). Skipping new buy.")
                                print(f"   ⏸️ Max open positions ({self.max_open_positions}). Skipping new buy.")
                                return (bal, skip)
                            is_price_market = False
                            if self.require_below_avg:
                                in_lower = price_context.get("in_lower_half")
                                under_avg = price_context.get("price_vs_avg") == "below average"
                                hype = int(decision.get("hype_score") or 0)
                                # Alla kategorier: samma köpvillighet när det finns edge (tidigare "preferred"-logik för alla).
                                hype_threshold = max(self.allow_at_avg_if_hype_min - 3, 1)
                                allow_exception = hype_threshold and hype >= hype_threshold
                                full_title_lower = (full_title or "").lower()
                                is_price_market = any(
                                    kw in full_title_lower
                                    for kw in ["price of", "above $", "below $", "finish week", "finish the week"]
                                ) or "$" in (full_title or "")
                                if is_price_market and e_category in self.high_risk_categories:
                                    if not in_lower:
                                        print(f"   ⏸️ Skipping buy (price-market, high risk): not in lower half of range.")
                                        return (bal, skip)
                                elif not (in_lower or under_avg or allow_exception):
                                    msg = "Skipping buy: price at/near average (no price edge)."
                                    print(f"   ⏸️ {msg}")
                                    self._log_to_live(f"⏸️ {msg}")
                                    return (bal, skip)
                            ai_max_price = float(decision.get('max_price', 0.0) or 0.0)
                            # Måste finnas utrymme för vinst inkl. avgifter. MAGNUS_MAX_BUY_PRICE (default 0.6) = max 40¢/andel.
                            try:
                                max_buy = float(os.getenv("MAGNUS_MAX_BUY_PRICE", "0.6"))
                            except ValueError:
                                max_buy = 0.6
                            max_buy = max(0.01, min(0.99, max_buy))
                            ai_cap = min(max_buy, 0.75) if e_category in self.high_risk_categories else max_buy
                            ai_max_price = min(ai_max_price, ai_cap)
                            # Quant sa BUY – kräv bara att vi inte betalar över deras max (ingen extra edge-spärr)
                            if current_price <= ai_max_price:
                                # Kelly: samma för alla icke high-risk (alla kategorier med edge); mindre för high-risk
                                if e_category in self.high_risk_categories:
                                    kelly_frac = 0.20 if not self.uncertain_market else 0.10
                                else:
                                    kelly_frac = 0.30 if self.uncertain_market else 0.45
                                bet = self.risk.calculate_kelly_bet(ai_max_price, current_price, bal, kelly_fraction=kelly_frac)
                                # Kelly kan bli väldigt liten – använd minst X% av saldo
                                bet_floor = bal * getattr(self, "min_bet_pct_balance", 0.05)
                                bet = max(bet, bet_floor)
                                bet = min(bet, self.max_bet_usdc)
                                # Färskt pris (ingen cache) så vi inte skippar pga gammal data eller cache/CLOB-skillnad
                                price_now = self.polymarket.get_buy_price(token_id, use_cache=False)
                                if price_now is None or price_now <= 0:
                                    print(f"   ⚠️ Price now {price_now!r} – no valid bid/ask; skipping buy.")
                                    return (bal, skip)
                                # Ingen tolerance uppåt – om priset stigit under analys, skippa. (MAGNUS_PRICE_MOVE_TOLERANCE=0.02 ger 2¢ buffer.)
                                price_tolerance = getattr(self, "price_move_tolerance", 0.02)
                                if price_now > ai_max_price + price_tolerance:
                                    print(f"   ⚠️ Price moved during analysis: {current_price:.2f} → {price_now:.2f} (max {ai_max_price}). Skipping buy.")
                                    return (bal, skip)
                                # Säkerhetsband: inte över vårt globala max; high-risk lite lägre
                                price_cap = self.max_entry_price
                                if e_category in self.high_risk_categories:
                                    price_cap = min(price_cap, 0.85)
                                min_p = getattr(self, "min_entry_price", 0.10)
                                if price_now < min_p or price_now > price_cap:
                                    print(f"   ⚠️ Price out of band ({price_now:.2f} > {price_cap:.2f}). Skipping buy.")
                                    return (bal, skip)
                                # Om vi ska köpa så köper vi alltid – tryck upp till minst $1 och 5 andelar (Polymarket-krav)
                                min_bet_polymarket = max(self.min_bet_usdc, self.min_shares_to_buy * price_now)
                                bet = max(bet, min_bet_polymarket)
                                bet = min(bet, self.max_bet_usdc)
                                if bet > bal:
                                    print(f"   ⚠️ Saldo {bal:.2f} USDC räcker inte för {bet:.2f} USDC. Skipping.")
                                    return (bal, skip)
                                # Säkerställ att förväntad brutto‑profit i USDC är värd avgifter och risk.
                                edge_per_share = max(0.0, ai_max_price - price_now)
                                if edge_per_share <= 0:
                                    msg = f"Expected edge per share ≤ 0 (max {ai_max_price:.3f} vs price {price_now:.3f}). Skipping."
                                    print(f"   ⚠️ {msg}")
                                    self._log_to_live(f"⚠️ {msg}")
                                    return (bal, skip)
                                est_shares = bet / price_now if price_now > 0 else 0.0
                                est_gross_profit = edge_per_share * est_shares
                                if est_gross_profit < getattr(self, "min_gross_profit_usdc", 0.0):
                                    msg = (
                                        f"Estimated gross profit {est_gross_profit:.3f} USDC below "
                                        f"threshold {self.min_gross_profit_usdc:.3f}. Skipping buy."
                                    )
                                    print(f"   ⚠️ {msg}")
                                    self._log_to_live(f"⚠️ {msg}")
                                    return (bal, skip)
                                print(f"\n💎 Approved for buy. Price now: {price_now:.2f}. Placing order ({bet:.2f} USDC, ≥5 shares).", flush=True)
                                # region agent log
                                try:
                                    import json as _json  # lokal alias för debug-loggning
                                    with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                                        _fdbg.write(
                                            _json.dumps(
                                                {
                                                    "sessionId": "ed1d60",
                                                    "runId": "pre-fix",
                                                    "hypothesisId": "H4",
                                                    "location": "trade.py:execute_market_order",
                                                    "message": "place_order",
                                                    "data": {
                                                        "question": (full_title or "")[:120],
                                                        "category": e_category,
                                                        "price_now": price_now,
                                                        "ai_max_price": ai_max_price,
                                                        "bet": bet,
                                                        "balance": bal,
                                                    },
                                                    "timestamp": int(time.time() * 1000),
                                                }
                                            )
                                            + "\n"
                                        )
                                except Exception:
                                    pass
                                # endregion
                                market_to_buy = SimpleNamespace(
                                    id=m_id, question=full_title, conditionId=market_data.get('conditionId'), active_token_id=token_id
                                )
                                order_id = self.polymarket.execute_market_order(market_to_buy, bet, max_price=ai_max_price)
                                # region agent log
                                try:
                                    import json as _json  # lokal alias för debug-loggning
                                    with open("/home/kim/agents/.cursor/debug-ed1d60.log", "a", encoding="utf-8") as _fdbg:
                                        _fdbg.write(
                                            _json.dumps(
                                                {
                                                    "sessionId": "ed1d60",
                                                    "runId": "pre-fix",
                                                    "hypothesisId": "H4",
                                                    "location": "trade.py:execute_market_order",
                                                    "message": "execute_market_order_return",
                                                    "data": {
                                                        "question": (full_title or "")[:120],
                                                        "token_id": str(token_id),
                                                        "order_id": str(order_id) if order_id else None,
                                                    },
                                                    "timestamp": int(time.time() * 1000),
                                                }
                                            )
                                            + "\n"
                                        )
                                except Exception:
                                    pass
                                # endregion
                                if order_id:
                                    # FOK fylls direkt; maker BUY (GTC) vilar på boken – polla för fill
                                    time.sleep(2)
                                    actual_shares = self.polymarket.get_token_balance(token_id)
                                    for _ in range(12):  # ~36s total för GTC
                                        if actual_shares and actual_shares >= 5.0:
                                            break
                                        time.sleep(3)
                                        actual_shares = self.polymarket.get_token_balance(token_id)
                                    actual_fill_price = round(bet / actual_shares, 3) if actual_shares and actual_shares > 0 else price_now
                                    if actual_shares and actual_shares >= 5.0:
                                        print(f"✅ Buy complete! Receipt: #{order_id} | Fill: {actual_fill_price:.3f} (shares: {actual_shares:.2f})")
                                    else:
                                        print(f"📋 Order placed (GTC). Receipt: #{order_id} | Shares: {(actual_shares or 0):.2f} – order på boken, väntar på fill. Inte loggat till DB.")
                                    # Logga endast till DB när vi faktiskt har andelar (undvik phantom)
                                    if actual_shares and actual_shares >= 5.0:
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
                                        bal -= bet
                                else:
                                    print("❌ Buy failed – ingen order placerad. (FOK: ingen match? MAGNUS_BUY_FOK_ONLY=1 och ingen ask?)", flush=True)
                            else:
                                msg = f"No edge (Price: {current_price} / AI Max: {ai_max_price})."
                                print(f"   ⚠️ {msg}")
                                self._log_to_live(f"⚠️ {msg}")
                        elif decision.get('action') == "REJECT":
                            reason = decision.get('reason', '')
                            if "Gatekeeper" in reason or "Lawyer" in reason:
                                skip = True
                        return (bal, skip)

                    async def _run_async():
                        bal, skip = balance, skip_rest
                        if len(payloads) == 1:
                            raw = await self.war_room.evaluate_market(payloads[0], skip_bouncer=skip_bouncer)
                            bal, skip = _process_one(batch_list[0], raw, bal, skip)
                        else:
                            async def _eval_with_idx(idx, m):
                                raw = await self.war_room.evaluate_market(m, skip_bouncer=skip_bouncer)
                                return (idx, raw)
                            tasks = [_loop.create_task(_eval_with_idx(i, m)) for i, m in enumerate(payloads)]
                            task_to_idx = {t: i for i, t in enumerate(tasks)}
                            pending = set(tasks)
                            while pending and not skip:
                                done_set, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                                if skip:
                                    for t in pending:
                                        t.cancel()
                                    break
                                for t in done_set:
                                    try:
                                        idx, raw = t.result()
                                    except asyncio.CancelledError:
                                        continue
                                    except Exception as e:
                                        idx = task_to_idx.get(t, 0)
                                        raw = {"action": "REJECT", "reason": str(e)[:200], "max_price": 0.0, "hype_score": 0}
                                    c = batch_list[idx]
                                    bal, skip = _process_one(c, raw, bal, skip)
                        return (bal, skip)

                    try:
                        balance, skip_rest = _loop.run_until_complete(_run_async())
                    except Exception as war_err:
                        err_short = str(war_err)[:120]
                        print(f"\n⚠️ War Room error (batch): {err_short}")
                        logger.exception("War Room batch error: %s", err_short)
                        self._log_to_live(f"⚠️ War Room batch error: {err_short}")
                        for c in batch_list:
                            balance, skip_rest = _process_one(c, {"action": "REJECT", "reason": f"Error: {war_err}", "max_price": 0.0, "hype_score": 0}, balance, skip_rest)
                    print("─" * 60, flush=True)
                    return (balance, skip_rest)

                # 3. Consumer: pull from scanner queue, run War Room (Bouncer already in scanner)
                try:
                    WAR_ROOM_BATCH_SIZE = int(os.getenv("MAGNUS_WAR_ROOM_BATCH_SIZE", "4"))
                except ValueError:
                    WAR_ROOM_BATCH_SIZE = 4
                WAR_ROOM_BATCH_SIZE = max(1, min(WAR_ROOM_BATCH_SIZE, 8))
                batch_list = []
                for _ in range(WAR_ROOM_BATCH_SIZE):
                    try:
                        c = candidate_queue.get(timeout=5)
                        batch_list.append(c)
                    except queue.Empty:
                        break
                if not batch_list:
                    self._consumer_empty_count = getattr(self, "_consumer_empty_count", 0) + 1
                    qsize = candidate_queue.qsize()
                    if self._consumer_empty_count == 1 or self._consumer_empty_count % 6 == 0:
                        print(f"\n💵 Balance: {balance:.2f} USDC", flush=True)
                        self._log_to_live(f"💓 Heartbeat | Balance: {balance:.2f} USDC")
                        print(f"⏳ Kön tom (qsize={qsize}) – väntar på scanner… ({self._consumer_empty_count}x)", flush=True)
                    time.sleep(5)
                    continue
                qsize_after = candidate_queue.qsize()
                print(f"\n💵 Balance: {balance:.2f} USDC")
                self._log_to_live(f"💓 Heartbeat | Balance: {balance:.2f} USDC")
                batch_list = [c for c in batch_list if c.get("market_for_ai") and c.get("full_title")]
                if not batch_list:
                    continue
                # Billigast först – vi vill köpa innan priset hinner röra sig
                batch_list.sort(key=lambda c: float(c.get("current_price") or 0.99))
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
                print("\n" + "═" * 60, flush=True)
                print("🧠 WAR ROOM – plockade " + str(len(batch_list)) + " från kön (återstår " + str(qsize_after) + ") – analyserar:", flush=True)
                print("═" * 60, flush=True)
                for c in batch_list:
                    title = str(c.get("full_title") or "")[:56]
                    price = float(c.get("current_price") or 0)
                    print("   • " + title + " @ " + f"{price:.2f}", flush=True)
                try:
                    balance, _ = run_batch(batch_list, balance, skip_bouncer=True)
                except Exception as batch_err:
                    print(f"\n⚠️ Consumer run_batch error: {batch_err}")
                    logger.exception("Consumer run_batch error")
                    traceback.print_exc()

            except Exception:
                logger.exception("Sniper loop exception")
                traceback.print_exc()
                time.sleep(30)