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
# 3. AUTONOMOUS MULTI-COIN PORTFOLIO ENGINE
# ==========================================
class GlobalBotEngine:
    def __init__(self):
        self.is_running = False
        self.trade_log = []
        self.thread = None
        
        # User defined rules
        self.candidates = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
        self.max_budget = 500.0
        self.trade_amount = 110.0
        self.tp_pct = 2.0
        self.sl_pct = 3.0
        self.check_interval = 5
        
        # Internal State Management
        self.inr_balance = 0.0
        self.active_positions = {}  # Format: {'BTC': {'qty': 0.01, 'entry_price': 50000, 'invested': 110}}
        self.last_prices = {}       # Live prices stored for the UI dashboard
        self.realized_pnl = 0.0
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

    def start(self, candidates, max_budget, trade_amount, tp_pct, sl_pct, check_interval):
        if not self.is_running:
            self.candidates = [c.strip().upper() for c in candidates.split(",")]
            self.max_budget = max_budget
            self.trade_amount = trade_amount
            self.tp_pct = tp_pct
            self.sl_pct = sl_pct
            self.check_interval = check_interval
            
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
        # 1. Fetch Actual Balances (Ghost Recovery Sync)
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

        # 2. Fetch Live Market Tickers & Precisions
        markets = requests.get("https://api.coindcx.com/exchange/v1/markets_details").json()
        market_precision = {m['symbol']: int(m.get('target_currency_precision', 5)) for m in markets if 'symbol' in m}
        
        tickers = requests.get("https://api.coindcx.com/exchange/ticker").json()
        self.last_prices = {t['market']: float(t['last_price']) for t in tickers if 'market' in t}

        # RECOVERY LOGIC: If app restarted, re-add existing coins to budget tracking
        for coin in self.candidates:
            if coin in actual_balances and coin not in self.active_positions:
                market = f"{coin}INR"
                curr_price = self.last_prices.get(market)
                if curr_price:
                    value = actual_balances[coin] * curr_price
                    if value > 50: # Only track if holding more than ₹50 worth
                        self.active_positions[coin] = {
                            "qty": actual_balances[coin],
                            "entry_price": curr_price,
                            "invested": value
                        }

        # CLEANUP: Remove coins from tracker if user manually sold them in the CoinDCX app
        for coin in list(self.active_positions.keys()):
            if coin not in actual_balances or (actual_balances[coin] * self.last_prices.get(f"{coin}INR", 0) < 50):
                del self.active_positions[coin]

        # 3. MANAGE HELD POSITIONS (Auto Take-Profit & Stop-Loss)
        for coin in list(self.active_positions.keys()):
            market = f"{coin}INR"
            curr_price = self.last_prices.get(market)
            if not curr_price: continue
            
            pos = self.active_positions[coin]
            pnl_pct = ((curr_price - pos['entry_price']) / pos['entry_price']) * 100
            
            # TRIGGER: Execute strict math-based sell
            if pnl_pct >= self.tp_pct or pnl_pct <= -self.sl_pct:
                action_type = "TAKE PROFIT" if pnl_pct >= self.tp_pct else "STOP LOSS"
                reasoning = f"{action_type} activated at {pnl_pct:.2f}%."
                
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
                        self.log_trade("SELL", coin, formatted_qty, f"₹{actual_value:.2f}", reasoning, "Success")
                        del self.active_positions[coin]
                        self.cooldown_counter = 2 # Rest for a few cycles after a big move
                        return # Only do one major action per cycle to prevent rate-limits
                    else:
                        err = res.get("message", "API Error")
                        self.log_trade("SELL FAILED", coin, formatted_qty, f"₹{actual_value:.2f}", f"Rejected: {err}", "Error")

        if self.cooldown_counter > 0:
            self.cooldown_counter -= 1
            return

        # 4. LOOK FOR NEW OPPORTUNITIES (Multi-Coin Auto-Scanning)
        current_invested = sum(p['invested'] for p in self.active_positions.values())
        
        # STRICT BUDGET LOCK: Only buy if total invested + new trade fits inside Max Budget
        if (current_invested + self.trade_amount) <= self.max_budget:
            
            lowest_rsi = 100
            best_coin = None
            best_market = None
            best_price = 0
            best_df_str = ""

            # Scan all candidate coins
            for coin in self.candidates:
                if coin in self.active_positions: continue # Don't buy a coin we already hold
                
                market = f"{coin}INR"
                curr_price = self.last_prices.get(market)
                if not curr_price: continue
                
                candle_pair = f"I-{coin}_INR"
                try:
                    url = f"https://public.coindcx.com/market_data/candles?pair={candle_pair}&interval=15m&limit=30"
                    candles_data = requests.get(url).json()
                    df = pd.DataFrame(candles_data)
                    df['close'] = df['close'].astype(float)
                    df = df.sort_values(by='time', ascending=True)
                    
                    # Calculate RSI
                    delta = df['close'].diff()
                    gain = delta.clip(lower=0)
                    loss = -1 * delta.clip(upper=0)
                    avg_gain = gain.rolling(window=14).mean()
                    avg_loss = loss.rolling(window=14).mean()
                    rs = avg_gain / avg_loss
                    df['RSI'] = 100 - (100 / (1 + rs))
                    
                    latest_rsi = df['RSI'].iloc[-1]
                    
                    # Find the absolute CHEAPEST (most oversold) coin right now
                    if not pd.isna(latest_rsi) and latest_rsi < lowest_rsi:
                        lowest_rsi = latest_rsi
                        best_coin = coin
                        best_market = market
                        best_price = curr_price
                        df['time'] = pd.to_datetime(df['time'], unit='ms')
                        best_df_str = df[['time', 'close', 'RSI']].tail(10).to_string(index=False)
                except Exception:
                    continue

            # 5. AI VERIFICATION (Only feed the best coin to the AI)
            if best_coin and lowest_rsi < 45: # Only bother the AI if it's actually a dip
                client = Groq(api_key=GROQ_API_KEY)
                system_prompt = """You are an elite crypto hedge fund bot. You are evaluating the most OVERSOLD coin currently detected by the scanner.
                - If RSI is strictly below 40, output 'BUY' to buy the dip.
                - If RSI is recovering or above 40, output 'HOLD'.
                You MUST respond ONLY with a valid JSON object containing:
                "action": strictly "BUY" or "HOLD",
                "reasoning": a brief 1-sentence analytical explanation mentioning the RSI."""
                
                try:
                    response = client.chat.completions.create(
                        model="openai/gpt-oss-120b",
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": f"Analyze metrics for {best_market}:\n\n{best_df_str}"}
                        ],
                        response_format={"type": "json_object"},
                        temperature=0.1
                    )
                    decision_data = json.loads(response.choices[0].message.content)
                    action = decision_data.get("action", "HOLD").upper()
                    reasoning = decision_data.get("reasoning", "No reason provided.")

                    # 6. EXECUTE AUTO-BUY
                    if action == "BUY":
                        precision = market_precision.get(best_market, 5)
                        multiplier = 10 ** precision
                        raw_crypto = self.trade_amount / best_price
                        
                        if (raw_crypto * best_price) >= 101.0:
                            buy_crypto_amount = math.floor(raw_crypto * multiplier) / multiplier
                        else:
                            buy_crypto_amount = math.ceil(raw_crypto * multiplier) / multiplier
                            
                        actual_cost = buy_crypto_amount * best_price
                        
                        if actual_cost > self.inr_balance:
                            self.log_trade("BUY SKIPPED", best_coin, "0", f"₹{actual_cost:.2f}", "Insufficient INR Balance.", "Failed")
                        else:
                            formatted_qty = f"{buy_crypto_amount:.{precision}f}"
                            order_body = {
                                "side": "buy",
                                "order_type": "market_order",
                                "market": best_market,
                                "total_quantity": formatted_qty,
                                "timestamp": int(round(time.time() * 1000))
                            }
                            res = coindcx_auth_post("/exchange/v1/orders/create", order_body)
                            if "orders" in res or "id" in res:
                                # Lock in the budget and cost
                                self.active_positions[best_coin] = {
                                    "qty": buy_crypto_amount,
                                    "entry_price": best_price,
                                    "invested": actual_cost
                                }
                                self.log_trade("BUY", best_coin, formatted_qty, f"₹{actual_cost:.2f}", reasoning, "Success")
                                self.cooldown_counter = 2
                            else:
                                err = res.get("message", "API Error")
                                self.log_trade("BUY FAILED", best_coin, formatted_qty, f"₹{actual_cost:.2f}", f"Rejected: {err}", "Error")
                except Exception as e:
                    pass


