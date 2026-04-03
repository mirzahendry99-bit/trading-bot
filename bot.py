import os
import gate_api
import pandas as pd
import numpy as np
from supabase import create_client

print("🚀 BOT FINAL STABLE RUNNING")

API_KEY = os.environ.get('GATE_API_KEY')
SECRET_KEY = os.environ.get('GATE_SECRET_KEY')
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')

TAKE_PROFIT = 0.05
STOP_LOSS = 0.025
TRAILING_GAP = 0.02
MIN_VOLUME = 700000
MIN_USDT_ORDER = 5   # ✅ FIX MIN ORDER

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def setup_client():
    config = gate_api.Configuration(
        host="https://api.gateio.ws/api/v4",
        key=API_KEY,
        secret=SECRET_KEY
    )
    return gate_api.SpotApi(gate_api.ApiClient(config))

def get_balance(client):
    for acc in client.list_spot_accounts():
        if acc.currency == "USDT":
            return float(acc.available)
    return 0

def save_position(data):
    supabase.table("positions").delete().neq("id", 0).execute()
    supabase.table("positions").insert(data).execute()

def load_position():
    res = supabase.table("positions").select("*").eq("status", "open").execute()
    return res.data[0] if res.data else None

def clear_position():
    supabase.table("positions").update({"status": "closed"}).eq("status", "open").execute()

def save_trade(pair, buy_price, sell_price, amount, result):
    profit = (sell_price - buy_price) * amount
    supabase.table("trade_history").insert({
        "pair": pair,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "amount": amount,
        "profit": profit,
        "result": result
    }).execute()
    print(f"📝 {result} | Profit: ${profit:.4f}")

def is_valid_pair(pair):
    blacklist = ["3S","3L","5S","5L","TUSD","USDC","BUSD","DAI","FDUSD"]
    return pair.endswith("_USDT") and not any(x in pair for x in blacklist)

def get_candles(client, pair):
    candles = client.list_candlesticks(currency_pair=pair, interval="5m", limit=80)
    closes = np.array([float(c[2]) for c in candles])
    volumes = np.array([float(c[5]) for c in candles])
    return closes, volumes

def rsi(data, period=14):
    s = pd.Series(data)
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / (loss + 1e-9)
    return float((100 - (100 / (1 + rs))).iloc[-1])

def ema(data, period):
    return float(pd.Series(data).ewm(span=period).mean().iloc[-1])

def market_ok(client):
    btc = client.list_tickers(currency_pair="BTC_USDT")[0]
    change = float(btc.change_percentage or 0)
    print(f"BTC change: {change}%")
    return change > -2

def score_coin(client, pair):
    try:
        closes, volumes = get_candles(client, pair)
        r = rsi(closes)
        e20 = ema(closes, 20)
        e50 = ema(closes, 50)

        vol_spike = volumes[-1] > np.mean(volumes[-20:]) * 1.5
        ticker = client.list_tickers(currency_pair=pair)[0]

        volume = float(ticker.quote_volume or 0)
        change = float(ticker.change_percentage or 0)
        price = float(ticker.last or 0)

        if price <= 0:
            return 0, None

        score = 0
        if r < 35: score += 2
        if e20 > e50: score += 2
        if change > 0: score += 1
        if vol_spike: score += 1
        if volume > MIN_VOLUME: score += 1
        if change > 6: score -= 2

        print(f"{pair} RSI:{r:.1f} Score:{score}")
        return score, price

    except:
        return 0, None

def find_best(client):
    best_pair, best_score, best_price = None, 0, 0

    for t in client.list_tickers():
        pair = t.currency_pair
        if not is_valid_pair(pair):
            continue

        score, price = score_coin(client, pair)

        if price and score > best_score:
            best_pair = pair
            best_score = score
            best_price = price

    return best_pair, best_price, best_score

# =======================
# ✅ FIX BUY ENGINE
# =======================
def market_buy(client, pair, usdt):
    funds = round(usdt * 0.97, 2)

    if funds < MIN_USDT_ORDER:
        raise Exception(f"Order terlalu kecil ({funds} USDT)")

    order = gate_api.Order(
        currency_pair=pair,
        type="market",
        side="buy"
    )
    order.funds = str(funds)

    result = client.create_order(order)

    # =========================
    # 🔥 VALIDASI HARD (WAJIB)
    # =========================

    # kalau API ga kasih deal sama sekali → gagal
    if not result or not hasattr(result, "status"):
        raise Exception("Order gagal total (no response)")

    # ambil data penting
    buy_price = float(result.avg_deal_price or 0)

    filled = float(
        getattr(result, "filled_amount", 0) or
        getattr(result, "amount", 0) or
        0
    )

    # ❗ VALIDASI UTAMA
    if filled == 0 or buy_price == 0:
        raise Exception(f"Order tidak ke-fill (pair kemungkinan illiquid)")

    return result, buy_price, filled
    
def market_sell(client, pair, amount):
    if amount <= 0:
        raise Exception("Amount invalid")

    order = gate_api.Order(
        currency_pair=pair,
        type="market",
        side="sell",
        amount=str(amount)
    )

    return client.create_order(order)

def run_bot():
    client = setup_client()
    print("=== ENGINE START ===")

    if not market_ok(client):
        print("❌ Market risk-off, skip")
        return

    balance = get_balance(client)
    print(f"💰 Balance: {balance}")

    if balance < MIN_USDT_ORDER:
        print("❌ Balance terlalu kecil")
        return

    position = load_position()

    # =======================
    # HOLD MODE
    # =======================
    if position:
        pair = position["pair"]
        buy_price = position["buy_price"]
        amount = position["amount"]
        peak = position.get("peak_price", buy_price)

        current_price = float(client.list_tickers(currency_pair=pair)[0].last)
        peak = max(peak, current_price)

        tp = buy_price * (1 + TAKE_PROFIT)
        sl = buy_price * (1 - STOP_LOSS)
        trailing = peak * (1 - TRAILING_GAP)

        print(f"HOLD {pair} | {current_price}")

        if current_price >= tp:
            sell = market_sell(client, pair, amount)
            price = float(sell.avg_deal_price or current_price)
            save_trade(pair, buy_price, price, amount, "TP")
            clear_position()
            print("🚀 TAKE PROFIT")

        elif current_price <= sl or current_price <= trailing:
            sell = market_sell(client, pair, amount)
            price = float(sell.avg_deal_price or current_price)
            save_trade(pair, buy_price, price, amount, "SL")
            clear_position()
            print("❌ STOP LOSS")

        else:
            supabase.table("positions").update({
                "peak_price": peak
            }).eq("status", "open").execute()

        return

    # =======================
    # ENTRY MODE
    # =======================
    pair, price, score = find_best(client)

    if not pair or score < 4:
        print("❌ No signal")
        return

    usdt = balance * 0.7

    print(f"🔥 ENTRY {pair} | Score {score}")

    try:
        order, buy_price, amount = market_buy(client, pair, usdt)

        save_position({
            "pair": pair,
            "buy_price": buy_price,
            "amount": amount,
            "peak_price": buy_price,
            "status": "open"
        })

        print(f"✅ BOUGHT {pair}")

    except Exception as e:
        print(f"❌ Trade error: {e}")

if __name__ == "__main__":
    run_bot()
