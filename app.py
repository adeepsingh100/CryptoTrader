import streamlit as st
import pandas as pd
import json
import time
import hmac
import hashlib
import requests
import math
import threading
from groq import Groq
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ==========================================
# 1. SECURE API KEYS (From Streamlit Secrets)
# ==========================================
try:
    API_KEY = st.secrets["COINDCX_API_KEY"]
    API_SECRET = st.secrets["COINDCX_API_SECRET"]
    GROQ_API_KEY = st.secrets["GROQ_API_KEY"]
except Exception:
    API_KEY = "YOUR_COINDCX_API_KEY_HERE"
    API_SECRET = "YOUR_COINDCX_API_SECRET_HERE"
    GROQ_API_KEY = "YOUR_GROQ_API_KEY_HERE"

# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================
def coindcx_auth_post(endpoint, body):
    """Signs and sends authenticated requests to CoinDCX."""
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
# 3. GLOBAL SHARED BOT ENGINE
# ==========================================
class GlobalBotEngine:
    def __init__(self):
        self.is_running = False
        self.trade_log = []
        self.thread = None
        self.coin = "BTC"
        self.trade_amount_inr = 150.0
        self.check_interval = 5
    
    def get_ist_time():
        """Gets exact Indian Standard Time regardless of server OS settings."""
        utc_now = datetime.now(timezone.utc)
        ist_now = utc_now + timedelta(hours=5, minutes=30)
        return ist_now.strftime("%I:%M:%S %p")
    
    def log_trade(self, action, crypto_amt, inr_budget, reason, status):
        # Force exact Indian Standard Time (Asia/Kolkata)
        ist_time = datetime.now(ZoneInfo("Asia/Kolkata"))
        
        log_entry = {
            "Time": get_ist_time(),
            "Action": action,
            "Quantity": crypto_amt,
            "Target Budget / Value": inr_budget,
            "Reasoning": reason,
            "Status": status
        }
        self.trade_log.insert(0, log_entry)

    def start(self, coin, trade_amount_inr, check_interval):
        if not self.is_running:
            self.coin = coin
            self.trade_amount_inr = trade_amount_inr
            self.check_interval = check_interval
            self.is_running = True
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()

    def stop(self):
        self.is_running = False

    def _run_loop(self):
        """Runs continuously in the background independent of UI sessions."""
        while self.is_running:
            try:
                self._execute_cycle()
            except Exception as e:
                self.log_trade("ERROR", "0", "₹0.00", str(e), "Background Crash")
            
            # Sleep in short increments so stopping is instant when triggered
            sleep_seconds = int(self.check_interval * 60)
            for _ in range(sleep_seconds):
                if not self.is_running:
                    break
                time.sleep(1)

    def _execute_cycle(self):
        market_symbol = f"{self.coin}INR"
        candle_pair = f"I-{self.coin}_INR"

        # 1. Fetch Balances
        balance_body = {"timestamp": int(round(time.time() * 1000))}
        balances_data = coindcx_auth_post("/exchange/v1/users/balances", balance_body)
        
        inr_balance = 0.0
        coin_balance = 0.0
        if isinstance(balances_data, list):
            for b in balances_data:
                curr = b.get('currency', '')
                bal = float(b.get('balance', 0))
                if curr == 'INR':
                    inr_balance = bal
                elif curr == self.coin:
                    coin_balance = bal

        # 2. Precision Rules
        markets_url = "https://api.coindcx.com/exchange/v1/markets_details"
        markets = requests.get(markets_url).json()
        market_info = next((m for m in markets if m.get('symbol') == market_symbol), None)
        precision = int(market_info.get('target_currency_precision', 5)) if market_info else 5
        multiplier = 10 ** precision

        # 3. Live Price
        tickers = requests.get("https://api.coindcx.com/exchange/ticker").json()
        ticker = next((t for t in tickers if t.get('market') == market_symbol), None)
        if not ticker:
            self.log_trade("ERROR", "0", f"₹{self.trade_amount_inr}", f"Market {market_symbol} not found.", "Failed")
            return
            
        current_price = float(ticker['last_price'])

        # 4. Fetch Chart Data for AI
        candles_url = f"https://public.coindcx.com/market_data/candles?pair={candle_pair}&interval=1h&limit=10"
        candles_data = requests.get(candles_url).json()
        
        df = pd.DataFrame(candles_data)
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        df = df.sort_values(by='time', ascending=True)
        market_data_str = df[['time', 'open', 'high', 'low', 'close', 'volume']].to_string(index=False)

        # 5. Ask Groq AI
        client = Groq(api_key=GROQ_API_KEY)
        system_prompt = """You are a highly analytical crypto trading bot. 
        You MUST respond ONLY with a valid JSON object containing:
        "action": strictly "BUY", "SELL", or "HOLD".
        "reasoning": a brief 1-sentence explanation based on the price data provided."""
        
        user_prompt = f"Analyze the last 10 hours of market data for {market_symbol}:\n\n{market_data_str}"
        
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        
        decision_data = json.loads(response.choices[0].message.content)
        action = decision_data.get("action", "HOLD").upper()
        reasoning = decision_data.get("reasoning", "No reason provided.")

        # 6. Execute Trade
        if action == "BUY":
            raw_crypto = self.trade_amount_inr / current_price
            qty_down = math.floor(raw_crypto * multiplier) / multiplier
            val_down = qty_down * current_price
            
            if val_down >= 101.0:
                buy_crypto_amount = qty_down
            else:
                buy_crypto_amount = math.ceil(raw_crypto * multiplier) / multiplier
                
            actual_cost = buy_crypto_amount * current_price
            
            if actual_cost > inr_balance:
                reason_msg = f"AI signaled BUY, but cost (₹{actual_cost:.2f}) exceeds INR balance (₹{inr_balance:.2f})."
                self.log_trade("BUY SKIPPED", "0", f"₹{actual_cost:.2f}", reason_msg, "Insufficient INR")
            else:
                formatted_qty = f"{buy_crypto_amount:.{precision}f}"
                order_body = {
                    "side": "buy",
                    "order_type": "market_order",
                    "market": market_symbol,
                    "total_quantity": formatted_qty,
                    "timestamp": int(round(time.time() * 1000))
                }
                order_response = coindcx_auth_post("/exchange/v1/orders/create", order_body)
                if "orders" in order_response or "id" in order_response:
                    self.log_trade("BUY", formatted_qty, f"₹{actual_cost:.2f}", reasoning, "Success")
                else:
                    err = order_response.get("message", "API Error")
                    self.log_trade("BUY FAILED", formatted_qty, f"₹{actual_cost:.2f}", reasoning, f"Rejected: {err}")

        elif action == "SELL":
            sell_crypto_amount = math.floor(coin_balance * multiplier) / multiplier
            actual_value = sell_crypto_amount * current_price
            
            if sell_crypto_amount <= 0:
                self.log_trade("SELL SKIPPED", "0", "₹0.00", f"AI signaled SELL, but you hold 0 {self.coin}.", "No Crypto Held")
            elif actual_value < 100.0:
                reason_msg = f"AI signaled SELL, but held {self.coin} ({sell_crypto_amount}) is worth ₹{actual_value:.2f}, below CoinDCX ₹100 limit."
                self.log_trade("SELL SKIPPED", f"{sell_crypto_amount:.{precision}f}", f"₹{actual_value:.2f}", reason_msg, "Below Min Order")
            else:
                formatted_qty = f"{sell_crypto_amount:.{precision}f}"
                order_body = {
                    "side": "sell",
                    "order_type": "market_order",
                    "market": market_symbol,
                    "total_quantity": formatted_qty,
                    "timestamp": int(round(time.time() * 1000))
                }
                order_response = coindcx_auth_post("/exchange/v1/orders/create", order_body)
                if "orders" in order_response or "id" in order_response:
                    self.log_trade("SELL", formatted_qty, f"₹{actual_value:.2f}", reasoning, "Success")
                else:
                    err = order_response.get("message", "API Error")
                    self.log_trade("SELL FAILED", formatted_qty, f"₹{actual_value:.2f}", reasoning, f"Rejected: {err}")
        else:
            self.log_trade("HOLD", "0", "₹0.00", reasoning, "No Action Taken")