# Cache instance globally across ALL web sessions
@st.cache_resource
def get_global_bot_v9():
    return GlobalBotEngine()

bot = get_global_bot_v9()

# ==========================================
# 4. STREAMLIT UI & PORTFOLIO DASHBOARD
# ==========================================
st.set_page_config(page_title="AI Portfolio Manager", layout="wide")
st.title("🌐 Fully Autonomous AI Portfolio Manager")

# Settings Expander (Collapses automatically when bot is running)
with st.expander("⚙️ Autopilot Configuration & Start", expanded=not bot.is_running):
    st.info("The bot will automatically scan the coins below, select the best dips, and trade automatically within your Max Budget limit.")
    
    cand_input = st.text_input("Candidate Coins to Scan (comma separated)", value="BTC, ETH, SOL, XRP, DOGE")
    
    colA, colB = st.columns(2)
    with colA:
        max_bud = st.number_input("Max Total Portfolio Budget (INR)", min_value=200.0, value=float(bot.max_budget), step=100.0)
        tp_pct = st.number_input("Auto Take-Profit (%)", min_value=0.5, value=float(bot.tp_pct), step=0.5)
    with colB:
        trade_amt = st.number_input("Trade Size Per Coin (INR)", min_value=105.0, value=float(bot.trade_amount), step=10.0, help="CoinDCX minimum order is ₹100. Use ₹105+ to be safe.")
        sl_pct = st.number_input("Auto Stop-Loss (%)", min_value=0.5, value=float(bot.sl_pct), step=0.5)
        
    interval_input = st.number_input("Scan Market Every (Minutes)", min_value=1, value=int(bot.check_interval))

    ctrl_col1, ctrl_col2, ctrl_col3 = st.columns(3)
    with ctrl_col1:
        if st.button("▶️ START AUTOPILOT", type="primary", use_container_width=True, disabled=bot.is_running):
            bot.start(cand_input, max_bud, trade_amt, tp_pct, sl_pct, interval_input)
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

