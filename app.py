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
# 2. PERSISTENT STORAGE HELPERS
# ==========================================
HISTORY_FILE = "trades_history.json"

def load_trade_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_trade_history(completed_trades):
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(completed_trades, f, indent=2)
    except Exception:
        pass

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
# 3. PRO-TRADER PORTFOLIO ENGINE
# ==========================================
class GlobalBotEngine:
    def __init__(self):
        self.is_running = False
        self.trade_log = []
        self.thread = None
        
        self.candidates = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
        self.max_budget = 500.0
        self.trade_amount = 110.0
        self.tp_pct = 2.0
        self.sl_pct = 3.0
        self.check_interval = 5
        
        self.inr_balance = 0.0
        self.active_positions = {}
        self.last_prices = {}
        self.completed_trades = load_trade_history()
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
        # 1. Fetch Actual Balances
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

        # RECOVERY LOGIC
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

        # CLEANUP
        for coin in list(self.active_positions.keys()):
            if coin not in actual_balances or (actual_balances[coin] * self.last_prices.get(f"{coin}INR", 0) < 50):
                del self.active_positions[coin]

        # 3. MANAGE HELD POSITIONS (Pro-Trader AI Exits)
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
                url = f"https://public.coindcx.com/market_data/candles?pair={candle_pair}&interval=15m&limit=40"
                candles_data = requests.get(url).json()
                df = pd.DataFrame(candles_data)
                df['close'] = df['close'].astype(float)
                df = df.sort_values(by='time', ascending=True)
                
                # --- RSI ---
                delta = df['close'].diff()
                gain = delta.clip(lower=0)
                loss = -1 * delta.clip(upper=0)
                avg_gain = gain.rolling(window=14).mean()
                avg_loss = loss.rolling(window=14).mean()
                rs = avg_gain / avg_loss
                df['RSI'] = 100 - (100 / (1 + rs))

                # --- MACD ---
                exp1 = df['close'].ewm(span=12, adjust=False).mean()
                exp2 = df['close'].ewm(span=26, adjust=False).mean()
                df['MACD'] = exp1 - exp2
                df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
                df['MACD_Hist'] = df['MACD'] - df['Signal']

                # --- Bollinger Bands ---
                df['SMA_20'] = df['close'].rolling(window=20).mean()
                df['STD_20'] = df['close'].rolling(window=20).std()
                df['Upper_BB'] = df['SMA_20'] + (df['STD_20'] * 2)
                
                latest_rsi = df['RSI'].iloc[-1]
                
                # Pro-Trader Exit Logic: Only bother AI if we are heavily overbought or hitting upper BB
                if latest_rsi > 60 or curr_price >= df['Upper_BB'].iloc[-1]:
                    df['time'] = pd.to_datetime(df['time'], unit='ms')
                    df_str = df[['time', 'close', 'RSI', 'MACD_Hist', 'Upper_BB']].tail(8).to_string(index=False)
                    
                    client = Groq(api_key=GROQ_API_KEY)
                    sell_prompt = """You are a ruthless, highly profitable quantitative crypto trader managing a HELD position.
                    Your goal is to maximize ROI and lock in profits at absolute peaks.
                    Analyze the RSI, MACD Histogram (momentum), and Upper Bollinger Band.
                    - If price is hitting/exceeding the Upper_BB AND MACD momentum is dying or RSI > 65, output 'SELL' to secure max profit.
                    - Otherwise, if the trend is still strong, output 'HOLD' to let profits run.
                    Respond ONLY with JSON: {"action": "SELL" or "HOLD", "reasoning": "1 sentence explanation"}"""
                    
                    response = client.chat.completions.create(
                        model="openai/gpt-oss-120b",
                        messages=[
                            {"role": "system", "content": sell_prompt},
                            {"role": "user", "content": f"Held coin {market} metrics:\n\n{df_str}"}
                        ],
                        response_format={"type": "json_object"},
                        temperature=0.1
                    )
                    decision_data = json.loads(response.choices[0].message.content)
                    if decision_data.get("action", "").upper() == "SELL":
                        ai_sell_signal = True
                        ai_reason = decision_data.get("reasoning", "AI locked in maximum profit at peak.")
                    else:
                        self.log_trade("AI HOLD", coin, f"{pos['qty']:.4f}", f"₹{curr_val:.2f}", decision_data.get("reasoning", "AI letting profits run."), "Trailing Peak")
            except Exception:
                pass

            # TRIGGER: Math TP/SL OR Pro-Trader AI Peak Sell
            is_tp = pnl_pct >= self.tp_pct
            is_sl = pnl_pct <= -self.sl_pct
            
            if is_tp or is_sl or ai_sell_signal:
                if is_tp:
                    action_type, reasoning = "TAKE PROFIT", f"Hard Take-Profit triggered at +{pnl_pct:.2f}%."
                elif is_sl:
                    action_type, reasoning = "STOP LOSS", f"Hard Stop-Loss triggered at {pnl_pct:.2f}%."
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
                        save_trade_history(self.completed_trades)
                        
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

        # 4. LOOK FOR NEW OPPORTUNITIES
        current_invested = sum(p['invested'] for p in self.active_positions.values())
        
        if (current_invested + self.trade_amount) <= self.max_budget:
            lowest_rsi = 100
            best_coin = None
            best_market = None
            best_price = 0
            best_df_str = ""

            for coin in self.candidates:
                if coin in self.active_positions: continue
                
                market = f"{coin}INR"
                curr_price = self.last_prices.get(market)
                if not curr_price: continue
                
                candle_pair = f"I-{coin}_INR"
                try:
                    url = f"https://public.coindcx.com/market_data/candles?pair={candle_pair}&interval=15m&limit=40"
                    candles_data = requests.get(url).json()
                    df = pd.DataFrame(candles_data)
                    df['close'] = df['close'].astype(float)
                    df = df.sort_values(by='time', ascending=True)
                    
                    # --- TA Calculation ---
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
                    
                    if not pd.isna(latest_rsi) and latest_rsi < lowest_rsi:
                        lowest_rsi = latest_rsi
                        best_coin = coin
                        best_market = market
                        best_price = curr_price
                        df['time'] = pd.to_datetime(df['time'], unit='ms')
                        best_df_str = df[['time', 'close', 'RSI', 'MACD_Hist', 'Lower_BB']].tail(8).to_string(index=False)
                except Exception:
                    continue

            # 5. PRO-TRADER AI BUY VERIFICATION
            if best_coin and lowest_rsi < 45:
                client = Groq(api_key=GROQ_API_KEY)
                system_prompt = """You are a ruthless, highly profitable quantitative crypto trader.
                You do not buy "falling knives". You only buy precise bounce setups.
                Analyze the RSI, MACD Histogram, and Lower Bollinger Band (Lower_BB):
                - If RSI < 45 AND price is bouncing off/near the Lower_BB AND MACD Histogram shows momentum shifting upward (becoming less negative or turning positive), output 'BUY'.
                - If it is just crashing with no sign of slowing down, output 'HOLD'.
                Respond ONLY with JSON: {"action": "BUY" or "HOLD", "reasoning": "1 sentence explanation"}"""
                
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
                    else:
                        self.log_trade("PRO AI HOLD", best_coin, "0", f"₹{best_price:.2f}", reasoning, "Avoiding Falling Knife")
                except Exception:
                    pass
            else:
                display_coin = best_coin if best_coin else "N/A"
                display_rsi = lowest_rsi if lowest_rsi != 100 else 0
                self.log_trade("SCAN HOLD", display_coin, "0", f"₹{best_price:.2f}", f"Lowest RSI is {display_rsi:.1f}. Waiting for a drop below 45.", "Market Too High")
        else:
            self.log_trade("BUDGET LOCK", "PORTFOLIO", "ALL", f"₹{current_invested:.2f}", "Maximum portfolio budget reached. Waiting for a sell.", "Budget Full")