# Cache instance globally across ALL web sessions/users
@st.cache_resource
def get_global_bot():
    return GlobalBotEngine()

bot = get_global_bot()

# ==========================================
# 4. STREAMLIT UI
# ==========================================
st.set_page_config(page_title="Global Live AI Bot", layout="wide")
st.title("🌐 Synchronized Live CoinDCX AI Bot")

# --- LIVE GLOBAL STATUS INDICATOR ---
if bot.is_running:
    st.success(f"🟢 **GLOBAL STATUS: RUNNING LIVE** (Target: **{bot.coin}**, Budget: **₹{bot.trade_amount_inr}**, Check Interval: **{bot.check_interval} min**)")
else:
    st.error("🔴 **GLOBAL STATUS: STOPPED**")

st.divider()

# --- CONTROLS ---
col1, col2, col3 = st.columns(3)
with col1:
    coin_input = st.text_input("Coin Symbol", value=bot.coin).upper()
with col2:
    budget_input = st.number_input("Target BUY Budget (INR)", min_value=10.0, value=float(bot.trade_amount_inr), step=10.0)
with col3:
    interval_input = st.number_input("Check Every (Minutes)", min_value=1, value=int(bot.check_interval))

ctrl_col1, ctrl_col2 = st.columns(2)
with ctrl_col1:
    if st.button("▶️ START BOT (Global)", type="primary", use_container_width=True, disabled=bot.is_running):
        bot.start(coin_input, budget_input, interval_input)
        st.rerun()

