import streamlit as st
import pandas as pd
import json
import time
import hmac
import hashlib
import requests
import math
from groq import Groq
from datetime import datetime

# ==========================================
# 1. STATIC API KEYS (DANGER: KEEP THIS FILE SECRET)
# ==========================================
API_KEY = "56b76b4c2ea66bb454198a7a607577e1d98c74944602eec8"
API_SECRET = "e3d5de48f74758a9095fca952b7f8b13c28b0bf3de99c671de5db4af700d52d3"
GROQ_API_KEY = "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"

# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================
def coindcx_auth_post(endpoint, body):
    """Signs and sends authenticated requests to the CoinDCX API."""
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

def log_trade(action, crypto_amt, inr_budget, reason, status):
    log_entry = {
        "Time": datetime.now().strftime("%I:%M:%S %p"),
        "Action": action,
        "Quantity": crypto_amt,
        "Target Budget / Value": inr_budget,
        "Reasoning": reason,
        "Status": status
    }
    st.session_state.trade_log.insert(0, log_entry)

# ==========================================
# 3. DASHBOARD CONFIGURATION
# ==========================================
st.set_page_config(page_title="Position-Aware AI Bot", layout="wide")
st.title("🦙 Llama-3.1 CoinDCX Bot (Position-Aware)")

if "trade_log" not in st.session_state:
    st.session_state.trade_log = []

# ==========================================
# 4. BOT SETTINGS UI
# ==========================================
col1, col2, col3 = st.columns(3)
with col1:
    coin = st.text_input("Coin Symbol (e.g., BTC, ETH, XRP)", "BTC").upper()
with col2:
    trade_amount_inr = st.number_input("Target BUY Budget (INR)", min_value=1.0, value=150.0, step=10.0)
with col3:
    check_interval = st.number_input("Check Every (Minutes)", min_value=1, value=5)

market_symbol = f"{coin}INR"
candle_pair = f"I-{coin}_INR"

# Fetch Live Balances (INR + Target Coin)
balance_body = {"timestamp": int(round(time.time() * 1000))}
balances_data = coindcx_auth_post("/exchange/v1/users/balances", balance_body)

inr_balance = 0.0
coin_balance = 0.0

st.subheader("💰 Live Wallet Balances")
if isinstance(balances_data, list):
    for b in balances_data:
        curr = b.get('currency', '')
        bal = float(b.get('balance', 0))
        if curr == 'INR':
            inr_balance = bal
        elif curr == coin:
            coin_balance = bal
            
    active_balances = [b for b in balances_data if float(b.get('balance', 0)) > 0]
    if active_balances:
        df_balance = pd.DataFrame(active_balances)[['currency', 'balance']]
        st.dataframe(df_balance.T, use_container_width=True, hide_index=True)

st.divider()
auto_mode = st.toggle("🚀 Enable Fully Automated Trading", value=False)

# ==========================================
# 5. ACTIVITY LOG VIEWER
# ==========================================
st.subheader("📋 Bot Activity Log")
log_container = st.empty()

def render_logs():
    if not st.session_state.trade_log:
        log_container.info("No actions taken yet.")
    else:
        df_logs = pd.DataFrame(st.session_state.trade_log)
        log_container.dataframe(df_logs, use_container_width=True, hide_index=True)

render_logs()
st.divider()

# ==========================================
# 6. AUTOMATION LOOP LOGIC
# ==========================================
if auto_mode:
    status_msg = st.empty()
    status_msg.warning("Bot is ACTIVE. Analyzing market...")
    
    try:
        # A. Fetch CoinDCX precision rules
        markets_url = "https://api.coindcx.com/exchange/v1/markets_details"
        markets = requests.get(markets_url).json()
        market_info = next((m for m in markets if m.get('symbol') == market_symbol), None)
        precision = int(market_info.get('target_currency_precision', 5)) if market_info else 5
        multiplier = 10 ** precision
        
        # B. Get Live Price
        tickers = requests.get("https://api.coindcx.com/exchange/ticker").json()
        ticker = next((t for t in tickers if t.get('market') == market_symbol), None)
        
        if not ticker:
            log_trade("ERROR", "0", f"₹{trade_amount_inr}", f"Market {market_symbol} not found.", "Failed")
            st.stop()
            
        current_price = float(ticker['last_price'])
        
        # C. Run AI Analysis
        candles_url = f"https://public.coindcx.com/market_data/candles?pair={candle_pair}&interval=1h&limit=10"
        candles_data = requests.get(candles_url).json()
        
        df = pd.DataFrame(candles_data)
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        df = df.sort_values(by='time', ascending=True)
        market_data_str = df[['time', 'open', 'high', 'low', 'close', 'volume']].to_string(index=False)
        
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
        
        # ==========================================
        # D. POSITION-AWARE TRADE EXECUTION
        # ==========================================
        if action == "BUY":
            # Calculate how much crypto to buy using specified INR budget
            raw_crypto = trade_amount_inr / current_price
            qty_down = math.floor(raw_crypto * multiplier) / multiplier
            val_down = qty_down * current_price
            
            if val_down >= 101.0:
                buy_crypto_amount = qty_down
            else:
                buy_crypto_amount = math.ceil(raw_crypto * multiplier) / multiplier
                
            actual_cost = buy_crypto_amount * current_price
            
            # Check INR Balance
            if actual_cost > inr_balance:
                reason_msg = f"AI signaled BUY, but cost (₹{actual_cost:.2f}) exceeds INR balance (₹{inr_balance:.2f})."
                log_trade("BUY SKIPPED", "0", f"₹{actual_cost:.2f}", reason_msg, "Insufficient INR")
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
                    log_trade("BUY", formatted_qty, f"₹{actual_cost:.2f}", reasoning, "Success")
                else:
                    err = order_response.get("message", "API Error")
                    log_trade("BUY FAILED", formatted_qty, f"₹{actual_cost:.2f}", reasoning, f"Rejected: {err}")

        elif action == "SELL":
            # SELL using the HELD CRYPTO BALANCE in the wallet
            sell_crypto_amount = math.floor(coin_balance * multiplier) / multiplier
            actual_value = sell_crypto_amount * current_price
            
            if sell_crypto_amount <= 0:
                log_trade("SELL SKIPPED", "0", "₹0.00", f"AI signaled SELL, but you hold 0 {coin}.", "No Crypto Held")
            elif actual_value < 100.0:
                reason_msg = f"AI signaled SELL, but your held {coin} ({sell_crypto_amount}) is worth ₹{actual_value:.2f}, below CoinDCX's ₹100 limit."
                log_trade("SELL SKIPPED", f"{sell_crypto_amount:.{precision}f}", f"₹{actual_value:.2f}", reason_msg, "Below Min Order")
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
                    log_trade("SELL", formatted_qty, f"₹{actual_value:.2f}", reasoning, "Success")
                else:
                    err = order_response.get("message", "API Error")
                    log_trade("SELL FAILED", formatted_qty, f"₹{actual_value:.2f}", reasoning, f"Rejected: {err}")
                    
        else:
            log_trade("HOLD", "0", "₹0.00", reasoning, "No Action Taken")
            
    except Exception as e:
        log_trade("ERROR", "0", "₹0.00", str(e), "Script Crashed")
        
    render_logs()
    
    # Sleep and Rerun Loop
    time_to_wait = check_interval * 60
    with st.spinner(f"Analysis complete. Sleeping for {check_interval} minutes..."):
        time.sleep(time_to_wait)
    
    st.rerun()