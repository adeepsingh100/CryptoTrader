import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import json
import time
import hmac
import hashlib
import requests
import math
import threading
import os
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

def coindcx_auth_post(endpoint, body):
    url = f"https://api.coindcx.com{endpoint}"
    secret_bytes = bytes(API_SECRET, 'utf-8')
    json_body = json.dumps(body, separators=(',', ':'))
    signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()
    
    headers = {
        'Content-Type': 'application/json',
        'X-AUTH-APIKEY': API_KEY,
        'X-AUTH-SIGNATURE': signature
    }
    
    try:
        res = requests.post(url, data=json_body, headers=headers)
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
        log_api_failure(endpoint, str(e))
        return {"error": str(e)}

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
                    "type": str(row.get("type", "CLOSE")),
                    "tp_price": float(row.get("tp_price", 0)) if "tp_price" in row else 0.0,
                    "sl_price": float(row.get("sl_price", 0)) if "sl_price" in row else 0.0
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

def log_api_failure(endpoint, error_msg):
    ts = time.time()
    ist_offset = timedelta(hours=5, minutes=30)
    ist_time = (datetime.fromtimestamp(ts, tz=timezone.utc) + ist_offset).strftime("%Y-%m-%d %I:%M:%S %p")
    try:
        with open("api-failures.log", "a") as f:
            f.write(f"[{ist_time}] {endpoint} - {error_msg}\n")
    except Exception:
        pass
    if 'gs_manager' in globals():
        gs_manager.append_api_error(ts, endpoint, error_msg)

