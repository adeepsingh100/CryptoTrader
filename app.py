import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import numpy as np
import json
import time
import hmac
import hashlib
import requests
import math
import threading
from groq import Groq
from datetime import datetime, timezone, timedelta

# Google Sheets Libraries
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ==========================================
# 1. SECURE API KEYS & AUTHENTICATION
# ==========================================
try:
    API_KEY = st.secrets["COINDCX_API_KEY"]
    API_SECRET = st.secrets["COINDCX_API_SECRET"]
    GROQ_API_KEY = st.secrets["GROQ_API_KEY"]
except Exception:
    API_KEY = "YOUR_COINDCX_API_KEY_HERE"
    API_SECRET = "YOUR_COINDCX_API_SECRET_HERE"
    GROQ_API_KEY = "YOUR_GROQ_API_KEY_HERE"

# Anti-Spam Log Throttler
_last_error_log_times = {}

def log_api_failure(endpoint, error_msg):
    global _last_error_log_times
    current_time = time.time()
    
    if endpoint in _last_error_log_times and (current_time - _last_error_log_times[endpoint]) < 300:
        return

    _last_error_log_times[endpoint] = current_time
    
    ist_offset = timedelta(hours=5, minutes=30)
    ist_time = (datetime.fromtimestamp(current_time, tz=timezone.utc) + ist_offset).strftime("%Y-%m-%d %I:%M:%S %p")
    try:
        with open("api-failures.log", "a") as f:
            f.write(f"[{ist_time}] {endpoint} - {error_msg}\n")
    except Exception:
        pass
        
    if 'gs_manager' in globals():
        gs_manager.append_api_error(current_time, endpoint, error_msg)

def coindcx_auth_post(endpoint, body, max_retries=3, timeout=20):
    url = f"https://api.coindcx.com{endpoint}"
    secret_bytes = bytes(API_SECRET, 'utf-8')
    json_body = json.dumps(body, separators=(',', ':'))
    signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()
    
    headers = {
        'Content-Type': 'application/json',
        'X-AUTH-APIKEY': API_KEY,
        'X-AUTH-SIGNATURE': signature
    }
    
    for attempt in range(max_retries):
        try:
            res = requests.post(url, data=json_body, headers=headers, timeout=timeout)
            try:
                data = res.json()
            except Exception:
                res.raise_for_status()
                return {"error": "Failed to parse CoinDCX API response"}
                
            if res.status_code != 200:
                err_msg = data.get("message", data.get("error", str(data)))
                log_api_failure(endpoint, f"HTTP {res.status_code}: {err_msg}")
                return {"error": err_msg}
            return data
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2) 
                continue
            log_api_failure(endpoint, str(e))
            return {"error": str(e)}

def coindcx_get_with_retry(url, max_retries=3, timeout=20):
    for attempt in range(max_retries):
        try:
            res = requests.get(url, timeout=timeout)
            res.raise_for_status()
            return res.json()
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            log_api_failure(url, str(e))
            return None

# ==========================================
# 2. CACHED & RELIABLE GOOGLE SHEETS MANAGER
# ==========================================
SHEET_NAME = "CryptoBotHistory"
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

class GoogleSheetsManager:
    def __init__(self):
        self.client = None
        self.spreadsheet = None
        self.trades_sheet = None
        self.logs_sheet = None
        self.errors_sheet = None
        self.last_error = None

    def connect(self):
        try:
            if not st.secrets.get("gcp_service_account"):
                self.last_error = "Missing [gcp_service_account] in Streamlit Secrets."
                return False

            creds_dict = dict(st.secrets["gcp_service_account"])
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
            self.client = gspread.authorize(creds)
            self.spreadsheet = self.client.open(SHEET_NAME)

            self.trades_sheet = self.spreadsheet.sheet1
            if not self.trades_sheet.get_all_values():
                headers = ["Date & Time (IST)", "coin", "entry_price", "exit_price", "invested", "net_pnl", "net_pnl_pct", "type"]
                self.trades_sheet.append_row(headers)

            try:
                self.logs_sheet = self.spreadsheet.worksheet("ExecutionLogs")
            except Exception:
                self.logs_sheet = self.spreadsheet.add_worksheet("ExecutionLogs", rows=2000, cols=7)
                headers = ["Time (IST)", "Action", "Coin", "Quantity", "Value (INR)", "Reasoning", "Status"]
                self.logs_sheet.append_row(headers)
                
            try:
                self.errors_sheet = self.spreadsheet.worksheet("APIErrors")
            except Exception:
                self.errors_sheet = self.spreadsheet.add_worksheet("APIErrors", rows=1000, cols=3)
                headers = ["Time (IST)", "API Endpoint", "Error Message"]
                self.errors_sheet.append_row(headers)

            self.last_error = None
            return True
        except Exception as e:
            self.last_error = str(e)
            return False

    def load_trades(self):
        if not self.trades_sheet and not self.connect():
            return []
        try:
            records = self.trades_sheet.get_all_records()
            parsed_records = []
            for row in records:
                raw_ts = row.get("timestamp", row.get("Date & Time (IST)", 0))
                try:
                    ts = float(raw_ts)
                except ValueError:
                    try:
                        dt_obj = datetime.strptime(str(raw_ts), "%Y-%m-%d %I:%M:%S %p")
                        ist_offset = timedelta(hours=5, minutes=30)
                        utc_dt = dt_obj - ist_offset
                        ts = utc_dt.replace(tzinfo=timezone.utc).timestamp()
                    except Exception:
                        ts = time.time()
                
                pnl_val = float(row.get("net_pnl", row.get("pnl", 0)))
                pnl_pct_val = float(row.get("net_pnl_pct", row.get("pnl_pct", 0)))

                parsed_records.append({
                    "timestamp": ts,
                    "coin": str(row.get("coin", "")),
                    "entry_price": float(row.get("entry_price", 0)),
                    "exit_price": float(row.get("exit_price", 0)),
                    "invested": float(row.get("invested", 0)),
                    "pnl": pnl_val,
                    "pnl_pct": pnl_pct_val,
                    "type": str(row.get("type", "CLOSE"))
                })
            return parsed_records
        except Exception as e:
            self.last_error = f"Load Trades Error: {e}"
            return []

    def append_trade(self, trade_record):
        if not self.trades_sheet and not self.connect():
            return
        try:
            ist_offset = timedelta(hours=5, minutes=30)
            utc_dt = datetime.fromtimestamp(trade_record["timestamp"], tz=timezone.utc)
            ist_time = (utc_dt + ist_offset).strftime("%Y-%m-%d %I:%M:%S %p")
            row = [
                ist_time, trade_record["coin"], trade_record["entry_price"], trade_record["exit_price"],
                trade_record["invested"], trade_record["pnl"], trade_record["pnl_pct"], trade_record.get("type", "CLOSE")
            ]
            self.trades_sheet.append_row(row)
        except Exception as e:
            self.last_error = f"Append Trade Error: {e}"

    def append_log(self, log_entry):
        if not self.logs_sheet and not self.connect():
            return
        try:
            ist_offset = timedelta(hours=5, minutes=30)
            utc_dt = datetime.fromtimestamp(log_entry["timestamp"], tz=timezone.utc)
            ist_time = (utc_dt + ist_offset).strftime("%Y-%m-%d %I:%M:%S %p")
            row = [
                ist_time, log_entry["Action"], log_entry["Coin"], str(log_entry["Quantity"]),
                str(log_entry["Value (INR)"]), log_entry["Reasoning"], log_entry["Status"]
            ]
            self.logs_sheet.append_row(row)
        except Exception as e:
            self.last_error = f"Append Log Error: {e}"
            
    def append_api_error(self, timestamp, endpoint, error_msg):
        if not self.errors_sheet and not self.connect():
            return
        try:
            ist_offset = timedelta(hours=5, minutes=30)
            utc_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            ist_time = (utc_dt + ist_offset).strftime("%Y-%m-%d %I:%M:%S %p")
            row = [ist_time, str(endpoint), str(error_msg)[:1000]]
            self.errors_sheet.append_row(row)
        except Exception as e:
            self.last_error = f"Append API Error Failed: {e}"