# --- ISOLATED AUTO-REFRESHING DASHBOARD ---
@st.fragment(run_every="10s")
def live_status_board():
    if bot.is_running:
        st.success(f"🟢 **AUTOPILOT ACTIVE** | Scanning {len(bot.candidates)} coins | Max Budget: **₹{bot.max_budget}** | Target Profit: **+{bot.tp_pct}%**")
    else:
        st.error("🔴 **BOT STOPPED**")
        
    # --- Top Portfolio Metrics ---
    current_invested = sum(p['invested'] for p in bot.active_positions.values())
    unrealized_pnl = 0.0
    for coin, pos in bot.active_positions.items():
        live_p = bot.last_prices.get(f"{coin}INR", pos['entry_price'])
        unrealized_pnl += (pos['qty'] * live_p) - pos['invested']

    metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
    metric_col1.metric("💰 Available INR Wallet", f"₹{bot.inr_balance:,.2f}")
    metric_col2.metric("💳 Budget Utilized", f"₹{current_invested:,.2f}", f"of ₹{bot.max_budget:,.2f} Max", delta_color="off")
    metric_col3.metric("📈 Unrealized PnL", f"₹{unrealized_pnl:,.2f}", f"{(unrealized_pnl/current_invested*100):.2f}%" if current_invested else "0.00%")
    metric_col4.metric("🏦 Realized Profit (Session)", f"₹{bot.realized_pnl:,.2f}")

    # --- Live Active Positions Table ---
    st.subheader("💼 Active Portfolio Positions")
    if not bot.active_positions:
        st.info("No active positions held. Waiting for AI to find the perfect dip...")
    else:
        pos_data = []
        for coin, pos in bot.active_positions.items():
            curr_price = bot.last_prices.get(f"{coin}INR", pos['entry_price'])
            curr_val = pos['qty'] * curr_price
            pnl = curr_val - pos['invested']
            pnl_pct = (pnl / pos['invested']) * 100
            
            pos_data.append({
                "Coin": coin,
                "Invested (INR)": f"₹{pos['invested']:.2f}",
                "Current Value": f"₹{curr_val:.2f}",
                "Entry Price": f"₹{pos['entry_price']:.2f}",
                "Current Price": f"₹{curr_price:.2f}",
                "PnL %": f"{pnl_pct:.2f}%",
                "PnL (INR)": f"₹{pnl:.2f}"
            })
        st.dataframe(pd.DataFrame(pos_data), use_container_width=True)

    # --- Activity Logs ---
    st.subheader("📋 Autonomous Activity Log")
    if not bot.trade_log:
        st.caption("No activity recorded yet.")
    else:
        formatted_logs = []
        ist_offset = timedelta(hours=5, minutes=30)
        
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

live_status_board()