# ==========================================
# 3. DUAL TIMEFRAME SPLIT ENGINE
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
        
        self.max_budget = 500.0
        self.trade_amount = 125.0
        self.candle_interval = "15m"  
        
        self.exchange_fee_pct = 0.5 
        self.tds_pct = 1.0 
        
        self.inr_balance = 0.0
        self.active_positions = {}
        self.last_prices = {}
        self.market_precision = {}
        self.market_min_qty = {}
        self.market_pair_string = {} 
        
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

    def start(self, candidate_str, auto_top_n, max_budget, trade_amount, exchange_fee_pct, tds_pct, candle_interval):
        if not self.is_running:
            self.auto_top_n = auto_top_n
            if self.auto_top_n > 0:
                self.candidates = [] 
            else:
                raw_coins = [c.strip().upper() for c in candidate_str.split(",") if c.strip()]
                self.candidates = raw_coins if raw_coins else ["BTC", "ETH", "SOL", "XRP", "DOGE"]
            
            self.max_budget = max_budget
            self.trade_amount = trade_amount
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

    def fetch_candle_data(self, coin, interval=None, limit=40):
        market_name = f"{coin}INR"
        pair = self.market_pair_string.get(market_name, f"B-{coin}_INR")
        actual_interval = interval or self.candle_interval
        url = f"https://public.coindcx.com/market_data/candles?pair={pair}&interval={actual_interval}&limit={limit}"
        try:
            res = requests.get(url, timeout=10)
            res.raise_for_status()
            data = res.json()
            if isinstance(data, list) and len(data) >= 20: return data
            else: log_api_failure(url, f"Unexpected format or insufficient candles: {data}")
        except Exception as e:
            log_api_failure(url, str(e))
        return None

    def process_df_indicators(self, candles_data):
        df = pd.DataFrame(candles_data)
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['close'] = df['close'].astype(float)
        df = df.sort_values(by='time', ascending=True)

        df['prev_close'] = df['close'].shift(1)
        df['tr1'] = df['high'] - df['low']
        df['tr2'] = (df['high'] - df['prev_close']).abs()
        df['tr3'] = (df['low'] - df['prev_close']).abs()
        df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
        df['ATR'] = df['tr'].rolling(window=14).mean()

        delta = df['close'].diff()
        gain = delta.clip(lower=0)
        loss = -1 * delta.clip(upper=0)
        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()
        rs = avg_gain / avg_loss
        df['RSI'] = 100 - (100 / (1 + rs))

        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp1 - exp2
        df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        df['MACD_Hist'] = df['MACD'] - df['Signal']

        df['SMA_20'] = df['close'].rolling(window=20).mean()
        df['STD_20'] = df['close'].rolling(window=20).std()
        df['Upper_BB'] = df['SMA_20'] + (df['STD_20'] * 2)
        df['Lower_BB'] = df['SMA_20'] - (df['STD_20'] * 2)
        
        df['EMA_50'] = df['close'].ewm(span=50, adjust=False).mean()
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
            except Exception as e:
                log_api_failure("check_htf_trend (Pandas Error)", str(e))
        return True 

    def _run_loop(self):
        last_candle_block = 0
        while self.is_running:
            try:
                self._fetch_market_state()
                self._monitor_live_exits()
                
                interval_sec = self.get_interval_seconds()
                now = time.time()
                current_block = int(now) // interval_sec
                next_close = (current_block + 1) * interval_sec
                
                self.next_scan_epoch = next_close + 1.5
                
                if current_block > last_candle_block:
                    time.sleep(1.5) 
                    self._scan_for_entries()
                    last_candle_block = current_block

            except Exception as e:
                self.log_trade("ERROR", "SYSTEM", "0", "₹0.00", f"Engine Crash: {str(e)}", "Failed")
            
            for _ in range(10):
                if not self.is_running: break
                time.sleep(1)

    def _fetch_market_state(self):
        balance_body = {"timestamp": int(round(time.time() * 1000))}
        balances_data = coindcx_auth_post("/exchange/v1/users/balances", balance_body)
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

        try:
            markets_res = requests.get("https://api.coindcx.com/exchange/v1/markets_details", timeout=10)
            markets_res.raise_for_status()
            markets = markets_res.json()
            
            self.market_precision = {m.get('coindcx_name'): int(m.get('target_currency_precision', 5)) for m in markets if m.get('coindcx_name')}
            self.market_min_qty = {m.get('coindcx_name'): float(m.get('min_quantity', 0.0001)) for m in markets if m.get('coindcx_name')}
            self.market_pair_string = {m.get('coindcx_name'): m.get('pair') for m in markets if m.get('coindcx_name') and m.get('pair')}
            
            tickers_res = requests.get("https://api.coindcx.com/exchange/ticker", timeout=10)
            tickers_res.raise_for_status()
            tickers = tickers_res.json()
            self.last_prices = {t['market']: float(t['last_price']) for t in tickers if 'market' in t}
            
            if self.auto_top_n > 0 and len(self.candidates) == 0:
                inr_markets = []
                for t in tickers:
                    market = t.get('market', '')
                    if market.endswith('INR'):
                        base_coin = market[:-3]
                        if base_coin not in ['USDT', 'USDC', 'FDUSD', 'TUSD', 'DAI']:
                            try:
                                vol = float(t.get('volume', 0))
                                price = float(t.get('last_price', 0))
                                inr_markets.append((base_coin, vol * price))
                            except Exception: pass
                inr_markets.sort(key=lambda x: x[1], reverse=True)
                self.candidates = [m[0] for m in inr_markets[:self.auto_top_n]]
                self.log_trade("SYSTEM", "AUTO-SCAN", str(self.auto_top_n), "₹0.00", f"Auto-selected top {self.auto_top_n} volatile coins by 24h volume.", "Info")
        except Exception as e:
            log_api_failure("markets_details or ticker", str(e))
            return

        for coin in self.candidates:
            if coin in actual_balances and coin not in self.active_positions:
                market = f"{coin}INR"
                curr_price = self.last_prices.get(market)
                if curr_price:
                    value = actual_balances[coin] * curr_price
                    if value > 50:
                        invested_with_fees = value * self.buy_fee_multiplier
                        self.active_positions[coin] = {
                            "qty": actual_balances[coin],
                            "entry_price": curr_price,
                            "invested": invested_with_fees,
                            "tp_price": curr_price * 1.05, 
                            "sl_price": curr_price * 0.95,
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

    def _monitor_live_exits(self):
        for coin in list(self.active_positions.keys()):
            market = f"{coin}INR"
            curr_price = self.last_prices.get(market)
            if not curr_price: continue
            
            pos = self.active_positions[coin]
            
            net_received = (pos['qty'] * curr_price) * self.sell_fee_multiplier
            net_profit = net_received - pos['invested']
            net_pnl_pct = (net_profit / pos['invested']) * 100
            
            is_tp = curr_price >= pos.get('tp_price', float('inf'))
            is_sl = curr_price <= pos.get('sl_price', 0.0)
            
            if is_tp:
                self._execute_sell(coin, "TAKE PROFIT", f"ATR Target Hit (+{net_pnl_pct:.2f}% Net).")
            elif is_sl:
                self._execute_sell(coin, "STOP LOSS", f"ATR Stop Hit ({net_pnl_pct:.2f}% Net).")

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
            res = coindcx_auth_post("/exchange/v1/orders/create", order_body)
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
        # 1. Evaluate Active Positions (Early Exit Check)
        for coin in list(self.active_positions.keys()):
            market = f"{coin}INR"
            curr_price = self.last_prices.get(market)
            if not curr_price: continue
            
            pos = self.active_positions[coin]
            breakeven_price = pos['entry_price'] * (self.buy_fee_multiplier / self.sell_fee_multiplier)
            
            candles_15m = self.fetch_candle_data(coin, interval="15m")
            time.sleep(0.4) 
            
            if candles_15m:
                try:
                    df = self.process_df_indicators(candles_15m)
                    latest_rsi = df['RSI'].iloc[-1]
                    
                    if latest_rsi > 58 or curr_price >= df['Upper_BB'].iloc[-1]:
                        df['time'] = pd.to_datetime(df['time'], unit='ms')
                        df_str = df[['time', 'open', 'high', 'low', 'close', 'RSI', 'MACD_Hist', 'Upper_BB']].tail(8).to_string(index=False)
                        
                        client = Groq(api_key=GROQ_API_KEY)
                        sell_prompt = """You are an elite quantitative crypto hedge fund manager. Analyze the raw candlestick data. Determine if the coin has reached a local peak or momentum is exhausting. Output 'SELL' if price is overextended near/above Upper_BB and candle wicks/MACD show slowing buyer momentum. Output 'HOLD' if the trend remains strong upwards. Respond ONLY in JSON: {"action": "SELL" or "HOLD", "reasoning": "1 short sentence"}"""
                        
                        response = client.chat.completions.create(
                            model="llama-3.3-70b-versatile", 
                            messages=[
                                {"role": "system", "content": sell_prompt},
                                {"role": "user", "content": f"Held asset {market} (15m candles):\n\n{df_str}"}
                            ],
                            response_format={"type": "json_object"}, temperature=0.1
                        )
                        decision_data = json.loads(response.choices[0].message.content)
                        if decision_data.get("action", "").upper() == "SELL":
                            if pos['entry_price'] < curr_price < breakeven_price:
                                self.log_trade("AI OVERRIDE", coin, f"{pos['qty']:.4f}", f"₹{(pos['qty']*curr_price):.2f}", f"AI Sell blocked: Gross profit is in the Dead Zone and cannot clear the Exchange Fee/TDS hurdle.", "Tax Shield")
                            else:
                                self._execute_sell(coin, "PRO AI SELL", decision_data.get("reasoning", "AI detected peak exhaustion."))
                        else:
                            # ✨ FIX: Log AI Hold reasoning when checking active positions
                            self.log_trade("AI HOLD (ACTIVE)", coin, f"{pos['qty']:.4f}", f"₹{(pos['qty']*curr_price):.2f}", decision_data.get("reasoning", "AI decided to ride the trend."), "Trailing Peak")
                except Exception as e:
                    log_api_failure("groq_chat_completion_sell", str(e))

        # 2. Scan for New Entries
        current_invested = sum(p['invested'] for p in self.active_positions.values())
        if (current_invested + self.trade_amount) <= self.max_budget:
            best_candidate_coin = None
            best_candidate_market = None
            best_candidate_price = 0
            best_candidate_1h_str = ""
            best_candidate_15m_str = ""
            best_setup_score = -999.0
            best_atr = 0.0

            for coin in self.candidates:
                if coin in self.active_positions: continue
                
                market = f"{coin}INR"
                curr_price = self.last_prices.get(market)
                if not curr_price: continue
                
                if not self.check_htf_trend(coin): continue
                
                candles_1h = self.fetch_candle_data(coin, interval="1h", limit=60)
                time.sleep(0.4)
                candles_15m = self.fetch_candle_data(coin, interval="15m", limit=40)
                time.sleep(0.4)
                
                if candles_1h and candles_15m:
                    try:
                        df_1h = self.process_df_indicators(candles_1h)
                        df_1h['time'] = pd.to_datetime(df_1h['time'], unit='ms')
                        df_1h_str = df_1h[['time', 'open', 'high', 'low', 'close', 'RSI', 'EMA_50', 'MACD_Hist']].tail(6).to_string(index=False)
                        
                        df_15m = self.process_df_indicators(candles_15m)
                        df_15m['time'] = pd.to_datetime(df_15m['time'], unit='ms')
                        df_15m_str = df_15m[['time', 'open', 'high', 'low', 'close', 'RSI', 'MACD_Hist', 'Lower_BB', 'ATR']].tail(8).to_string(index=False)
                        
                        latest_rsi_15m = df_15m['RSI'].iloc[-1]
                        latest_close_15m = df_15m['close'].iloc[-1]
                        lower_bb_15m = df_15m['Lower_BB'].iloc[-1]
                        latest_atr_15m = df_15m['ATR'].iloc[-1]
                        
                        bb_distance_pct = ((latest_close_15m - lower_bb_15m) / lower_bb_15m) * 100
                        setup_score = (100.0 - latest_rsi_15m) - (bb_distance_pct * 2)

                        if setup_score > best_setup_score:
                            best_setup_score = setup_score
                            best_candidate_coin = coin
                            best_candidate_market = market
                            best_candidate_price = curr_price
                            best_atr = latest_atr_15m
                            best_candidate_1h_str = df_1h_str
                            best_candidate_15m_str = df_15m_str
                    except Exception as e:
                        log_api_failure(f"MTF calculation ({coin})", str(e))
                        continue

            if best_candidate_coin and best_candidate_1h_str and best_candidate_15m_str:
                client = Groq(api_key=GROQ_API_KEY)
                system_prompt = """You are an elite quantitative crypto hedge fund manager specializing in Multi-Timeframe (MTF) market structure analysis. Your job is to analyze BOTH the 1-Hour (Macro Trend) and 15-Minute (Trigger Entry) charts before issuing a decision.
                Evaluation Rules:
                1. 1-Hour Macro Filter: Ensure the 1-Hour price is holding key support or aligned with positive MACD/EMA momentum. Reject if 1-Hour is breaking down hard.
                2. 15-Minute Trigger: Look for bullish bounce setups (low rejection wicks, holding near/above Lower Bollinger Band, RSI oversold recovery).
                3. Confluence Requirement: Only output 'BUY' if BOTH timeframes agree.
                Respond ONLY in JSON format: {"action": "BUY" or "HOLD", "reasoning": "1 short concise sentence explaining the MTF alignment"}"""
                
                mtf_user_prompt = f"Analyze Multi-Timeframe data for candidate {best_candidate_market}:\n=== 1-HOUR CHART ===\n{best_candidate_1h_str}\n=== 15-MINUTE CHART ===\n{best_candidate_15m_str}"
                
                try:
                    response = client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": mtf_user_prompt}
                        ],
                        response_format={"type": "json_object"}, temperature=0.1
                    )
                    decision_data = json.loads(response.choices[0].message.content)
                    action = decision_data.get("action", "HOLD").upper()
                    reasoning = decision_data.get("reasoning", "AI evaluated MTF market structure.")

                    if action == "BUY":
                        precision = self.market_precision.get(best_candidate_market, 5)
                        multiplier = 10 ** precision
                        raw_crypto = self.trade_amount / best_candidate_price
                        
                        buy_crypto_amount = math.ceil(raw_crypto * multiplier) / multiplier
                        min_required_qty = self.market_min_qty.get(best_candidate_market, 0.0001)
                        if buy_crypto_amount < min_required_qty: buy_crypto_amount = min_required_qty
                            
                        actual_cost = buy_crypto_amount * best_candidate_price
                        if actual_cost < 105.0:
                            required_for_105 = 105.0 / best_candidate_price
                            buy_crypto_amount = math.ceil(required_for_105 * multiplier) / multiplier
                            actual_cost = buy_crypto_amount * best_candidate_price
                            
                        total_invested_with_fees = actual_cost * self.buy_fee_multiplier

                        if total_invested_with_fees > (self.trade_amount + 15.0):
                            self.log_trade("BUY SKIPPED", best_candidate_coin, "0", f"₹{total_invested_with_fees:.2f}", f"Exchange min qty requires ₹{total_invested_with_fees:.2f}, exceeding your limit.", "Limit Enforced")
                        elif total_invested_with_fees > self.inr_balance:
                            self.log_trade("BUY SKIPPED", best_candidate_coin, "0", f"₹{total_invested_with_fees:.2f}", "Insufficient INR Balance.", "Failed")
                        else:
                            formatted_qty = f"{buy_crypto_amount:.{precision}f}"
                            order_body = {
                                "side": "buy", "order_type": "limit_order", "market": best_candidate_market,
                                "price_per_unit": float(best_candidate_price), "total_quantity": float(formatted_qty), 
                                "timestamp": int(round(time.time() * 1000))
                            }
                            res = coindcx_auth_post("/exchange/v1/orders/create", order_body)
                            if "orders" in res or "id" in res:
                                breakeven_price = best_candidate_price * (self.buy_fee_multiplier / self.sell_fee_multiplier)
                                min_net_profit_tp = breakeven_price * 1.005 
                                
                                target_tp = best_candidate_price + (3.0 * best_atr)
                                if target_tp < min_net_profit_tp:
                                    target_tp = min_net_profit_tp
                                    
                                target_sl = best_candidate_price - (1.5 * best_atr)
                                
                                self.active_positions[best_candidate_coin] = {
                                    "qty": buy_crypto_amount, "entry_price": best_candidate_price,
                                    "invested": total_invested_with_fees, "tp_price": target_tp, "sl_price": target_sl,
                                    "buy_time": time.time()
                                }
                                self.log_trade("BUY", best_candidate_coin, formatted_qty, f"₹{total_invested_with_fees:.2f}", reasoning, "Success")
                            else:
                                err = res.get("error", "API Error")
                                self.log_trade("BUY FAILED", best_candidate_coin, formatted_qty, f"₹{actual_cost:.2f}", f"Rejected: {err}", "Error")
                    else:
                        # ✨ FIX: Log AI Hold reasoning when checking new entry candidates
                        self.log_trade("AI HOLD (ENTRY)", best_candidate_coin, "0", f"₹{best_candidate_price:.2f}", reasoning, "MTF Scan Hold")
                except Exception as e:
                    log_api_failure("groq_chat_completion_buy", str(e))
                    self.log_trade("AI ERROR", best_candidate_coin, "0", "₹0.00", f"AI Decision Error: {str(e)}", "Bypassed")