gs_manager = GoogleSheetsManager()

# ==========================================
# 3. PURE PYTHON SWING ENGINE (vFinal)
# ==========================================
class GlobalBotEngine:
    _active_instances = []

    def __init__(self):
        for old_bot in GlobalBotEngine._active_instances:
            old_bot.is_running = False
        GlobalBotEngine._active_instances.clear()
        GlobalBotEngine._active_instances.append(self)

        self.is_running = False
        self.trade_log = []
        self.thread = None
        self.next_scan_epoch = 0 
        
        self.candidates = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
        self.auto_top_n = 0 
        
        self.max_budget = 1000.0
        self.candle_interval = "15m"  
        
        self.exchange_fee_pct = 0.5 
        self.tds_pct = 1.0 
        
        self.inr_balance = 0.0
        self.active_positions = {}
        self.last_prices = {}
        self.market_precision = {}
        self.market_min_qty = {}
        self.market_pair_string = {} 
        self.api_is_down = False
        
        self.completed_trades = gs_manager.load_trades()
        self.realized_pnl = sum(t.get("pnl", 0.0) for t in self.completed_trades)

    @property
    def buy_fee_multiplier(self):
        return 1 + (self.exchange_fee_pct / 100.0 * 1.18)

    @property
    def sell_fee_multiplier(self):
        return 1 - (self.exchange_fee_pct / 100.0 * 1.18) - (self.tds_pct / 100.0)

    def log_trade(self, action, coin, qty, value, reason, status):
        log_entry = {
            "timestamp": time.time(), "Action": action, "Coin": coin,
            "Quantity": qty, "Value (INR)": value, "Reasoning": reason, "Status": status
        }
        self.trade_log.insert(0, log_entry)
        if len(self.trade_log) > 100: self.trade_log.pop()
        gs_manager.append_log(log_entry)

    def start(self, candidate_str, auto_top_n, max_budget, exchange_fee_pct, tds_pct, candle_interval):
        if not self.is_running:
            self.auto_top_n = auto_top_n
            if self.auto_top_n > 0:
                self.candidates = [] 
            else:
                raw_coins = [c.strip().upper() for c in candidate_str.split(",") if c.strip()]
                self.candidates = raw_coins if raw_coins else ["BTC", "ETH", "SOL", "XRP", "DOGE"]
            
            self.max_budget = max_budget
            self.exchange_fee_pct = exchange_fee_pct
            self.tds_pct = tds_pct
            self.candle_interval = candle_interval
            
            self.is_running = True
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()

    def stop(self):
        self.is_running = False

    def get_interval_seconds(self):
        interval_map = {"1m": 60, "15m": 900, "1h": 3600, "1d": 86400}
        return interval_map.get(self.candle_interval, 900)

    def fetch_candle_data(self, coin, interval=None, limit=50):
        market_name = f"{coin}INR"
        pair = self.market_pair_string.get(market_name, f"I-{coin}_INR")
        actual_interval = interval or self.candle_interval
        url = f"https://public.coindcx.com/market_data/candles?pair={pair}&interval={actual_interval}&limit={limit}"
        data = coindcx_get_with_retry(url, max_retries=3, timeout=20)
        if isinstance(data, list) and len(data) >= 20: 
            return data
        return None

    def process_df_indicators(self, candles_data):
        df = pd.DataFrame(candles_data)
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['close'] = df['close'].astype(float)
        df['volume'] = df['volume'].astype(float)
        df = df.sort_values(by='time', ascending=True)

        df['Vol_SMA_20'] = df['volume'].rolling(window=20).mean()

        df['prev_close'] = df['close'].shift(1)
        df['tr1'] = df['high'] - df['low']
        df['tr2'] = (df['high'] - df['prev_close']).abs()
        df['tr3'] = (df['low'] - df['prev_close']).abs()
        df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
        df['ATR'] = df['tr'].rolling(window=14).mean()
        
        df['up_move'] = df['high'].diff()
        df['down_move'] = df['low'].shift(1) - df['low']
        df['+dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0.0)
        df['-dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0.0)
        
        atr_14 = df['tr'].ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        df['+di'] = 100 * (df['+dm'].ewm(alpha=1/14, min_periods=14, adjust=False).mean() / atr_14)
        df['-di'] = 100 * (df['-dm'].ewm(alpha=1/14, min_periods=14, adjust=False).mean() / atr_14)
        adx_dx = 100 * abs(df['+di'] - df['-di']) / (df['+di'] + df['-di'])
        df['ADX'] = adx_dx.ewm(alpha=1/14, min_periods=14, adjust=False).mean()

        delta = df['close'].diff()
        gain = delta.clip(lower=0)
        loss = -1 * delta.clip(upper=0)
        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()
        rs = avg_gain / avg_loss
        df['RSI'] = 100 - (100 / (1 + rs))

        df['SMA_20'] = df['close'].rolling(window=20).mean()
        df['STD_20'] = df['close'].rolling(window=20).std()
        df['Lower_BB'] = df['SMA_20'] - (df['STD_20'] * 2)
        
        return df

    def check_htf_trend(self, coin):
        candles = self.fetch_candle_data(coin, interval="1h", limit=250)
        time.sleep(0.4) 
        if candles and len(candles) >= 200:
            try:
                df = pd.DataFrame(candles)
                df['close'] = df['close'].astype(float)
                df = df.sort_values(by='time', ascending=True)
                df['EMA_200'] = df['close'].ewm(span=200, adjust=False).mean()
                latest_close = df['close'].iloc[-1]
                latest_ema = df['EMA_200'].iloc[-1]
                return latest_close > latest_ema 
            except Exception:
                pass
        return True 

    def _run_loop(self):
        last_candle_block = 0
        while self.is_running:
            try:
                success = self._fetch_market_state()
                self.api_is_down = not success
                
                if success:
                    self._monitor_live_exits()
                
                interval_sec = self.get_interval_seconds()
                now = time.time()
                current_block = int(now) // interval_sec
                next_close = (current_block + 1) * interval_sec
                
                self.next_scan_epoch = next_close + 1.5
                
                if current_block > last_candle_block and success:
                    time.sleep(1.5) 
                    self._scan_for_entries()
                    last_candle_block = current_block

            except Exception as e:
                self.log_trade("ERROR", "SYSTEM", "0", "₹0.00", f"Engine Crash: {str(e)}", "Failed")
            
            sleep_duration = 30 if self.api_is_down else 10
            for _ in range(sleep_duration):
                if not self.is_running: break
                time.sleep(1)

    def _fetch_market_state(self):
        balance_body = {"timestamp": int(round(time.time() * 1000))}
        balances_data = coindcx_auth_post("/exchange/v1/users/balances", balance_body, max_retries=3, timeout=20)
        
        if not balances_data or "error" in balances_data:
            return False 
            
        actual_balances = {}
        if isinstance(balances_data, list):
            for b in balances_data:
                avail_bal = float(b.get('balance', 0))
                locked_bal = float(b.get('locked_balance', 0))
                total_bal = avail_bal + locked_bal
                
                if total_bal > 0:
                    actual_balances[b['currency']] = total_bal
                    if b['currency'] == 'INR': 
                        self.inr_balance = avail_bal 

        markets = coindcx_get_with_retry("https://api.coindcx.com/exchange/v1/markets_details", max_retries=3, timeout=20)
        if not markets: return False
        
        if isinstance(markets, list):
            self.market_precision = {m.get('coindcx_name'): int(m.get('target_currency_precision', 5)) for m in markets if m.get('coindcx_name')}
            self.market_min_qty = {m.get('coindcx_name'): float(m.get('min_quantity', 0.0001)) for m in markets if m.get('coindcx_name')}
            self.market_pair_string = {m.get('coindcx_name'): m.get('pair') for m in markets if m.get('coindcx_name') and m.get('pair')}
            
        tickers = coindcx_get_with_retry("https://api.coindcx.com/exchange/ticker", max_retries=3, timeout=20)
        if not tickers: return False
        
        if isinstance(tickers, list):
            self.last_prices = {t['market']: float(t['last_price']) for t in tickers if 'market' in t}
            
            if self.auto_top_n > 0 and len(self.candidates) == 0:
                inr_markets = []
                stablecoins = {'USDT', 'USDC', 'FDUSD', 'TUSD', 'DAI', 'BUSD', 'USDD', 'PYUSD', 'USDE', 'FRAX', 'USDP', 'CUSD'}
                for t in tickers:
                    market = t.get('market', '')
                    if market.endswith('INR'):
                        base_coin = market[:-3]
                        if base_coin not in stablecoins:
                            try:
                                vol = float(t.get('volume', 0))
                                price = float(t.get('last_price', 0))
                                inr_markets.append((base_coin, vol * price))
                            except Exception: pass
                inr_markets.sort(key=lambda x: x[1], reverse=True)
                self.candidates = [m[0] for m in inr_markets[:self.auto_top_n]]
                self.log_trade("SYSTEM", "AUTO-SCAN", str(self.auto_top_n), "₹0.00", f"Auto-selected top {self.auto_top_n} volatile coins.", "Info")

        for coin in self.candidates:
            if coin in actual_balances and coin not in self.active_positions:
                market = f"{coin}INR"
                curr_price = self.last_prices.get(market)
                if curr_price:
                    value = actual_balances[coin] * curr_price
                    if value > 50:
                        invested_with_fees = value * self.buy_fee_multiplier
                        breakeven = curr_price * (self.buy_fee_multiplier / self.sell_fee_multiplier)
                        
                        self.active_positions[coin] = {
                            "qty": actual_balances[coin],
                            "entry_price": curr_price,
                            "invested": invested_with_fees,
                            "sl_price": curr_price * 0.90, # 10% default loose SL until calculated
                            "peak_price": curr_price,
                            "atr": curr_price * 0.02,
                            "breakeven_price": breakeven,
                            "risk_free_active": False,
                            "buy_time": time.time() - 200
                        }
                        self.log_trade("SYNC BUY", coin, f"{actual_balances[coin]:.4f}", f"₹{invested_with_fees:.2f}", "Detected external wallet asset.", "Wallet Sync")

        for coin in list(self.active_positions.keys()):
            pos = self.active_positions[coin]
            if time.time() - pos.get('buy_time', 0) < 180:
                continue
                
            if coin not in actual_balances or (actual_balances[coin] * self.last_prices.get(f"{coin}INR", 0) < 50):
                self.log_trade("SYNC SELL", coin, "0", "₹0.00", "Asset no longer in wallet.", "Wallet Sync")
                del self.active_positions[coin]
                
        return True 

    def _monitor_live_exits(self):
        # 100% PURE PYTHON EXITS. No LLM delays. Instant execution.
        for coin in list(self.active_positions.keys()):
            market = f"{coin}INR"
            curr_price = self.last_prices.get(market)
            if not curr_price: continue
            
            pos = self.active_positions[coin]
            
            if curr_price > pos['peak_price']:
                pos['peak_price'] = curr_price
                
            # Trailing Stop: 2x ATR behind the peak price
            dynamic_sl = pos['peak_price'] - (2.0 * pos['atr'])
            
            # The Tax Shield Pivot (Activates at +3% profit margin)
            pivot_threshold = pos['breakeven_price'] * 1.03
            
            if not pos['risk_free_active'] and curr_price > pivot_threshold:
                pos['risk_free_active'] = True
                self.log_trade("RISK-FREE PIVOT", coin, "0", f"₹{curr_price:.2f}", "Price cleared 3% profit margin. Stop-Loss mechanically locked above breakeven.", "Shield Up")

            if pos['risk_free_active']:
                # Floor the SL at Breakeven + 0.2% so a loss is mathematically impossible
                guaranteed_sl = pos['breakeven_price'] * 1.002
                pos['sl_price'] = max(pos['sl_price'], dynamic_sl, guaranteed_sl)
            else:
                pos['sl_price'] = max(pos['sl_price'], dynamic_sl)

            if curr_price <= pos['sl_price']:
                reason = "Trailing Stop Loss Hit (Secured Profit)." if pos['risk_free_active'] else "Initial Stop Loss Hit (Cut Loss to save capital)."
                self._execute_sell(coin, "TSL EXIT", reason)

    def _execute_sell(self, coin, action_type, reasoning):
        pos = self.active_positions[coin]
        market = f"{coin}INR"
        curr_price = self.last_prices.get(market, pos['entry_price'])
        
        precision = self.market_precision.get(market, 5)
        multiplier = 10 ** precision
        sell_qty = math.floor(pos['qty'] * multiplier) / multiplier
        
        gross_value = sell_qty * curr_price
        
        if gross_value >= 100.0:
            formatted_qty = f"{sell_qty:.{precision}f}"
            order_body = {
                "side": "sell", "order_type": "limit_order", "market": market,
                "price_per_unit": float(curr_price), "total_quantity": float(formatted_qty), 
                "timestamp": int(round(time.time() * 1000))
            }
            res = coindcx_auth_post("/exchange/v1/orders/create", order_body, max_retries=3, timeout=20)
            if "orders" in res or "id" in res:
                net_received = gross_value * self.sell_fee_multiplier
                net_profit = net_received - pos['invested']
                net_pnl_pct = (net_profit / pos['invested']) * 100
                
                self.realized_pnl += net_profit
                
                trade_record = {
                    "timestamp": time.time(), "coin": coin, "entry_price": pos['entry_price'], "exit_price": curr_price,
                    "invested": pos['invested'], "pnl": net_profit, "pnl_pct": net_pnl_pct, "type": action_type
                }
                self.completed_trades.append(trade_record)
                gs_manager.append_trade(trade_record)
                
                self.log_trade("SELL", coin, formatted_qty, f"₹{net_received:.2f}", reasoning, "Success")
                del self.active_positions[coin]
            else:
                err = res.get("error", "API Error")
                self.log_trade("SELL FAILED", coin, formatted_qty, f"₹{gross_value:.2f}", f"Rejected: {err}", "Error")

    def _scan_for_entries(self):
        current_invested = sum(p['invested'] for p in self.active_positions.values())
        available_budget = self.max_budget - current_invested
        
        if available_budget >= 110.0:
            best_candidate_coin = None
            best_candidate_market = None
            best_candidate_price = 0
            best_atr = 0.0
            best_summary = ""
            best_setup_score = -999.0
            
            rejected_by_macro = []
            rejected_by_adx = []
            rejected_by_vol = []

            for coin in self.candidates:
                if coin in self.active_positions: continue
                
                market = f"{coin}INR"
                curr_price = self.last_prices.get(market)
                if not curr_price: continue
                
                if not self.check_htf_trend(coin): 
                    rejected_by_macro.append(coin)
                    continue
                
                candles_15m = self.fetch_candle_data(coin, interval="15m", limit=50)
                time.sleep(0.4)
                
                if candles_15m:
                    try:
                        df_15m = self.process_df_indicators(candles_15m)
                        
                        latest_rsi = df_15m['RSI'].iloc[-1]
                        latest_adx = df_15m['ADX'].iloc[-1]
                        latest_vol = df_15m['volume'].iloc[-1]
                        latest_vol_sma = df_15m['Vol_SMA_20'].iloc[-1]
                        latest_atr = df_15m['ATR'].iloc[-1]
                        
                        # PURE MATHEMATICAL ENTRY GATES
                        if pd.isna(latest_adx) or latest_adx < 25.0:
                            rejected_by_adx.append(coin)
                            continue
                            
                        if latest_vol < (1.5 * latest_vol_sma):
                            rejected_by_vol.append(coin)
                            continue

                        # Score based on momentum strength + volume surge
                        vol_multiplier = latest_vol / latest_vol_sma
                        setup_score = latest_adx + (vol_multiplier * 10)

                        if setup_score > best_setup_score:
                            best_setup_score = setup_score
                            best_candidate_coin = coin
                            best_candidate_market = market
                            best_candidate_price = curr_price
                            best_atr = latest_atr
                            
                            # Create a clean, human-readable summary for the LLM
                            best_summary = f"""
                            Asset: {coin}
                            Current Price: {curr_price}
                            Macro Trend: Bullish (Price is above 1-Hour 200 EMA)
                            15m ADX (Trend Strength): {latest_adx:.1f} (Above 25 = Strong Trend)
                            15m RSI: {latest_rsi:.1f}
                            Volume Surge: {vol_multiplier:.1f}x higher than the 20-candle average.
                            """
                    except Exception as e:
                        continue

            if best_candidate_coin and best_summary:
                # SINGLE LLM SANITY CHECK (Llama 3.3)
                client = Groq(api_key=GROQ_API_KEY)
                system_prompt = """You are an elite hedge fund risk manager. The Python quant engine has already verified that this coin is in a massive, volume-backed mathematical breakout.
                Your job is to read the summary of the technical indicators. 
                If the data shows strong momentum (ADX > 25) and high volume, output 'BUY' to authorize the trade.
                Respond ONLY in JSON format: {"action": "BUY" or "HOLD", "reasoning": "1 short sentence"}"""
                
                try:
                    response = client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": f"Review this mathematical breakout:\n{best_summary}"}
                        ],
                        response_format={"type": "json_object"}, temperature=0.1
                    )
                    decision_data = json.loads(response.choices[0].message.content)
                    action = decision_data.get("action", "HOLD").upper()
                    reasoning = decision_data.get("reasoning", "Llama 3.3 Sanity Check passed.")

                    if action == "BUY":
                        precision = self.market_precision.get(best_candidate_market, 5)
                        multiplier = 10 ** precision
                        
                        usable_cash = min(available_budget, self.inr_balance)
                        target_cost_excluding_fees = usable_cash / self.buy_fee_multiplier
                        
                        raw_crypto = target_cost_excluding_fees / best_candidate_price
                        buy_crypto_amount = math.floor(raw_crypto * multiplier) / multiplier
                        
                        min_required_qty = self.market_min_qty.get(best_candidate_market, 0.0001)
                        if buy_crypto_amount < min_required_qty: 
                            buy_crypto_amount = min_required_qty
                            
                        actual_cost = buy_crypto_amount * best_candidate_price
                        if actual_cost < 105.0:
                            required_for_105 = 105.0 / best_candidate_price
                            buy_crypto_amount = math.ceil(required_for_105 * multiplier) / multiplier
                            actual_cost = buy_crypto_amount * best_candidate_price
                            
                        total_invested_with_fees = actual_cost * self.buy_fee_multiplier

                        if total_invested_with_fees > self.inr_balance:
                            self.log_trade("BUY SKIPPED", best_candidate_coin, "0", f"₹{total_invested_with_fees:.2f}", "Insufficient real INR Balance in wallet.", "Failed")
                        elif total_invested_with_fees > (available_budget + 15.0): 
                            self.log_trade("BUY SKIPPED", best_candidate_coin, "0", f"₹{total_invested_with_fees:.2f}", f"Exchange min qty exceeds your remaining budget.", "Limit Enforced")
                        else:
                            formatted_qty = f"{buy_crypto_amount:.{precision}f}"
                            order_body = {
                                "side": "buy", "order_type": "limit_order", "market": best_candidate_market,
                                "price_per_unit": float(best_candidate_price), "total_quantity": float(formatted_qty), 
                                "timestamp": int(round(time.time() * 1000))
                            }
                            res = coindcx_auth_post("/exchange/v1/orders/create", order_body, max_retries=3, timeout=20)
                            if "orders" in res or "id" in res:
                                breakeven_price = best_candidate_price * (self.buy_fee_multiplier / self.sell_fee_multiplier)
                                initial_sl = best_candidate_price - (2.0 * best_atr) # Give it room to breathe
                                
                                self.active_positions[best_candidate_coin] = {
                                    "qty": buy_crypto_amount, 
                                    "entry_price": best_candidate_price,
                                    "invested": total_invested_with_fees, 
                                    "sl_price": initial_sl,
                                    "peak_price": best_candidate_price,
                                    "atr": best_atr,
                                    "breakeven_price": breakeven_price,
                                    "risk_free_active": False,
                                    "buy_time": time.time()
                                }
                                self.log_trade("ALL-IN BUY", best_candidate_coin, formatted_qty, f"₹{total_invested_with_fees:.2f}", reasoning, "Success")
                            else:
                                err = res.get("error", "API Error")
                                self.log_trade("BUY FAILED", best_candidate_coin, formatted_qty, f"₹{actual_cost:.2f}", f"Rejected: {err}", "Error")
                    else:
                        self.log_trade("AI HOLD (ENTRY)", best_candidate_coin, "0", f"₹{best_candidate_price:.2f}", reasoning, "AI Denied")
                except Exception as e:
                    pass
            else:
                if rejected_by_macro:
                    coins = f"{len(rejected_by_macro)} assets" if len(rejected_by_macro) > 4 else ", ".join(rejected_by_macro)
                    self.log_trade("MACRO REJECT", "ALL", "0", "₹0.00", f"Assets {coins} are trading below 1H 200 EMA.", "Filter Active")
                elif rejected_by_adx:
                    coins = f"{len(rejected_by_adx)} assets" if len(rejected_by_adx) > 4 else ", ".join(rejected_by_adx)
                    self.log_trade("MOMENTUM REJECT", "ALL", "0", "₹0.00", f"Assets {coins} have ADX < 25 (Chop / Weak Trend).", "Filter Active")
                elif rejected_by_vol:
                    coins = f"{len(rejected_by_vol)} assets" if len(rejected_by_vol) > 4 else ", ".join(rejected_by_vol)
                    self.log_trade("VOLUME REJECT", "ALL", "0", "₹0.00", f"Assets {coins} lack 1.5x volume surge.", "Filter Active")
                else:
                    self.log_trade("SCAN SKIPPED", "ALL", "0", "₹0.00", "No explosive setups found.", "Standby")