@st.cache_resource
def get_global_bot_v16():
    return GlobalBotEngine()

bot = get_global_bot_v16()

# ==========================================
# 4. STREAMLIT UI & DASHBOARD ROUTING
# ==========================================
st.set_page_config(page_title="Pro-Trader AI Portfolio Manager", layout="wide")

st.sidebar.title("Navigation")
page = st.sidebar.radio("Go to:", ["📊 Live Portfolio Dashboard", "🤖 Bot Control & Logs"])

st.sidebar.divider()
if bot.is_running:
    st.sidebar.success("🟢 Bot is **RUNNING**")
else:
    st.sidebar.error("🔴 Bot is **STOPPED**")


if page == "🤖 Bot Control & Logs":
    st.title("🤖 Bot Control & Logs")
    
    with st.expander("⚙️ Autopilot Configuration & Start", expanded=not bot.is_running):
        st.info("The Pro-Trader bot uses MACD, Bollinger Bands, and RSI to strictly trade bounces and dynamically lock in peak profits.")
        
        cand_input = st.text_input("Candidate Coins to Scan (comma separated)", value="BTC, ETH, SOL, XRP, DOGE")
        
        colA, colB = st.columns(2)
        with colA:
            max_bud = st.number_input("Max Total Portfolio Budget (INR)", min_value=200.0, value=float(bot.max_budget), step=100.0)
            tp_pct = st.number_input("Hard Take-Profit (%)", min_value=0.5, value=float(bot.tp_pct), step=0.5)
        with colB:
            trade_amt = st.number_input("Trade Size Per Coin (INR)", min_value=105.0, value=float(bot.trade_amount), step=10.0)
            sl_pct = st.number_input("Hard Stop-Loss (%)", min_value=0.5, value=float(bot.sl_pct), step=0.5)
            
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

    @st.fragment(run_every="10s")
    def bot_logs_view():
        if bot.is_running:
            st.success(f"🟢 **AUTOPILOT ACTIVE** | Scanning {len(bot.candidates)} coins | Max Budget: **₹{bot.max_budget}** | Target Profit: **+{bot.tp_pct}%**")
        else:
            st.error("🔴 **BOT STOPPED**")
            
        st.subheader("📋 Real-Time Activity Log (Live Heartbeat)")
        if not bot.trade_log:
            st.caption("No session activity recorded yet.")
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