with ctrl_col2:
    if st.button("⏹️ STOP BOT (Global)", use_container_width=True, disabled=not bot.is_running):
        bot.stop()
        st.rerun()

st.divider()

# --- SHARED ACTIVITY LOG ---
st.subheader("📋 Live Activity Log (Shared Across All Tabs)")
if not bot.trade_log:
    st.info("No activity recorded yet.")
else:
    df_logs = pd.DataFrame(bot.trade_log)
    st.dataframe(df_logs, use_container_width=True, hide_index=True)

# --- AUTO REFRESH OPEN UI TABS EVERY 10 SECONDS ---
if bot.is_running:
    time.sleep(10)
    st.rerun()
    @st.cache_resource
def get_global_bot():
    return GlobalBotEngine()

bot = get_global_bot()

# ==========================================
# STREAMLIT UI & LIVE DISPLAY
# ==========================================
st.set_page_config(page_title="Global Live AI Bot", layout="wide")
st.title("🌐 Synchronized Live CoinDCX AI Bot")

col1, col2, col3 = st.columns(3)
with col1:
    coin_input = st.text_input("Coin Symbol", value=bot.coin).upper()
with col2:
    budget_input = st.number_input("Target BUY Budget (INR)", min_value=10.0, value=float(bot.trade_amount_inr), step=10.0)
with col3:
    interval_input = st.number_input("Check Every (Minutes)", min_value=1, value=int(bot.check_interval))

ctrl_col1, ctrl_col2, ctrl_col3 = st.columns(3)
with ctrl_col1:
    if st.button("▶️ START BOT", type="primary", use_container_width=True, disabled=bot.is_running):
        bot.start(coin_input, budget_input, interval_input)
        st.rerun()

with ctrl_col2:
    if st.button("⏹️ STOP BOT", use_container_width=True, disabled=not bot.is_running):
        bot.stop()
        st.rerun()

with ctrl_col3:
    if st.button("🧹 CLEAR LOGS", use_container_width=True):
        bot.trade_log.clear()
        st.rerun()

st.divider()

# --- ISOLATED AUTO-REFRESHING FRAGMENT ---
@st.fragment(run_every="10s")
def live_status_board():
    if bot.is_running:
        st.success(f"🟢 **GLOBAL STATUS: RUNNING LIVE** (Target: **{bot.coin}**, Budget: **₹{bot.trade_amount_inr}**, Check Interval: **{bot.check_interval} min**)")
    else:
        st.error("🔴 **GLOBAL STATUS: STOPPED**")
        
    st.subheader("📋 Live Activity Log (IST - India Local Time)")
    
    if not bot.trade_log:
        st.info("No activity recorded yet.")
    else:
        # Convert raw timestamps to Indian Standard Time (+5:30) at UI render time
        formatted_logs = []
        ist_offset = timedelta(hours=5, minutes=30)
        
        for entry in bot.trade_log:
            e = entry.copy()
            if "timestamp" in e:
                utc_dt = datetime.fromtimestamp(e["timestamp"], tz=timezone.utc)
                ist_dt = utc_dt + ist_offset
                e["Time (IST)"] = ist_dt.strftime("%I:%M:%S %p")
                del e["timestamp"]
            elif "Time" in e:
                e["Time (IST)"] = e["Time"]
                del e["Time"]
            formatted_logs.append(e)
            
        df_logs = pd.DataFrame(formatted_logs)
        
        # Ensure Time column is always displayed first
        if "Time (IST)" in df_logs.columns:
            cols = ["Time (IST)"] + [c for c in df_logs.columns if c != "Time (IST)"]
            df_logs = df_logs[cols]
            
        st.dataframe(df_logs, use_container_width=True, hide_index=True)

live_status_board()
