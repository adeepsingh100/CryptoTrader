import streamlit as st
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
    return requests.post(url, data=json_body, headers=headers).json()

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
                headers = ["Date & Time (IST)", "coin", "entry_price", "exit_price", "invested", "pnl", "pnl_pct", "type"]
                self.trades_sheet.append_row(headers)

            try:
                self.logs_sheet = self.spreadsheet.worksheet("ExecutionLogs")
            except Exception:
                self.logs_sheet = self.spreadsheet.add_worksheet("ExecutionLogs", rows=2000, cols=7)
                headers = ["Time (IST)", "Action", "Coin", "Quantity", "Value (INR)", "Reasoning", "Status"]
                self.logs_sheet.append_row(headers)

            self.last_error = None
            return True
        except Exception as e:
            self.last_error = str(e)
            return False

    def load_trades(self):
        """Loads historical trades and handles both raw float timestamps and new IST strings."""
        if not self.trades_sheet and not self.connect():
            return []
        try:
            records = self.trades_sheet.get_all_records()
            parsed_records = []
            for row in records:
                raw_ts = row.get("timestamp", row.get("Date & Time (IST)", 0))
                
                # Check if it's a readable date string or an old raw timestamp
                try:
                    ts = float(raw_ts)
                except ValueError:
                    # Parse the IST string back into a computer timestamp
                    try:
                        dt_obj = datetime.strptime(str(raw_ts), "%Y-%m-%d %I:%M:%S %p")
                        ist_offset = timedelta(hours=5, minutes=30)
                        utc_dt = dt_obj - ist_offset
                        ts = utc_dt.replace(tzinfo=timezone.utc).timestamp()
                    except Exception:
                        ts = time.time() # Failsafe
                        
                parsed_records.append({
                    "timestamp": ts,
                    "coin": str(row.get("coin", "")),
                    "entry_price": float(row.get("entry_price", 0)),
                    "exit_price": float(row.get("exit_price", 0)),
                    "invested": float(row.get("invested", 0)),
                    "pnl": float(row.get("pnl", 0)),
                    "pnl_pct": float(row.get("pnl_pct", 0)),
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
            # Convert timestamp to human-readable IST for Sheet1
            ist_offset = timedelta(hours=5, minutes=30)
            utc_dt = datetime.fromtimestamp(trade_record["timestamp"], tz=timezone.utc)
            ist_time = (utc_dt + ist_offset).strftime("%Y-%m-%d %I:%M:%S %p")
            
            row = [
                ist_time,
                trade_record["coin"],
                trade_record["entry_price"],
                trade_record["exit_price"],
                trade_record["invested"],
                trade_record["pnl"],
                trade_record["pnl_pct"],
                trade_record.get("type", "CLOSE")
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
                ist_time,
                log_entry["Action"],
                log_entry["Coin"],
                str(log_entry["Quantity"]),
                str(log_entry["Value (INR)"]),
                log_entry["Reasoning"],
                log_entry["Status"]
            ]
            self.logs_sheet.append_row(row)
        except Exception as e:
            self.last_error = f"Append Log Error: {e}"

gs_manager = GoogleSheetsManager()

# ==========================================
# 3. AUTONOMOUS PRO-TRADER PORTFOLIO ENGINE
# ==========================================
class GlobalBotEngine:
    _active_instances = []

    def __init__(self):
        # Terminate older duplicate background threads
        for old_bot in GlobalBotEngine._active_instances:
            old_bot.is_running = False
        GlobalBotEngine._active_instances.clear()
        GlobalBotEngine._active_instances.append(self)

        self.is_running = False
        self.trade_log = []
        self.thread = None
        
        self.candidates = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
        self.max_budget = 500.0
        self.trade_amount = 110.0
        self.tp_pct = 2.0
        self.sl_pct = 3.0
        self.check_interval = 10
        self.candle_interval = "1h"
        
        self.inr_balance = 0.0
        self.active_positions = {}
        self.last_prices = {}
        
        self.completed_trades = gs_manager.load_trades()
        self.realized_pnl = sum(t.get("pnl", 0.0) for t in self.completed_trades)
        self.cooldown_counter = 0

    def log_trade(self, action, coin, qty, value, reason, status):
        log_entry = {
            "timestamp": time.time(),
            "Action": action,
            "Coin": coin,
            "Quantity": qty,
            "Value (INR)": value,
            "Reasoning": reason,
            "Status": status
        }
        
        self.trade_log.insert(0, log_entry)
        if len(self.trade_log) > 100:
            self.trade_log.pop()
            
        gs_manager.append_log(log_entry)

    def start(self, candidates, max_budget, trade_amount, tp_pct, sl_pct, check_interval, candle_interval):
        if not self.is_running:
            self.candidates = [c.strip().upper() for c in candidates.split(",")]
            self.max_budget = max_budget
            self.trade_amount = trade_amount
            self.tp_pct = tp_pct
            self.sl_pct = sl_pct
            self.check_interval = check_interval
            self.candle_interval = candle_interval
            
            self.cooldown_counter = 0
            self.is_running = True
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()

    def stop(self):
        self.is_running = False

    def _run_loop(self):
        while self.is_running:
            try:
                self._execute_cycle()
            except Exception as e:
                self.log_trade("ERROR", "SYSTEM", "0", "₹0.00", f"Cycle Crash: {str(e)}", "Failed")
            
            sleep_seconds = int(self.check_interval * 60)
            for _ in range(sleep_seconds):
                if not self.is_running:
                    break
                time.sleep(1)

    def _execute_cycle(self):
        # 1. Fetch Balances
        balance_body = {"timestamp": int(round(time.time() * 1000))}
        balances_data = coindcx_auth_post("/exchange/v1/users/balances", balance_body)
        
        actual_balances = {}
        if isinstance(balances_data, list):
            for b in balances_data:
                bal = float(b.get('balance', 0))
                if bal > 0:
                    actual_balances[b['currency']] = bal
                    if b['currency'] == 'INR':
                        self.inr_balance = bal

        # 2. Market Tickers
        markets = requests.get("https://api.coindcx.com/exchange/v1/markets_details").json()
        market_precision = {m['symbol']: int(m.get('target_currency_precision', 5)) for m in markets if 'symbol' in m}
        
        tickers = requests.get("https://api.coindcx.com/exchange/ticker").json()
        self.last_prices = {t['market']: float(t['last_price']) for t in tickers if 'market' in t}

        # 3. Wallet Sync Recovery & Cleanup
        for coin in self.candidates:
            if coin in actual_balances and coin not in self.active_positions:
                market = f"{coin}INR"
                curr_price = self.last_prices.get(market)
                if curr_price:
                    value = actual_balances[coin] * curr_price
                    if value > 50:
                        self.active_positions[coin] = {
                            "qty": actual_balances[coin],
                            "entry_price": curr_price,
                            "invested": value
                        }
                        self.log_trade("SYNC BUY", coin, f"{actual_balances[coin]:.4f}", f"₹{value:.2f}", "Detected coin in wallet from external trade.", "Wallet Sync")

        for coin in list(self.active_positions.keys()):
            if coin not in actual_balances or (actual_balances[coin] * self.last_prices.get(f"{coin}INR", 0) < 50):
                self.log_trade("SYNC SELL", coin, "0", "₹0.00", "Coin missing from wallet. Assuming external/manual sell.", "Wallet Sync")
                del self.active_positions[coin]

        # =========================================================================
        # 4. THE GUARDIAN AI: Manages ACTIVE positions to SELL at peak profit
        # =========================================================================
        for coin in list(self.active_positions.keys()):
            market = f"{coin}INR"
            curr_price = self.last_prices.get(market)
            if not curr_price: continue
            
            pos = self.active_positions[coin]
            pnl_pct = ((curr_price - pos['entry_price']) / pos['entry_price']) * 100
            curr_val = pos['qty'] * curr_price
            
            candle_pair = f"I-{coin}_INR"
            ai_sell_signal = False
            ai_reason = ""
            
            try:
                url = f"https://public.coindcx.com/market_data/candles?pair={candle_pair}&interval={self.candle_interval}&limit=40"
                res = requests.get(url, timeout=8)
                candles_data = res.json()
                if isinstance(candles_data, list) and len(candles_data) >= 20:
                    df = pd.DataFrame(candles_data)
                    df['close'] = df['close'].astype(float)
                    df['high'] = df['high'].astype(float)
                    df['low'] = df['low'].astype(float)
                    df = df.sort_values(by='time', ascending=True)
                    
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
                    
                    latest_rsi = df['RSI'].iloc[-1]
                    
                    if latest_rsi > 58 or curr_price >= df['Upper_BB'].iloc[-1]:
                        df['time'] = pd.to_datetime(df['time'], unit='ms')
                        df_str = df[['time', 'open', 'high', 'low', 'close', 'RSI', 'MACD_Hist', 'Upper_BB']].tail(8).to_string(index=False)
                        
                        client = Groq(api_key=GROQ_API_KEY)
                        sell_prompt = """You are an elite quantitative crypto hedge fund manager.
                        Analyze the raw candlestick data (OHLC), RSI, MACD Histogram, and Upper Bollinger Band.
                        Determine if the coin has reached a local peak or momentum is exhausting.
                        - Output 'SELL' if price is overextended near/above Upper_BB and candle wicks/MACD show slowing buyer momentum.
                        - Output 'HOLD' if the trend remains strong upwards.
                        Respond ONLY in JSON: {"action": "SELL" or "HOLD", "reasoning": "1 short concise sentence"}"""
                        
                        response = client.chat.completions.create(
                            model="openai/gpt-oss-120b",
                            messages=[
                                {"role": "system", "content": sell_prompt},
                                {"role": "user", "content": f"Held asset {market} ({self.candle_interval} candles):\n\n{df_str}"}
                            ],
                            response_format={"type": "json_object"},
                            temperature=0.1
                        )
                        decision_data = json.loads(response.choices[0].message.content)
                        if decision_data.get("action", "").upper() == "SELL":
                            ai_sell_signal = True
                            ai_reason = decision_data.get("reasoning", "AI detected peak exhaustion.")
                        else:
                            self.log_trade("AI HOLD", coin, f"{pos['qty']:.4f}", f"₹{curr_val:.2f}", decision_data.get("reasoning", "AI riding trend."), "Trailing Peak")
            except Exception:
                pass

            is_tp = pnl_pct >= self.tp_pct
            is_sl = pnl_pct <= -self.sl_pct
            
            if is_tp or is_sl or ai_sell_signal:
                if is_tp:
                    action_type, reasoning = "TAKE PROFIT", f"Hard Take-Profit hit (+{pnl_pct:.2f}%)."
                elif is_sl:
                    action_type, reasoning = "STOP LOSS", f"Hard Stop-Loss hit ({pnl_pct:.2f}%)."
                else:
                    action_type, reasoning = "PRO AI SELL", ai_reason
                
                precision = market_precision.get(market, 5)
                multiplier = 10 ** precision
                sell_qty = math.floor(actual_balances.get(coin, 0) * multiplier) / multiplier
                actual_value = sell_qty * curr_price
                
                if actual_value >= 100.0:
                    formatted_qty = f"{sell_qty:.{precision}f}"
                    order_body = {
                        "side": "sell",
                        "order_type": "market_order",
                        "market": market,
                        "total_quantity": formatted_qty,
                        "timestamp": int(round(time.time() * 1000))
                    }
                    res = coindcx_auth_post("/exchange/v1/orders/create", order_body)
                    if "orders" in res or "id" in res:
                        profit_inr = actual_value - pos['invested']
                        self.realized_pnl += profit_inr
                        
                        trade_record = {
                            "timestamp": time.time(),
                            "coin": coin,
                            "entry_price": pos['entry_price'],
                            "exit_price": curr_price,
                            "invested": pos['invested'],
                            "pnl": profit_inr,
                            "pnl_pct": pnl_pct,
                            "type": action_type
                        }
                        self.completed_trades.append(trade_record)
                        gs_manager.append_trade(trade_record)
                        
                        self.log_trade("SELL", coin, formatted_qty, f"₹{actual_value:.2f}", reasoning, "Success")
                        del self.active_positions[coin]
                        self.cooldown_counter = 2
                        return
                    else:
                        err = res.get("message", "API Error")
                        self.log_trade("SELL FAILED", coin, formatted_qty, f"₹{actual_value:.2f}", f"Rejected: {err}", "Error")

        if self.cooldown_counter > 0:
            self.cooldown_counter -= 1
            return

        # =========================================================================
        # 5. THE SCANNER AI: Hunts for new BUY opportunities
        # =========================================================================
        current_invested = sum(p['invested'] for p in self.active_positions.values())
        
        if (current_invested + self.trade_amount) <= self.max_budget:
            best_candidate_coin = None
            best_candidate_market = None
            best_candidate_price = 0
            best_candidate_df_str = ""
            best_setup_score = -999.0
            scan_success_count = 0

            for coin in self.candidates:
                if coin in self.active_positions: continue
                
                market = f"{coin}INR"
                curr_price = self.last_prices.get(market)
                if not curr_price: continue
                
                candle_pair = f"I-{coin}_INR"
                try:
                    url = f"https://public.coindcx.com/market_data/candles?pair={candle_pair}&interval={self.candle_interval}&limit=40"
                    res = requests.get(url, timeout=8)
                    candles_data = res.json()
                    
                    if not isinstance(candles_data, list) or len(candles_data) < 20:
                        continue

                    df = pd.DataFrame(candles_data)
                    if not {'close', 'high', 'low', 'open', 'time'}.issubset(df.columns):
                        continue

                    df['open'] = df['open'].astype(float)
                    df['high'] = df['high'].astype(float)
                    df['low'] = df['low'].astype(float)
                    df['close'] = df['close'].astype(float)
                    df = df.sort_values(by='time', ascending=True)
                    
                    # Technical Indicators
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
                    df['Lower_BB'] = df['SMA_20'] - (df['STD_20'] * 2)
                    
                    latest_rsi = df['RSI'].iloc[-1]
                    latest_close = df['close'].iloc[-1]
                    lower_bb = df['Lower_BB'].iloc[-1]
                    
                    scan_success_count += 1
                    
                    # Score setups based on deep dips + momentum
                    bb_distance_pct = ((latest_close - lower_bb) / lower_bb) * 100
                    setup_score = (100.0 - latest_rsi) - (bb_distance_pct * 2)

                    if setup_score > best_setup_score:
                        best_setup_score = setup_score
                        best_candidate_coin = coin
                        best_candidate_market = market
                        best_candidate_price = curr_price
                        df['time'] = pd.to_datetime(df['time'], unit='ms')
                        best_candidate_df_str = df[['time', 'open', 'high', 'low', 'close', 'RSI', 'MACD_Hist', 'Lower_BB']].tail(8).to_string(index=False)
                except Exception:
                    continue

            # API Timeout Failure Check
            if scan_success_count == 0 and len([c for c in self.candidates if c not in self.active_positions]) > 0:
                self.log_trade("API RETRY", "MARKET", "0", "₹0.00", "CoinDCX candle endpoint unresponsive. Retrying next cycle.", "API Pause")
                return

            if best_candidate_coin and best_candidate_df_str:
                client = Groq(api_key=GROQ_API_KEY)
                system_prompt = """You are a ruthless quantitative crypto trader analyzing live candlestick data (OHLC), RSI, MACD Histogram, and Lower Bollinger Bands.
                Your task is to identify precision bounce setups and reject dangerous 'falling knives'.
                
                Evaluate the candlestick structure:
                1. Look for rejection wicks at the low prices or candles closing higher than opens (bullish reversal).
                2. Check if the MACD Histogram is curving upwards (loss of downward momentum).
                3. Check if price is holding or bouncing near/above Lower Bollinger Band support.
                
                Respond ONLY in JSON format:
                {"action": "BUY" or "HOLD", "reasoning": "1 short concise sentence explaining the price action"}"""
                
                try:
                    response = client.chat.completions.create(
                        model="openai/gpt-oss-120b",
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": f"Analyze candles for candidate {best_candidate_market} ({self.candle_interval} chart):\n\n{best_candidate_df_str}"}
                        ],
                        response_format={"type": "json_object"},
                        temperature=0.1
                    )
                    decision_data = json.loads(response.choices[0].message.content)
                    action = decision_data.get("action", "HOLD").upper()
                    reasoning = decision_data.get("reasoning", "AI evaluated market structure.")

                    if action == "BUY":
                        precision = market_precision.get(best_candidate_market, 5)
                        multiplier = 10 ** precision
                        raw_crypto = self.trade_amount / best_candidate_price
                        
                        if (raw_crypto * best_candidate_price) >= 101.0:
                            buy_crypto_amount = math.floor(raw_crypto * multiplier) / multiplier
                        else:
                            buy_crypto_amount = math.ceil(raw_crypto * multiplier) / multiplier
                            
                        actual_cost = buy_crypto_amount * best_candidate_price
                        
                        if actual_cost > self.inr_balance:
                            self.log_trade("BUY SKIPPED", best_candidate_coin, "0", f"₹{actual_cost:.2f}", "Insufficient INR Balance.", "Failed")
                        else:
                            formatted_qty = f"{buy_crypto_amount:.{precision}f}"
                            order_body = {
                                "side": "buy",
                                "order_type": "market_order",
                                "market": best_candidate_market,
                                "total_quantity": formatted_qty,
                                "timestamp": int(round(time.time() * 1000))
                            }
                            res = coindcx_auth_post("/exchange/v1/orders/create", order_body)
                            if "orders" in res or "id" in res:
                                self.active_positions[best_candidate_coin] = {
                                    "qty": buy_crypto_amount,
                                    "entry_price": best_candidate_price,
                                    "invested": actual_cost
                                }
                                self.log_trade("BUY", best_candidate_coin, formatted_qty, f"₹{actual_cost:.2f}", reasoning, "Success")
                                self.cooldown_counter = 2
                            else:
                                err = res.get("message", "API Error")
                                self.log_trade("BUY FAILED", best_candidate_coin, formatted_qty, f"₹{actual_cost:.2f}", f"Rejected: {err}", "Error")
                    else:
                        self.log_trade("AI HOLD", best_candidate_coin, "0", f"₹{best_candidate_price:.2f}", reasoning, "Candle Scan Hold")
                except Exception as e:
                    self.log_trade("AI ERROR", best_candidate_coin, "0", "₹0.00", f"AI Decision Error: {str(e)}", "Bypassed")
        else:
            self.log_trade("BUDGET LOCK", "PORTFOLIO", "ALL", f"₹{current_invested:.2f}", "Maximum portfolio budget reached. Awaiting sell event.", "Budget Full")

@st.cache_resource
def get_bot_engine():
    return GlobalBotEngine()

bot = get_bot_engine()

# ==========================================
# 4. STREAMLIT UI CONFIG & STYLING
# ==========================================
st.set_page_config(
    page_title="AI Portfolio Manager", 
    page_icon="🏦", 
    layout="wide"
)

def inject_custom_css():
    st.markdown("""
    <style>
        .stApp { background-color: #f8f9fa; color: #202124; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
        .block-container { padding-top: 2rem !important; }
        [data-testid="stSidebar"] { background-color: #ffffff; border-right: 1px solid #e0e0e0; }
        
        .metric-card {
            background: #ffffff;
            border: 1px solid #eaebed;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
            margin-bottom: 12px;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
        }
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
        
        @media (max-width: 768px) { .metric-value { font-size: 1.4rem; } .metric-card { padding: 16px; } }
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
st.sidebar.caption("Version 26.0 (Pro AI Guard) • GPT-120B")

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
        cand_input = st.text_input("Assets to Monitor (Comma separated)", value="BTC, ETH, SOL, XRP, DOGE")
    with col2:
        candle_interval = st.selectbox("Candle Timeframe (Chart Size)", ["15m", "30m", "1h", "4h", "1d"], index=2)
    
    st.markdown("<br>#### 💰 Capital & Risk Management", unsafe_allow_html=True)
    colA, colB = st.columns(2)
    with colA:
        max_bud = st.number_input("Max Portfolio Allocation (INR)", min_value=200.0, value=float(bot.max_budget), step=100.0)
        tp_pct = st.number_input("Take-Profit Target (%)", min_value=0.5, value=float(bot.tp_pct), step=0.5)
    with colB:
        trade_amt = st.number_input("Position Size Per Asset (INR)", min_value=105.0, value=float(bot.trade_amount), step=10.0)
        sl_pct = st.number_input("Stop-Loss Limit (%)", min_value=0.5, value=float(bot.sl_pct), step=0.5)
        
    interval_input = st.number_input("Scan Market Frequency (Minutes)", min_value=1, value=int(bot.check_interval))
    st.markdown('</div>', unsafe_allow_html=True)

    ctrl_col1, ctrl_col2, ctrl_col3 = st.columns(3)
    with ctrl_col1:
        if st.button("▶️ Start Engine", type="primary", use_container_width=True, disabled=bot.is_running):
            bot.start(cand_input, max_bud, trade_amt, tp_pct, sl_pct, interval_input, candle_interval)
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
    
    st.subheader("☁️ Cloud Storage Connection Status")
    is_connected = gs_manager.connect()
    
    if is_connected:
        st.success(f"✅ Connected to Google Sheet (`{SHEET_NAME}`)! Found **Sheet1** (Trades) & **ExecutionLogs** tabs.")
    else:
        st.error(f"❌ Google Sheets Connection Failed: **{gs_manager.last_error}**")

    st.markdown("---")

    @st.fragment(run_every="10s")
    def bot_logs_view():
        if bot.is_running:
            st.info(f"⚡ **System Active:** Scanning {len(bot.candidates)} assets on {bot.candle_interval} timeframe | Interval: {bot.check_interval}m")
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
                        
            tickers = requests.get("https://api.coindcx.com/exchange/ticker").json()
            if isinstance(tickers, list):
                for t in tickers:
                    if 'market' in t:
                        live_prices[t['market']] = float(t['last_price'])
        except Exception:
            pass 
        
        ist_offset = timedelta(hours=5, minutes=30)
        now_ist = datetime.now(timezone.utc) + ist_offset
        today_date_ist = now_ist.date()
        yesterday_date_ist = today_date_ist - timedelta(days=1)
        
        current_year = now_ist.year
        current_month = now_ist.month
        
        if current_month == 1:
            last_month_year = current_year - 1
            last_month_num = 12
        else:
            last_month_year = current_year
            last_month_num = current_month - 1

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
            pnl = trade.get("pnl", 0.0)

            if trade_dt_ist >= two_years_ago:
                if pnl >= 0: period_stats["all_time"]["profit"] += pnl
                else: period_stats["all_time"]["loss"] += abs(pnl)
                period_stats["all_time"]["count"] += 1

                if trade_date == today_date_ist:
                    if pnl >= 0: period_stats["today"]["profit"] += pnl
                    else: period_stats["today"]["loss"] += abs(pnl)
                    period_stats["today"]["count"] += 1

                if trade_date == yesterday_date_ist:
                    if pnl >= 0: period_stats["yesterday"]["profit"] += pnl
                    else: period_stats["yesterday"]["loss"] += abs(pnl)
                    period_stats["yesterday"]["count"] += 1

                if trade_dt_ist.year == current_year and trade_dt_ist.month == current_month:
                    if pnl >= 0: period_stats["this_month"]["profit"] += pnl
                    else: period_stats["this_month"]["loss"] += abs(pnl)
                    period_stats["this_month"]["count"] += 1

                if trade_dt_ist.year == last_month_year and trade_dt_ist.month == last_month_num:
                    if pnl >= 0: period_stats["last_month"]["profit"] += pnl
                    else: period_stats["last_month"]["loss"] += abs(pnl)
                    period_stats["last_month"]["count"] += 1

        current_invested = sum(p['invested'] for p in bot.active_positions.values())
        unrealized_pnl = 0.0
        for coin, pos in bot.active_positions.items():
            live_p = live_prices.get(f"{coin}INR", pos['entry_price'])
            unrealized_pnl += (pos['qty'] * live_p) - pos['invested']

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
                    <div class="metric-title">Budget Utilized</div>
                    <div class="metric-value">₹{current_invested:,.2f}</div>
                    <div class="metric-sub">Max Allocation Limit: ₹{bot.max_budget:,.2f}</div>
                    <div style="margin-top: 8px;"></div>
                </div>
            """, unsafe_allow_html=True)
            st.progress(budget_pct)
            
        with m_col3:
            pnl_color = "text-green" if unrealized_pnl >= 0 else "text-red"
            pnl_pct_str = f"{(unrealized_pnl/current_invested*100):.2f}%" if current_invested else "0.00%"
            st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-title">Floating P&L</div>
                    <div class="metric-value {pnl_color}">₹{unrealized_pnl:,.2f}</div>
                    <div class="metric-sub {pnl_color}">{pnl_pct_str} Total Unsold Return</div>
                </div>
            """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        st.subheader("📈 Performance History")
        
        tab_today, tab_yesterday, tab_this_m, tab_last_m, tab_all = st.tabs([
            "Today", "Yesterday", "This Month", "Last Month", "All-Time"
        ])

        def render_period_metrics(p_data):
            net = p_data["profit"] - p_data["loss"]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Gross Profit", f"₹{p_data['profit']:,.2f}")
            c2.metric("Gross Loss", f"₹{p_data['loss']:,.2f}")
            c3.metric("Net Return", f"₹{net:,.2f}", delta=f"₹{net:,.2f}")
            c4.metric("Trades Executed", f"{p_data['count']}")

        with tab_today: render_period_metrics(period_stats["today"])
        with tab_yesterday: render_period_metrics(period_stats["yesterday"])
        with tab_this_m: render_period_metrics(period_stats["this_month"])
        with tab_last_m: render_period_metrics(period_stats["last_month"])
        with tab_all: render_period_metrics(period_stats["all_time"])

        st.markdown("<br>", unsafe_allow_html=True)

        st.subheader("💼 Active Holdings")
        if not bot.active_positions:
            st.info("No active positions held. Bot is scanning candidates for precision bounce setups.")
        else:
            pos_data = []
            for coin, pos in bot.active_positions.items():
                curr_price = live_prices.get(f"{coin}INR", pos['entry_price'])
                curr_val = pos['qty'] * curr_price
                pnl = curr_val - pos['invested']
                pnl_pct = (pnl / pos['invested']) * 100
                
                pos_data.append({
                    "Asset": coin,
                    "Invested (INR)": f"₹{pos['invested']:.2f}",
                    "Current Value": f"₹{curr_val:.2f}",
                    "Avg Entry": f"₹{pos['entry_price']:.2f}",
                    "Current Price": f"₹{curr_price:.2f}",
                    "Return %": f"{pnl_pct:.2f}%",
                    "PnL (INR)": f"₹{pnl:.2f}"
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
                    "Return %": f"{trade.get('pnl_pct', 0.0):.2f}%"
                })
            st.dataframe(pd.DataFrame(history_rows), use_container_width=True, hide_index=True)

    portfolio_view()
