import streamlit as st
import pandas as pd
import json
import time
import hmac
import hashlib
import requests
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
    """Signs and sends requests to the CoinDCX API."""
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
    """Saves the bot's decision so you can analyze it later."""
    log_entry = {
        "Time": datetime.now().strftime("%I:%M:%S %p"),
        "Action": action,
        "Crypto Amount": crypto_amt,
        "INR Budget": f"₹{inr_budget}",
        "Reasoning": reason,
        "Exchange Status": status
    }
    st.session_state.trade_log.insert(0, log_entry) # Put newest at the top

# ==========================================
# 3. DASHBOARD CONFIG
# ==========================================
st.set_page_config(page_title="Open-Source AI Bot", layout="wide")
st.title("🦙 Llama-3 CoinDCX Bot (Fully Automated)")
st.sidebar.info("Keep this browser tab open for the bot to run continuously in the background.")

# Initialize the log memory
if "trade_log" not in st.session_state:
    st.session_state.trade_log = []

# ==========================================
# 4. BOT SETTINGS UI
# ==========================================
col1, col2, col3 = st.columns(3)
with col1:
    coin = st.text_input("Coin Symbol (e.g., BTC, ETH)", "BTC").upper()
with col2:
    trade_amount_inr = st.number_input("Fixed INR Budget per trade", min_value=1.0, value=20.0, step=10.0)
with col3:
    check_interval = st.number_input("Check Every (Minutes)", min_value=1, value=5)

market_symbol = f"{coin}INR"
candle_pair = f"I-{coin}_INR"

# The Master Switch
st.divider()
auto_mode = st.toggle("🚀 Enable Fully Automated Trading", value=False)

# ==========================================
# 5. ACTIVITY LOG VIEWER
# ==========================================
st.subheader("📋 Bot Activity Log")
log_container = st.empty()

def render_logs():
    """Updates the table on the screen with the latest memory."""
    if not st.session_state.trade_log:
        log_container.info("No actions taken yet. Turn on the switch above to start the bot.")
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
    status_msg.warning(f"Bot is ACTIVE. Running market analysis with Llama 3. Do not close this tab.")
    
    try:
        # A. Fetch Price to Calc Exact Fraction
        tickers = requests.get("https://api.coindcx.com/exchange/ticker").json()
        ticker = next((t for t in tickers if t.get('market') == market_symbol), None)
        
        if not ticker:
            log_trade("ERROR", 0, trade_amount_inr, f"Market {market_symbol} not found on CoinDCX.", "Failed")
        else:
            current_price = float(ticker['last_price'])
            
            # The Exact Decimal Calculation
            raw_crypto_amount = trade_amount_inr / current_price
            final_crypto_amount = round(raw_crypto_amount, 5) 
            
            # B. Fetch Chart Data
            candles_url = f"https://public.coindcx.com/market_data/candles?pair={candle_pair}&interval=1h&limit=10"
            candles_data = requests.get(candles_url).json()
            
            df = pd.DataFrame(candles_data)
            df['time'] = pd.to_datetime(df['time'], unit='ms')
            df = df.sort_values(by='time', ascending=True)
            market_data_str = df[['time', 'open', 'high', 'low', 'close', 'volume']].to_string(index=False)
            
            # C. Open-Source AI Decision (Llama 3 via Groq)
            client = Groq(api_key=GROQ_API_KEY)
            
            # Force the model to output strict JSON
            system_prompt = """You are a highly analytical crypto trading bot. 
            You MUST respond ONLY with a valid JSON object. Do not include any other text.
            The JSON object must contain exactly two keys:
            "action": strictly one of the words "BUY", "SELL", or "HOLD".
            "reasoning": a brief 1-sentence explanation based on the price data provided."""
            
            user_prompt = f"Analyze the last 10 hours of market data for {market_symbol}:\n\n{market_data_str}"
            
            response = client.chat.completions.create(
                model="openai/gpt-oss-120b", # Meta's fast 8-Billion parameter open-source model
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            
            # Parse the Llama 3 output
            decision_data = json.loads(response.choices[0].message.content)
            action = decision_data.get("action", "HOLD").upper()
            reasoning = decision_data.get("reasoning", "No reason provided.")
            
            # D. Execute Trade Automatically
            if action in ["BUY", "SELL"]:
                order_body = {
                    "side": action.lower(),
                    "order_type": "market_order",
                    "market": market_symbol,
                    "total_quantity": final_crypto_amount,
                    "timestamp": int(round(time.time() * 1000))
                }
                
                order_response = coindcx_auth_post("/exchange/v1/orders/create", order_body)
                
                # Verify if it succeeded
                if "orders" in order_response or "id" in order_response:
                    log_trade(action, final_crypto_amount, trade_amount_inr, reasoning, "Success")
                else:
                    error_msg = order_response.get("message", "Unknown API Error")
                    log_trade(action, final_crypto_amount, trade_amount_inr, reasoning, f"Rejected: {error_msg}")
            else:
                log_trade("HOLD", 0, trade_amount_inr, reasoning, "No Action Taken")
                
    except Exception as e:
        log_trade("ERROR", 0, 0, str(e), "Script Crashed")
        
    # Update the table UI with the new logs
    render_logs()
    
    # E. Sleep and Restart Loop
    time_to_wait = check_interval * 60
    with st.spinner(f"Analysis complete. Bot is sleeping for {check_interval} minutes before next check..."):
        time.sleep(time_to_wait)
    
    st.rerun()