# Cache Buster v67
@st.cache_resource
def get_bot_engine_v67():
    return GlobalBotEngine()

bot = get_bot_engine_v67()

# ==========================================
# 4. STREAMLIT UI CONFIG & STYLING
# ==========================================
st.set_page_config(page_title="Apex Engine", page_icon="📈", layout="wide")

def inject_custom_css():
    st.markdown("""
    <style>
        .stApp { background-color: #f8f9fa; color: #202124; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
        .block-container { padding-top: 2rem !important; }
        [data-testid="stSidebar"] { background-color: #ffffff; border-right: 1px solid #e0e0e0; }
        .metric-card { background: #ffffff; border: 1px solid #eaebed; border-radius: 12px; padding: 20px; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04); margin-bottom: 12px; transition: transform 0.2s ease, box-shadow 0.2s ease; }
        .metric-card:hover { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(0, 0, 0, 0.08); border-color: #1a73e8; }
        .metric-title { font-size: 0.85rem; color: #5f6368; text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600; margin-bottom: 8px; }
        .metric-value { font-size: 1.8rem; font-weight: 700; color: #202124; }
        .metric-sub { font-size: 0.85rem; margin-top: 6px; font-weight: 500; color: #5f6368; }
        .text-green { color: #0f9d58 !important; }
        .text-red { color: #d23f31 !important; }
        .text-blue { color: #1a73e8 !important; }
        .stProgress > div > div > div > div { background-color: #1a73e8; }
        .settings-box { background: #ffffff; border: 1px solid #eaebed; border-radius: 12px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.02); margin-bottom: 24px; }
        .stTabs [data-baseweb="tab-list"] { background-color: transparent; border-bottom: 1px solid #e0e0e0; gap: 16px; }
        .stTabs [data-baseweb="tab"] { height: 48px; font-weight: 600; color: #5f6368; }
        .stTabs [aria-selected="true"] { color: #1a73e8 !important; border-bottom-color: #1a73e8 !important; }
    </style>
    """, unsafe_allow_html=True)