elif page == "📊 Live Portfolio Dashboard":
    st.title("📊 Live Portfolio Dashboard")

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

        st.subheader("📊 Live Account Summary (Updates every 10s)")
        metric_col1, metric_col2, metric_col3 = st.columns(3)
        metric_col1.metric("💰 Available INR Wallet", f"₹{live_inr:,.2f}")
        metric_col2.metric("💳 Budget Utilized", f"₹{current_invested:,.2f}", f"of ₹{bot.max_budget:,.2f} Max", delta_color="off")
        metric_col3.metric("📈 Open Positions PnL", f"₹{unrealized_pnl:,.2f}", f"{(unrealized_pnl/current_invested*100):.2f}%" if current_invested else "0.00%")

        st.divider()

        st.subheader("📈 Multi-Timeframe Performance Analytics (IST)")
        
        tab_today, tab_yesterday, tab_this_m, tab_last_m, tab_all = st.tabs([
            "📅 Today", "⏪ Yesterday", "🗓️ This Month", "⏮️ Last Month", "📜 2-Year Lifetime"
        ])

        def render_period_metrics(p_data):
            net = p_data["profit"] - p_data["loss"]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("🟢 Realized Profit", f"₹{p_data['profit']:,.2f}")
            c2.metric("🔴 Realized Loss", f"₹{p_data['loss']:,.2f}")
            c3.metric("⚖️ Net P&L", f"₹{net:,.2f}", delta=f"₹{net:,.2f}")
            c4.metric("📊 Closed Trades", f"{p_data['count']}")

        with tab_today: render_period_metrics(period_stats["today"])
        with tab_yesterday: render_period_metrics(period_stats["yesterday"])
        with tab_this_m: render_period_metrics(period_stats["this_month"])
        with tab_last_m: render_period_metrics(period_stats["last_month"])
        with tab_all: render_period_metrics(period_stats["all_time"])

        st.divider()

        st.subheader("💼 Active Portfolio Positions")
        if not bot.active_positions:
            st.info("No active positions held. Waiting for AI to find the perfect dip...")
        else:
            pos_data = []
            for coin, pos in bot.active_positions.items():
                curr_price = live_prices.get(f"{coin}INR", pos['entry_price'])
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

        st.subheader("📜 Historical Closed Trades Archive")
        if not bot.completed_trades:
            st.caption("No closed trades in history file yet.")
        else:
            history_rows = []
            for trade in reversed(bot.completed_trades):
                t_dt = (datetime.fromtimestamp(trade["timestamp"], tz=timezone.utc) + ist_offset).strftime("%Y-%m-%d %I:%M:%S %p")
                history_rows.append({
                    "Time (IST)": t_dt,
                    "Coin": trade["coin"],
                    "Type": trade.get("type", "CLOSE"),
                    "Invested": f"₹{trade['invested']:.2f}",
                    "Entry Price": f"₹{trade['entry_price']:.2f}",
                    "Exit Price": f"₹{trade['exit_price']:.2f}",
                    "PnL (INR)": f"₹{trade['pnl']:.2f}",
                    "PnL %": f"{trade.get('pnl_pct', 0.0):.2f}%"
                })
            st.dataframe(pd.DataFrame(history_rows), use_container_width=True, hide_index=True)

    portfolio_view()
