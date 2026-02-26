import json
import websocket
import threading
import time
import datetime as dt

class MagnusObserver(threading.Thread):
    def __init__(self, token_ids, trade_manager):
        super().__init__(daemon=True)
        
        self.ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        self._lock = threading.Lock()
        
        self.trade_manager = trade_manager 
        self.trade = trade_manager  # backward compat alias
        
        self.token_ids = [str(tid) for tid in token_ids if len(str(tid)) > 10]
        self.tokens = self.token_ids 
        
        self.ws = None
        self.is_running = False
        
        try:
            self.history_map = self._load_history_from_db()
        except Exception:
            self.history_map = {}
            
        self.last_prices = {}
        self.last_trigger_time = {} 
        self.msg_count = 0

    def _log(self, msg):
        timestamp = dt.datetime.now().strftime('%H:%M:%S')
        full_msg = f"📡 Observer: {msg}"
        
        try:
            if hasattr(self.trade_manager, '_log_to_live'):
                self.trade_manager._log_to_live(full_msg)
        except Exception:
            pass

    def _load_history_from_db(self):
        mapping = {}
        try:
            open_trades = self.trade_manager.db.get_open_positions()
            target_mult = 1 + getattr(self.trade_manager, 'profit_target', 0.05)
            
            for entry in open_trades:
                t_id = str(entry.get("token_id"))
                buy_price = float(entry.get("buy_price", 0))
                stored_target = entry.get("target_price")
                target = float(stored_target) if stored_target is not None and float(stored_target) >= 0.01 else (buy_price * target_mult)
                opened_at = None
                ts_str = (entry.get("timestamp") or "").strip()
                if ts_str:
                    try:
                        opened_dt = dt.datetime.strptime(ts_str.split(".", 1)[0], "%Y-%m-%d %H:%M:%S")
                        opened_at = str(opened_dt.replace(tzinfo=dt.timezone.utc).timestamp())
                    except Exception:
                        pass
                mapping[t_id] = {
                    "buy_price": buy_price,
                    "question": entry.get("question", "Unknown"),
                    "target": target,
                    "last_log_time": 0,
                    "high_water_mark": buy_price,
                    "break_even_triggered": False,
                    "opened_at": opened_at,
                }
        except Exception as e:
            self._log(f"⚠️ Observer DB error: {e}")
        return mapping

    def add_token(self, token_id, buy_price, question, target_price=None):
        t_id = str(token_id)
        if target_price is not None and target_price >= 0.01:
            target = target_price
        else:
            target_mult = 1 + getattr(self.trade_manager, 'profit_target', 0.07)
            target = buy_price * target_mult
        with self._lock:
            if t_id not in self.token_ids:
                self.token_ids.append(t_id)
                self.history_map[t_id] = {
                    "buy_price": buy_price,
                    "question": question,
                    "target": target,
                    "last_log_time": 0,
                    "high_water_mark": buy_price,
                    "break_even_triggered": False,
                    "opened_at": str(time.time()),
                }
                if self.ws and self.is_running:
                    try:
                        self.ws.send(json.dumps({"type": "market", "assets_ids": [t_id], "operation": "subscribe"}))
                    except Exception:
                        pass

    def on_message(self, ws, message):
        self.msg_count += 1
        if not message or not isinstance(message, str) or message.strip() == "":
            return

        try:
            raw_data = json.loads(message)
        except json.JSONDecodeError:
            
            return

        try:
            data_list = raw_data if isinstance(raw_data, list) else [raw_data]

            for data in data_list:
                changes = (data.get("price_changes") or []) + (data.get("changes") or [])
                if not changes and data.get("asset_id"): changes = [data]

                for change in changes:
                    asset_id = str(change.get("asset_id"))
                    
                    if asset_id not in self.token_ids:
                        continue

                    new_price = float(change.get("best_bid") or change.get("price") or 0)
                    if new_price <= 0: continue
                    new_price = round(new_price, 3)

                    info = self.history_map.get(asset_id, {})
                    buy_p = info.get("buy_price", 0)
                    target_p = info.get("target", 0)
                    
                    if self.last_prices.get(asset_id) == new_price:
                        continue
                    
                    self.last_prices[asset_id] = new_price
                    
                    if buy_p > 0:
                        diff_pct = ((new_price - buy_p) / buy_p) * 100
                        is_at_target = new_price >= target_p
                        now = time.time()
                        hwm = info.get("high_water_mark", buy_p)
                        info["high_water_mark"] = max(hwm, new_price)
                        trailing_trigger_pct = 0.08
                        if info["high_water_mark"] >= buy_p * (1 + trailing_trigger_pct):
                            info["break_even_triggered"] = True

                        if is_at_target or (now - info.get("last_log_time", 0) > 30):
                            icon = "🎯" if is_at_target else ("📈" if diff_pct >= 0 else "📉")
                            q_name = info.get('question', 'Unknown')[:20]
                            log_line = f"{icon} ({asset_id[-6:]}) {q_name}... {buy_p:.2f} -> {new_price:.3f} ({diff_pct:+.1f}%)"
                            
                            self._log(log_line)
                            info["last_log_time"] = now

                        # At target: place sell order (GTC may have been cancelled)
                        if is_at_target:
                            last_hit = self.last_trigger_time.get(asset_id, 0)
                            if (now - last_hit) > 45:
                                self.last_trigger_time[asset_id] = now
                                try:
                                    balance = self.trade_manager.polymarket.get_token_balance(asset_id)
                                    if balance >= 5.0 and target_p >= 0.01:
                                        ok = self.trade_manager.polymarket.execute_sell_order(asset_id, balance, target_p)
                                        if ok:
                                            self._log(f"✅ Target reached for {asset_id[-6:]}. Sell order placed @ {target_p:.2f}")
                                        else:
                                            retry_price = round(target_p - 0.01, 3)
                                            if retry_price >= 0.01:
                                                self._log(f"⚠️ Target {asset_id[-6:]}: sell failed. Retrying one tick below @ {retry_price:.2f}")
                                                ok2 = self.trade_manager.polymarket.execute_sell_order(asset_id, balance, retry_price)
                                                if ok2:
                                                    self._log(f"✅ Sell one tick below succeeded for {asset_id[-6:]} @ {retry_price:.2f}")
                                                else:
                                                    self._log(f"⚠️ Target {asset_id[-6:]}: sell failed, try restore_sell_orders.py")
                                            else:
                                                self._log(f"⚠️ Target {asset_id[-6:]}: sell failed, try restore_sell_orders.py")
                                    else:
                                        self._log(f"✅ Target reached for {asset_id[-6:]}. (Balance {balance:.1f} – too low to sell or already sold)")
                                except Exception as ex:
                                    self._log(f"⚠️ Observer sell error {asset_id[-6:]}: {ex}")

                        # Real-time stop-loss: trigger if price drops below threshold
                        sl_pct = getattr(self.trade_manager, 'stop_loss_pct', 0.20)
                        min_hold_h = getattr(self.trade_manager, 'min_hold_hours_before_sl', 2.0)
                        if sl_pct > 0 and not is_at_target and new_price < buy_p * (1 - sl_pct):
                            trade_old_enough = True
                            opened_str = info.get("opened_at")
                            if opened_str:
                                try:
                                    age_h = (time.time() - float(opened_str)) / 3600.0
                                    trade_old_enough = age_h >= min_hold_h
                                except (ValueError, TypeError):
                                    pass
                            if trade_old_enough:
                                last_sl = self.last_trigger_time.get(asset_id + "_sl", 0)
                                if (now - last_sl) > 60:
                                    self.last_trigger_time[asset_id + "_sl"] = now
                                    try:
                                        balance = self.trade_manager.polymarket.get_token_balance(asset_id)
                                        if balance >= 5.0:
                                            sl_price = round(buy_p * (1 - sl_pct), 3)
                                            sl_price = max(sl_price, 0.01)
                                            ok = self.trade_manager.polymarket.execute_sell_order(asset_id, balance, sl_price)
                                            if ok:
                                                self._log(f"🛑 STOP-LOSS triggered for {asset_id[-6:]} @ {sl_price:.2f} (price {new_price:.3f} < threshold {buy_p * (1 - sl_pct):.3f})")
                                            else:
                                                self._log(f"⚠️ Stop-loss sell failed for {asset_id[-6:]}")
                                    except Exception as ex:
                                        self._log(f"⚠️ Observer SL error {asset_id[-6:]}: {ex}")

                        trailing_sell_threshold_pct = 0.03
                        if not is_at_target and info.get("break_even_triggered") and new_price <= buy_p * (1 + trailing_sell_threshold_pct):
                            last_be = self.last_trigger_time.get(asset_id + "_be", 0)
                            if (now - last_be) > 45:
                                self.last_trigger_time[asset_id + "_be"] = now
                                try:
                                    balance = self.trade_manager.polymarket.get_token_balance(asset_id)
                                    if balance >= 5.0:
                                        be_price = round(buy_p * 1.01, 3)
                                        be_price = min(be_price, 0.99)
                                        ok = self.trade_manager.polymarket.execute_sell_order(asset_id, balance, be_price)
                                        if ok:
                                            self._log(f"✅ Break-even sell (trailing) for {asset_id[-6:]} @ {be_price:.2f}")
                                        else:
                                            self._log(f"⚠️ Break-even sell failed {asset_id[-6:]}")
                                except Exception as ex:
                                    self._log(f"⚠️ Observer break-even error {asset_id[-6:]}: {ex}")
                                
        except Exception as e:
            self._log(f"⚠️ Observer message error: {e}")

    def remove_token(self, token_id):
        """Stop watching a token and unsubscribe from WebSocket."""
        tid_str = str(token_id).strip()
        
        with self._lock:
            if tid_str not in self.token_ids:
                return
            try:
                self.token_ids.remove(tid_str)
            except ValueError:
                pass
            self.history_map.pop(tid_str, None)
            self.last_prices.pop(tid_str, None)
            self.last_trigger_time.pop(tid_str, None)

        if self.ws and getattr(self.ws, "sock", None) and getattr(self.ws.sock, "connected", False):
            unsubscribe_msg = {
                "type": "market",
                "assets_ids": [tid_str],
                "operation": "unsubscribe",
            }
            try:
                self.ws.send(json.dumps(unsubscribe_msg))
                self._log(f"🛑 WebSocket: unsubscribed {tid_str[-6:]}")
            except Exception as send_err:
                self._log(f"⚠️ Unsubscribe send failed for {tid_str[-6:]}: {send_err}")
        else:
            self._log(f"🛑 Token removed from watchlist: {tid_str[-6:]} (WS not connected – cleaned on next on_open)")

    def sync_from_db(self):
        """Sync watchlist with DB and chain. Remove closed positions; detect sold tokens by on-chain balance."""
        try:
            open_trades = self.trade_manager.db.get_open_positions()
            open_ids = {str(t.get("token_id")) for t in open_trades if t.get("token_id")}
            with self._lock:
                to_remove = [tid for tid in self.token_ids if tid not in open_ids]
            for tid in to_remove:
                self.remove_token(tid)

            # On-chain check: detect tokens with zero balance (sold via GTC without DB update)
            open_by_tid = {str(t.get("token_id")): t for t in open_trades}
            for tid in list(self.token_ids):
                try:
                    balance = self.trade_manager.polymarket.get_token_balance(tid)
                    if balance < 0.01:
                        trade_info = open_by_tid.get(tid, {})
                        buy_price = float(trade_info.get("buy_price") or 0)
                        target_price = float(trade_info.get("target_price") or 0)
                        status = "CLOSED_PROFIT" if target_price >= buy_price * 1.01 else "CLOSED_LOSS"
                        self.trade_manager.db.update_trade_status(tid, status, "Balance zero (observer sync)")
                        self._log(f"🔄 Sync: {tid[-6:]} has 0 balance on-chain – closing as {status}.")
                        self.remove_token(tid)
                except Exception as e:
                    self._log(f"⚠️ sync_from_db chain check {tid[-6:]}: {e}")
        except Exception as e:
            self._log(f"⚠️ sync_from_db error: {e}")

    def on_open(self, ws):
        self._log(f"📡 WebSocket open. Watching {len(self.token_ids)} tokens.")
        with self._lock:
            ids_to_sub = list(self.token_ids)
        if ids_to_sub:
            ws.send(json.dumps({"type": "market", "assets_ids": ids_to_sub, "operation": "subscribe"}))
        
        def run_ping():
            while self.is_running:
                time.sleep(20)
                try: 
                    if self.ws: self.ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    break
        threading.Thread(target=run_ping, daemon=True).start()

    def on_error(self, ws, error):
        self._log(f"❌ WebSocket-error: {error}")

    def on_close(self, ws, code, msg):
        self._log("📡 WebSocket closed. Reconnecting...")
        if self.is_running:
            time.sleep(5)
            self.start()

    def start(self):
        self.is_running = True
        self.ws = websocket.WebSocketApp(
            self.ws_url, on_open=self.on_open, on_message=self.on_message, 
            on_error=self.on_error, on_close=self.on_close
        )
        threading.Thread(target=self.ws.run_forever, kwargs={"ping_interval": 20, "ping_timeout": 10}, daemon=True).start()