inject_custom_css()

# ==========================================
# 5. SIDEBAR NAVIGATION
# ==========================================
st.sidebar.markdown("## 📈 **Apex Engine**")
page = st.sidebar.radio("Navigation", ["📊 Live Dashboard", "⚙️ Bot Engine & Settings"])

st.sidebar.markdown("---")
st.sidebar.markdown("#### System Health")

if bot.is_running:
    if getattr(bot, "api_is_down", False):
        st.sidebar.markdown("""<div style="background: #fce8e6; border: 1px solid #fad2cf; border-radius: 8px; padding: 12px; color: #c5221f; font-weight: 600; font-size: 0.9rem; text-align: center;">🔴 COINDCX API DOWN/LAGGING</div>""", unsafe_allow_html=True)
    else:
        st.sidebar.markdown("""<div style="background: #e6f4ea; border: 1px solid #ceead6; border-radius: 8px; padding: 12px; color: #137333; font-weight: 600; font-size: 0.9rem; text-align: center;">🟢 APEX ENGINE ACTIVE</div>""", unsafe_allow_html=True)
else:
    st.sidebar.markdown("""<div style="background: #fce8e6; border: 1px solid #fad2cf; border-radius: 8px; padding: 12px; color: #c5221f; font-weight: 600; font-size: 0.9rem; text-align: center;">🔴 ENGINE STOPPED</div>""", unsafe_allow_html=True)