# Cache Buster v56
@st.cache_resource
def get_bot_engine_v56():
    return GlobalBotEngine()

bot = get_bot_engine_v56()

# ==========================================
# 4. STREAMLIT UI CONFIG & STYLING
# ==========================================
st.set_page_config(page_title="AI Portfolio Manager", page_icon="🏦", layout="wide")

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
st.sidebar.markdown("## 🏦 **AI Portfolio**")
page = st.sidebar.radio("Navigation", ["📊 Live Dashboard", "⚙️ Bot Engine & Settings"])

st.sidebar.markdown("---")
st.sidebar.markdown("#### System Health")

if bot.is_running:
    st.sidebar.markdown("""<div style="background: #e6f4ea; border: 1px solid #ceead6; border-radius: 8px; padding: 12px; color: #137333; font-weight: 600; font-size: 0.9rem; text-align: center;">🟢 AUTOPILOT ACTIVE</div>""", unsafe_allow_html=True)
else:
    st.sidebar.markdown("""<div style="background: #fce8e6; border: 1px solid #fad2cf; border-radius: 8px; padding: 12px; color: #c5221f; font-weight: 600; font-size: 0.9rem; text-align: center;">🔴 AUTOPILOT STOPPED</div>""", unsafe_allow_html=True)

st.sidebar.markdown("<br>", unsafe_allow_html=True)
st.sidebar.caption("Version 56.0 (Hit Trigger Logging Added)")

