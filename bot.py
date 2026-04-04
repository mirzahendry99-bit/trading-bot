import os
import time
import gate_api
import pandas as pd
import numpy as np
import urllib.request
import json
from supabase import create_client

print("BOT V7 RUNNING")

API_KEY = os.environ.get('GATE_API_KEY')
SECRET_KEY = os.environ.get('GATE_SECRET_KEY')
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')
TG_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TG_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

TAKE_PROFIT = 0.05
STOP_LOSS = 0.025
TRAILING_GAP = 0.02
MIN_VOLUME = 700000
BUY_RATIO = 0.7

BLACKLIST = [
    "3S","3L","5S","5L","TUSD","USDC","BUSD","DAI",
    "FDUSD","USD1","USDP","USDD","USDJ","ZUSD","GUSD",
    "CUSD","SUSD","STBL","FRAX","LUSD","USDN","STABLE","BARD"
]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def tg(msg):
    try:
        if not TG_TOKEN or not TG_CHAT_ID:
            return
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        body = json.dumps({"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"TG error: {e}")

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
    profit = round((sell_price - buy_price) * amount, 6)
    supabase.table("trade_history").insert({
        "pair": pair, "buy_price": buy_price, "sell_price": sell_price,
        "amount": amount, "profit": profit, "result": result
    }).execute()
    print(f"Trade saved: {result} | Profit: ${profit:.4f}")

def is_valid(pair):
    if not pair.endswith("_USDT"):
        return False
    for b in BLACKLIST:
        if b in pair:
            return False
    return True

def get_candles(client, pair):
    candles = client.list_candlesticks(currency_pair=pair, interval="5m", limit=80)
    closes = np.array([float(c[2]) for c in candles])
    volumes = np.array([float(c[5]) for c in candles])
    return closes, volumes

def calc_rsi(closes, period=14):
    s = pd.Series(closes)
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / (loss + 1e-9)
    return float((100 - 100 / (1 + rs)).iloc[-1])

def calc_ema(closes, period):
    return float(pd.Series(closes).ewm(span=period).mean().iloc[-1])

def market_ok(client):
    btc = client.list_tickers(currency_pair="BTC_USDT")[0]
    change = float(btc.change_percentage or 0)
    print(f"BTC 24h: {change:.2f}%")
    return change > -2

def score_pair(client, pair):
    try:
        closes, volumes = get_candles(client, pair)
        if len(closes) < 20:
            return 0, None
        rsi = calc_rsi(closes)
        ema20 = calc_ema(closes, 20)
        ema50 = calc_ema(closes, 50)
        ticker = client.list_tickers(currency_pair=pair)[0]
        vol_24h = float(ticker.quote_volume or 0)
        change = float(ticker.change_percentage or 0)
        price = float(ticker.last or 0)
        if price <= 0 or vol_24h < MIN_VOLUME:
            return 0, None
        vol_spike = volumes[-1] > np.mean(volumes[-20:]) * 1.5
        score = 0
        if rsi < 35: score += 2
        if ema20 > ema50: score += 2
        if change > 0: score += 1
        if vol_spike: score += 1
        if vol_24h > MIN_VOLUME: score += 1
        if change > 6: score -= 2
        print(f"{pair} RSI:{rsi:.1f} Score:{score}")
        return score, price
    except:
        return 0, None

def find_best(client):
    best_pair, best_score, best_price = None, 0, 0
    for t in client.list_tickers():
        pair = t.currency_pair
        if not is_valid(pair):
            continue
        score, price = score_pair(client, pair)
        if price and score > best_score:
            best_pair, best_score, best_price = pair, score, price
    return best_pair, best_price, best_score

def get_min_amount(client, pair):
    try:
        pairs = client.list_currency_pairs()
        for p in pairs:
            if p.id == pair:
                return float(p.min_base_amount or 0), int(p.amount_precision or 4)
    except:
        pass
    return 0, 4

def do_buy(client, pair, balance):
    funds = round(balance * BUY_RATIO * 0.97, 2)
    if funds < 1:
        raise Exception(f"Dana terlalu kecil: {funds}")

    price = float(client.list_tickers(currency_pair=pair)[0].last)
    min_amount, precision = get_min_amount(client, pair)
    estimated_amount = round(funds / price, precision)

    if min_amount > 0 and estimated_amount < min_amount:
        raise Exception(f"Order terlalu kecil: {estimated_amount} < min {min_amount}")

    print(f"Buy {pair} | Funds: {funds} USDT | Est. amount: {estimated_amount}")

    order = gate_api.Order(
        currency_pair=pair,
        type="market",
        side="buy",
        amount="0",
        price="0",
        time_in_force="ioc"
    )
    order.funds = str(funds)
    result = client.create_order(order)

    buy_price = float(result.avg_deal_price or 0)
    filled = float(result.fill_price or 0)

    time.sleep(2)

    if buy_price <= 0:
        buy_price = price
    if filled <= 0:
        filled = round(funds / buy_price, precision)

    print(f"Filled: {filled} @ {buy_price}")
    return buy_price, filled

def do_sell(client, pair, amount):
    _, precision = get_min_amount(client, pair)
    amount = round(float(amount), precision)
    price = float(client.list_tickers(currency_pair=pair)[0].last)

    print(f"Sell {pair} | Amount: {amount}")

    order = gate_api.Order(
        currency_pair=pair,
        type="market",
        side="sell",
        amount=str(amount),
        time_in_force="ioc"
    )
    result = client.create_order(order)
    sell_price = float(result.avg_deal_price or price)
    return sell_price

def run():
    client = setup_client()
    print("=== START ===")

    if not market_ok(client):
        print("Market bearish, skip")
        return

    balance = get_balance(client)
    print(f"Balance: {balance:.2f} USDT")

    if balance < 1:
        print("Balance terlalu kecil")
        return

    position = load_position()

    if position:
        pair = position["pair"]
        buy_price = float(position.get("buy_price") or 0)
        amount = float(position.get("amount") or 0)
        peak = float(position.get("peak_price") or buy_price)

        if amount <= 0:
            print(f"Amount invalid, clear position")
            clear_position()
            return

        current_price = float(client.list_tickers(currency_pair=pair)[0].last)
        if current_price <= 0:
            print(f"Harga {pair} tidak valid")
            return

        peak = max(peak, current_price)
        tp = buy_price * (1 + TAKE_PROFIT)
        sl = buy_price * (1 - STOP_LOSS)
        trailing = peak * (1 - TRAILING_GAP)

        print(f"HOLD {pair} | Now:{current_price:.6f} TP:{tp:.6f} SL:{sl:.6f}")

        if current_price >= tp:
            sell_price = do_sell(client, pair, amount)
            profit = round((sell_price - buy_price) * amount, 4)
            save_trade(pair, buy_price, sell_price, amount, "TAKE_PROFIT")
            clear_position()
            print(f"TAKE PROFIT | Profit: ${profit}")
            tg(f"TAKE PROFIT\nPair: {pair}\nBuy: ${buy_price:.6f}\nSell: ${sell_price:.6f}\nProfit: +${profit:.4f}")

        elif current_price <= sl or current_price <= trailing:
            sell_price = do_sell(client, pair, amount)
            loss = round((sell_price - buy_price) * amount, 4)
            reason = "STOP_LOSS" if current_price <= sl else "TRAILING"
            save_trade(pair, buy_price, sell_price, amount, reason)
            clear_position()
            print(f"{reason} | Loss: ${loss}")
            tg(f"{reason}\nPair: {pair}\nBuy: ${buy_price:.6f}\nSell: ${sell_price:.6f}\nLoss: ${loss:.4f}")

        else:
            supabase.table("positions").update({"peak_price": peak}).eq("status", "open").execute()
            print(f"Holding | Peak: {peak:.6f}")
        return

    pair, price, score = find_best(client)

    if not pair or score < 4:
        print("No valid signal")
        return

    print(f"ENTRY {pair} | Score: {score} | Price: {price}")

    try:
        buy_price, amount = do_buy(client, pair, balance)

        if buy_price <= 0 or amount <= 0:
            print(f"Order tidak valid | price={buy_price} amount={amount}")
            return

        save_position({
            "pair": pair, "buy_price": buy_price,
            "amount": amount, "peak_price": buy_price, "status": "open"
        })

        usdt_spent = round(amount * buy_price, 2)
        print(f"BOUGHT {pair} | Price: {buy_price} | Amount: {amount} | Spent: ${usdt_spent}")
        tg(f"BUY\nPair: {pair}\nPrice: ${buy_price:.6f}\nAmount: {amount}\nModal: ~${usdt_spent} USDT\nScore: {score}")

    except Exception as e:
        print(f"Trade error: {e}")
        tg(f"TRADE ERROR\nPair: {pair}\n{e}")

if __name__ == "__main__":
    run()