st.sidebar.markdown("<br>", unsafe_allow_html=True)
st.sidebar.caption("Version 67.0 (Python-Dominant Swing Trader)")

# ==========================================
# 6. ROUTED PAGE VIEWS
# ==========================================

if page == "⚙️ Bot Engine & Settings":
    st.title("⚙️ Engine Settings & Live Logs")
    st.markdown("Configure the automated allocation limits.")
    
    st.markdown('<div class="settings-box">', unsafe_allow_html=True)
    st.markdown("#### 🎯 Execution Strategy")
    col1, col2 = st.columns(2)
    
    with col1:
        scan_mode = st.radio("Asset Selection Mode", ["Manual List", "Auto Top Volume Coins"], horizontal=True)
        if scan_mode == "Manual List":
            default_candidates = ", ".join(bot.candidates) if bot.candidates else "BTC, ETH, SOL, XRP, DOGE"
            candidate_input = st.text_input("Monitored Assets (Comma Separated)", value=default_candidates)
            top_n = 0
        else:
            top_n = st.number_input(
                "Top Coins Limit (By Volume)", 
                min_value=1, max_value=20, 
                value=5 if bot.auto_top_n == 0 else bot.auto_top_n,
                help="Automatically scans CoinDCX for the highest 24-hour volume INR pairs (excluding stablecoins)."
            )
            candidate_input = ""
            
    with col2:
        timeframes = ["1m", "15m", "1h", "1d"]
        default_idx = timeframes.index(bot.candle_interval) if bot.candle_interval in timeframes else 1
        candle_interval = st.selectbox(
            "Candle Timeframe (Trigger Execution)", 
            timeframes, index=default_idx
        )

    st.markdown("<br>#### 💰 Capital Allocator", unsafe_allow_html=True)
    colA, colB, colC = st.columns(3)
    with colA:
        max_bud = st.number_input("Maximum Capital Strike (INR)", min_value=150.0, value=float(bot.max_budget), step=100.0)
    with colB:
        exchange_fee_pct = st.number_input("Exchange Fee % (e.g. 0.5)", min_value=0.0, value=float(bot.exchange_fee_pct), step=0.1)
    with colC:
        tds_pct = st.number_input("Govt TDS % (e.g. 1.0)", min_value=0.0, value=float(bot.tds_pct), step=0.1)
        
    st.info(f"🛡️ **Tax Shield Active:** The bot will dynamically size up to **₹{max_bud:.2f}** into the best confirmed breakout. Exits are handled 100% mechanically by Python to prevent LLM latency delays.", icon="🎯")
    st.markdown('</div>', unsafe_allow_html=True)

    ctrl_col1, ctrl_col2, ctrl_col3 = st.columns(3)
    with ctrl_col1:
        if st.button("▶️ Start Apex Engine", type="primary", use_container_width=True, disabled=bot.is_running):
            bot.start(candidate_input, top_n, max_bud, exchange_fee_pct, tds_pct, candle_interval)
            st.rerun()
    with ctrl_col2:
        if st.button("⏹️ Stop Engine", use_container_width=True, disabled=not bot.is_running):
            bot.stop()
            st.rerun()
    with ctrl_col3:
        if st.button("🧹 Clear Logs", use_container_width=True):
            bot.trade_log.clear()
            st.rerun()

    st.markdown("---")
    
    @st.fragment(run_every="10s")
    def bot_logs_view():
        if bot.is_running:
            asset_str = f"Auto-Scanning Top {bot.auto_top_n} Coins" if bot.auto_top_n > 0 and len(bot.candidates) == 0 else f"{len(bot.candidates)} assets ({', '.join(bot.candidates)})"
            
            timer_html = f"""
            <!DOCTYPE html>
            <html>
            <head>
            <style>
                body {{ margin: 0; padding: 0; overflow: hidden; background-color: transparent; }}
                .info-box {{
                    background-color: #e8f0fe;
                    padding: 16px;
                    border-radius: 8px;
                    color: #1967d2;
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                    font-size: 14.5px;
                    border: 1px solid #d2e3fc;
                    display: flex;
                    align-items: center;
                }}
                .error-box {{
                    background-color: #fce8e6; color: #c5221f; border: 1px solid #fad2cf;
                }}
            </style>
            </head>
            <body>
                <div class="info-box {'error-box' if getattr(bot, 'api_is_down', False) else ''}">
                    <span><strong>⚡ System Active:</strong> { "CoinDCX API Outage Detected. Retrying..." if getattr(bot, 'api_is_down', False) else f"Breakout Monitor LIVE. {asset_str}" } <span id="live-timer"></span></span>
                </div>
                <script>
                    var targetEpoch = {bot.next_scan_epoch * 1000};
                    function updateTimer() {{
                        var now = new Date().getTime();
                        var distance = targetEpoch - now;
                        var el = document.getElementById('live-timer');
                        if (distance <= 0) {{
                            el.innerHTML = " | ⏳ Target Locked. Aggregating...";
                        }} else {{
                            var m = Math.floor(distance / 60000);
                            var s = Math.floor((distance % 60000) / 1000);
                            var mStr = m < 10 ? "0" + m : m;
                            var sStr = s < 10 ? "0" + s : s;
                            el.innerHTML = " | ⏳ Next Scan in: " + mStr + ":" + sStr;
                        }}
                    }}
                    updateTimer();
                    setInterval(updateTimer, 1000);
                </script>
            </body>
            </html>
            """
            components.html(timer_html, height=55)
            
        else:
            st.warning("⚠️ **System Idle:** Awaiting execution. Click 'Start Apex Engine' to commence trading.")
            
        st.subheader("📡 Real-Time Execution Log")
        if not bot.trade_log:
            st.caption("No session activity logged yet.")
        else:
            ist_offset = timedelta(hours=5, minutes=30)
            formatted_logs = []
            for entry in bot.trade_log:
                e = entry.copy()
                if "timestamp" in e:
                    utc_dt = datetime.fromtimestamp(e["timestamp"], tz=timezone.utc)
                    e["Time (IST)"] = (utc_dt + ist_offset).strftime("%I:%M:%S %p")
                    del e["timestamp"]
                formatted_logs.append(e)
                
            df_logs = pd.DataFrame(formatted_logs)
            if "Time (IST)" in df_logs.columns:
                cols = ["Time (IST)"] + [c for c in df_logs.columns if c != "Time (IST)"]
                df_logs = df_logs[cols]
                
            st.dataframe(df_logs, use_container_width=True, hide_index=True)

    bot_logs_view()