# ==========================================
# 6. ROUTED PAGE VIEWS
# ==========================================

if page == "⚙️ Bot Engine & Settings":
    st.title("⚙️ Engine Settings & Live Logs")
    st.markdown("Configure your quantitative trading parameters and monitor real-time AI decisions.")
    
    st.markdown('<div class="settings-box">', unsafe_allow_html=True)
    st.markdown("#### 🎯 Trade Strategy")
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

    st.markdown("<br>#### 💰 Capital & Tax Shield Settings", unsafe_allow_html=True)
    colA, colB, colC = st.columns(3)
    with colA:
        max_bud = st.number_input("Max Portfolio Allocation (INR)", min_value=200.0, value=float(bot.max_budget), step=100.0)
    with colB:
        trade_amt = st.number_input("Position Size Per Asset (INR)", min_value=120.0, value=float(bot.trade_amount), step=10.0)
    with colC:
        exchange_fee_pct = st.number_input("Exchange Fee % (e.g. 0.5)", min_value=0.0, value=float(bot.exchange_fee_pct), step=0.1)
        tds_pct = st.number_input("Govt TDS % (e.g. 1.0)", min_value=0.0, value=float(bot.tds_pct), step=0.1)
        
    st.info(f"🛡️ **Tax Shield Active:** The bot automatically factors in {exchange_fee_pct}% Maker/Taker fees + 18% GST + {tds_pct}% TDS. It calculates absolute exact net cash-in-hand and explicitly prevents taking profits if the margin lands in the Tax Dead Zone.", icon="🇮🇳")
    st.markdown('</div>', unsafe_allow_html=True)

    ctrl_col1, ctrl_col2, ctrl_col3 = st.columns(3)
    with ctrl_col1:
        if st.button("▶️ Start Engine", type="primary", use_container_width=True, disabled=bot.is_running):
            bot.start(candidate_input, top_n, max_bud, trade_amt, exchange_fee_pct, tds_pct, candle_interval)
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
            </style>
            </head>
            <body>
                <div class="info-box">
                    <span><strong>⚡ System Active:</strong> High-Frequency Exit Monitor LIVE. {asset_str} <span id="live-timer"></span></span>
                </div>
                <script>
                    var targetEpoch = {bot.next_scan_epoch * 1000};
                    function updateTimer() {{
                        var now = new Date().getTime();
                        var distance = targetEpoch - now;
                        var el = document.getElementById('live-timer');
                        if (distance <= 0) {{
                            el.innerHTML = " | ⏳ Aggregating Data...";
                        }} else {{
                            var m = Math.floor(distance / 60000);
                            var s = Math.floor((distance % 60000) / 1000);
                            var mStr = m < 10 ? "0" + m : m;
                            var sStr = s < 10 ? "0" + s : s;
                            el.innerHTML = " | ⏳ Next AI Entry Scan in: " + mStr + ":" + sStr;
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
            st.warning("⚠️ **System Idle:** Awaiting execution. Click 'Start Engine' to commence trading.")
            
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
        try:
            balance_body = {"timestamp": int(round(time.time() * 1000))}
            balances_data = coindcx_auth_post("/exchange/v1/users/balances", balance_body)
            if isinstance(balances_data, list):
                for b in balances_data:
                    if b.get('currency') == 'INR':
                        live_inr = float(b.get('balance', 0))
                        
            tickers = requests.get("https://api.coindcx.com/exchange/ticker", timeout=10).json()
            if isinstance(tickers, list):
                for t in tickers:
                    if 'market' in t: live_prices[t['market']] = float(t['last_price'])
        except Exception as e:
            log_api_failure("Dashboard Balance/Ticker Fetch", str(e))
        
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
            st.info("No active positions held. The bot will scan for precision setups when the current candle closes.")
        else:
            pos_data = []
            for coin, pos in bot.active_positions.items():
                curr_price = live_prices.get(f"{coin}INR", pos['entry_price'])
                
                net_live_value = (pos['qty'] * curr_price) * bot.sell_fee_multiplier
                net_pnl = net_live_value - pos['invested']
                net_pnl_pct = (net_pnl / pos['invested']) * 100
                
                pos_data.append({
                    "Asset": coin,
                    "Current Price": f"₹{curr_price:.2f}",
                    "Avg Entry": f"₹{pos['entry_price']:.2f}",
                    "Actual Cost": f"₹{pos['invested']:.2f}",
                    "Net Return %": f"{net_pnl_pct:.2f}%",
                    "Take Profit Target": f"₹{pos.get('tp_price', 0):.2f}",
                    "Stop Loss Limit": f"₹{pos.get('sl_price', 0):.2f}"
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