elif page == "📊 Live Dashboard":
    st.title("📊 Financial Overview")
    st.markdown("Real-time portfolio tracking and historical performance analytics.")

    @st.fragment(run_every="10s")
    def portfolio_view():
        live_inr = bot.inr_balance
        live_prices = bot.last_prices.copy()
        
        balance_body = {"timestamp": int(round(time.time() * 1000))}
        balances_data = coindcx_auth_post("/exchange/v1/users/balances", balance_body, max_retries=1, timeout=10)
        if isinstance(balances_data, list):
            for b in balances_data:
                if b.get('currency') == 'INR':
                    live_inr = float(b.get('balance', 0))
                    
        tickers = coindcx_get_with_retry("https://api.coindcx.com/exchange/ticker", max_retries=1, timeout=10)
        if isinstance(tickers, list):
            for t in tickers:
                if 'market' in t: live_prices[t['market']] = float(t['last_price'])
        
        ist_offset = timedelta(hours=5, minutes=30)
        now_ist = datetime.now(timezone.utc) + ist_offset
        today_date_ist = now_ist.date()
        yesterday_date_ist = today_date_ist - timedelta(days=1)
        
        current_year = now_ist.year
        current_month = now_ist.month
        
        last_month_year = current_year - 1 if current_month == 1 else current_year
        last_month_num = 12 if current_month == 1 else current_month - 1

        two_years_ago = now_ist - timedelta(days=730)

        period_stats = {
            "today": {"profit": 0.0, "loss": 0.0, "count": 0},
            "yesterday": {"profit": 0.0, "loss": 0.0, "count": 0},
            "this_month": {"profit": 0.0, "loss": 0.0, "count": 0},
            "last_month": {"profit": 0.0, "loss": 0.0, "count": 0},
            "all_time": {"profit": 0.0, "loss": 0.0, "count": 0}
        }

        for trade in bot.completed_trades:
            trade_dt_ist = datetime.fromtimestamp(trade["timestamp"], tz=timezone.utc) + ist_offset
            trade_date = trade_dt_ist.date()
            
            net_pnl = float(trade.get("pnl", 0.0))

            if trade_dt_ist >= two_years_ago:
                if net_pnl >= 0: period_stats["all_time"]["profit"] += net_pnl
                else: period_stats["all_time"]["loss"] += abs(net_pnl)
                period_stats["all_time"]["count"] += 1

                if trade_date == today_date_ist:
                    if net_pnl >= 0: period_stats["today"]["profit"] += net_pnl
                    else: period_stats["today"]["loss"] += abs(net_pnl)
                    period_stats["today"]["count"] += 1

                if trade_date == yesterday_date_ist:
                    if net_pnl >= 0: period_stats["yesterday"]["profit"] += net_pnl
                    else: period_stats["yesterday"]["loss"] += abs(net_pnl)
                    period_stats["yesterday"]["count"] += 1

                if trade_dt_ist.year == current_year and trade_dt_ist.month == current_month:
                    if net_pnl >= 0: period_stats["this_month"]["profit"] += net_pnl
                    else: period_stats["this_month"]["loss"] += abs(net_pnl)
                    period_stats["this_month"]["count"] += 1

                if trade_dt_ist.year == last_month_year and trade_dt_ist.month == last_month_num:
                    if net_pnl >= 0: period_stats["last_month"]["profit"] += net_pnl
                    else: period_stats["last_month"]["loss"] += abs(net_pnl)
                    period_stats["last_month"]["count"] += 1

        current_invested = sum(p['invested'] for p in bot.active_positions.values())
        unrealized_net_pnl = 0.0
        
        for coin, pos in bot.active_positions.items():
            live_p = live_prices.get(f"{coin}INR", pos['entry_price'])
            net_live_value = (pos['qty'] * live_p) * bot.sell_fee_multiplier
            unrealized_net_pnl += (net_live_value - pos['invested'])

        budget_pct = min(1.0, current_invested / bot.max_budget) if bot.max_budget > 0 else 0.0

        m_col1, m_col2, m_col3 = st.columns(3)
        with m_col1:
            st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-title">Available Capital</div>
                    <div class="metric-value">₹{live_inr:,.2f}</div>
                    <div class="metric-sub text-blue">Unallocated Exchange Wallet</div>
                </div>
            """, unsafe_allow_html=True)
            
        with m_col2:
            st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-title">Budget Utilized (With Fees)</div>
                    <div class="metric-value">₹{current_invested:,.2f}</div>
                    <div class="metric-sub">Max Allocation Limit: ₹{bot.max_budget:,.2f}</div>
                    <div style="margin-top: 8px;"></div>
                </div>
            """, unsafe_allow_html=True)
            st.progress(budget_pct)
            
        with m_col3:
            pnl_color = "text-green" if unrealized_net_pnl >= 0 else "text-red"
            pnl_pct_str = f"{(unrealized_net_pnl/current_invested*100):.2f}%" if current_invested else "0.00%"
            st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-title">True Floating P&L (Net of Tax)</div>
                    <div class="metric-value {pnl_color}">₹{unrealized_net_pnl:,.2f}</div>
                    <div class="metric-sub {pnl_color}">{pnl_pct_str} Total Unsold Net Return</div>
                </div>
            """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        st.subheader("📈 NET Performance History (Post-TDS & Fees)")
        tab_today, tab_yesterday, tab_this_m, tab_last_m, tab_all = st.tabs(["Today", "Yesterday", "This Month", "Last Month", "All-Time"])

        def render_period_metrics(p_data):
            net = p_data["profit"] - p_data["loss"]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Net Winning Trades", f"₹{p_data['profit']:,.2f}")
            c2.metric("Net Losing Trades", f"₹{p_data['loss']:,.2f}")
            c3.metric("Total Net Return", f"₹{net:,.2f}", delta=f"₹{net:,.2f}")
            c4.metric("Trades Executed", f"{p_data['count']}")

        with tab_today: render_period_metrics(period_stats["today"])
        with tab_yesterday: render_period_metrics(period_stats["yesterday"])
        with tab_this_m: render_period_metrics(period_stats["this_month"])
        with tab_last_m: render_period_metrics(period_stats["last_month"])
        with tab_all: render_period_metrics(period_stats["all_time"])

        st.markdown("<br>", unsafe_allow_html=True)
        st.subheader("💼 Active Holdings")
        
        if not bot.active_positions:
            st.info("No active positions held. Waiting for a mathematically confirmed volume breakout.")
        else:
            pos_data = []
            for coin, pos in bot.active_positions.items():
                curr_price = live_prices.get(f"{coin}INR", pos['entry_price'])
                
                net_live_value = (pos['qty'] * curr_price) * bot.sell_fee_multiplier
                net_pnl = net_live_value - pos['invested']
                net_pnl_pct = (net_pnl / pos['invested']) * 100
                
                shield_status = "🟢 ACTIVE" if pos.get('risk_free_active', False) else "🔴 WAITING"
                
                pos_data.append({
                    "Asset": coin,
                    "Current Price": f"₹{curr_price:.2f}",
                    "Avg Entry": f"₹{pos['entry_price']:.2f}",
                    "Actual Cost": f"₹{pos['invested']:.2f}",
                    "Net Return %": f"{net_pnl_pct:.2f}%",
                    "Risk-Free Shield": shield_status,
                    "Current Trailing SL": f"₹{pos.get('sl_price', 0):.2f}"
                })
            st.dataframe(pd.DataFrame(pos_data), use_container_width=True)

        st.markdown("<br>", unsafe_allow_html=True)
        st.subheader("📂 Settled Transactions")
        if not bot.completed_trades:
            st.caption("No completed trades recorded in history file yet.")
        else:
            history_rows = []
            for trade in reversed(bot.completed_trades):
                t_dt = (datetime.fromtimestamp(trade["timestamp"], tz=timezone.utc) + ist_offset).strftime("%Y-%m-%d %I:%M:%S %p")
                history_rows.append({
                    "Date & Time": t_dt,
                    "Asset": trade["coin"],
                    "Action": trade.get("type", "CLOSE"),
                    "Amount": f"₹{trade['invested']:.2f}",
                    "Entry": f"₹{trade['entry_price']:.2f}",
                    "Exit": f"₹{trade['exit_price']:.2f}",
                    "Net PnL": f"₹{trade['pnl']:.2f}",
                    "Net Return %": f"{trade.get('pnl_pct', 0.0):.2f}%"
                })
            st.dataframe(pd.DataFrame(history_rows), use_container_width=True, hide_index=True)

    portfolio_view()
