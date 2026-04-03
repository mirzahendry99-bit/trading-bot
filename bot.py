import os
import time
import json
import urllib.request
import gate_api
import pandas as pd
import numpy as np
from supabase import create_client

print("🚀 BOT RUNNING")

# =====================
# ENV VARIABLES
# =====================
API_KEY        = os.environ.get("GATE_API_KEY")
SECRET_KEY     = os.environ.get("GATE_SECRET_KEY")
SUPABASE_URL   = os.environ.get("SUPABASE_URL")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
TG_TOKEN       = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID")

# =====================
# CONFIG
# =====================
TAKE_PROFIT    = 0.05
STOP_LOSS      = 0.025
TRAILING_GAP   = 0.02
MIN_VOLUME     = 700000
MIN_USDT_ORDER = 5
BUY_RATIO      = 0.7

BLACKLIST = ["3S","3L","5S","5L","TUSD","USDC","BUSD","DAI","FDUSD","USD1"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# =====================
# TELEGRAM
# =====================
def tg(msg):
    try:
        if not TG_TOKEN or not TG_CHAT_ID:
            return
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        body = json.dumps({
            "chat_id": TG_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"⚠️ Telegram error: {e}")


# =====================
# GATE CLIENT
# =====================
def get_client():
    cfg = gate_api.Configuration(
        host="https://api.gateio.ws/api/v4",
        key=API_KEY,
        secret=SECRET_KEY
    )
    return gate_api.SpotApi(gate_api.ApiClient(cfg))


# =====================
# BALANCE
# =====================
def get_balance(client):
    try:
        for acc in client.list_spot_accounts():
            if acc.currency == "USDT":
                return float(acc.available or 0)
    except Exception as e:
        print(f"⚠️ Balance error: {e}")
    return 0.0


# =====================
# SUPABASE HELPERS
# =====================
def save_position(data):
    amt = data.get("amount")
    if not amt or float(amt) <= 0:
        raise Exception(f"Amount invalid saat save: {amt}")
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
        "pair": pair,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "amount": amount,
        "profit": profit,
        "result": result
    }).execute()
    print(f"📝 {result} | Profit: ${profit:.4f}")


# =====================
# INDICATORS
# =====================
def calc_rsi(closes, period=14):
    s     = pd.Series(closes)
    delta = s.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / (loss + 1e-9)
    return float((100 - 100 / (1 + rs)).iloc[-1])

def calc_ema(closes, period):
    return float(pd.Series(closes).ewm(span=period).mean().iloc[-1])


# =====================
# MARKET FILTER
# =====================
def market_ok(client):
    try:
        btc    = client.list_tickers(currency_pair="BTC_USDT")[0]
        change = float(btc.change_percentage or 0)
        print(f"BTC 24h: {change:.2f}%")
        return change > -2
    except Exception as e:
        print(f"⚠️ Market check error: {e}")
        return False


# =====================
# PAIR FILTER
# =====================
def is_valid(pair):
    if not pair.endswith("_USDT"):
        return False
    for b in BLACKLIST:
        if b in pair:
            return False
    return True


# =====================
# SCORING
# =====================
def score_pair(client, pair):
    try:
        candles = client.list_candlesticks(
            currency_pair=pair, interval="5m", limit=80
        )
        if not candles or len(candles) < 20:
            return 0, None

        closes  = np.array([float(c[2]) for c in candles])
        volumes = np.array([float(c[5]) for c in candles])

        rsi   = calc_rsi(closes)
        ema20 = calc_ema(closes, 20)
        ema50 = calc_ema(closes, 50)

        ticker  = client.list_tickers(currency_pair=pair)[0]
        vol_24h = float(ticker.quote_volume or 0)
        change  = float(ticker.change_percentage or 0)
        price   = float(ticker.last or 0)

        if price <= 0 or vol_24h < MIN_VOLUME:
            return 0, None

        vol_spike = volumes[-1] > np.mean(volumes[-20:]) * 1.5

        score = 0
        if rsi < 35:       score += 2
        if ema20 > ema50:  score += 2
        if change > 0:     score += 1
        if vol_spike:      score += 1
        if vol_24h > MIN_VOLUME: score += 1
        if change > 6:     score -= 2

        print(f"{pair} RSI:{rsi:.1f} Score:{score}")
        return score, price

    except Exception:
        return 0, None


# =====================
# FIND BEST PAIR
# =====================
def find_best(client):
    best_pair  = None
    best_score = 0
    best_price = 0

    tickers = client.list_tickers()
    for t in tickers:
        pair = t.currency_pair
        if not is_valid(pair):
            continue
        score, price = score_pair(client, pair)
        if price and score > best_score:
            best_pair  = pair
            best_score = score
            best_price = price

    return best_pair, best_price, best_score


# =====================
# BUY — FIX PERMANEN
# =====================
def do_buy(client, pair, usdt_balance):
    funds = round(usdt_balance * BUY_RATIO * 0.97, 2)
    if funds < MIN_USDT_ORDER:
        raise Exception(f"Dana terlalu kecil: {funds} USDT")

    order = gate_api.Order(
        currency_pair=pair,
        type="market",
        side="buy"
    )
    order.funds = str(funds)

    result = client.create_order(order)

    if result is None:
        raise Exception("Order return None")

    # ✅ FIX: tunggu sebentar lalu ambil order detail via ID
    time.sleep(2)
    order_detail = client.get_order(str(result.id), pair) if result.id else result

    buy_price = float(
    getattr(order_detail, "avg_deal_price", None) or
    getattr(result, "avg_deal_price", None) or 0
)
filled = float(
    getattr(order_detail, "filled_amount", None) or
    getattr(result, "filled_amount", None) or
    getattr(result, "amount", None) or 0
)

    if buy_price <= 0 or filled <= 0:
        raise Exception(
            f"Order tidak terisi (illiquid?) "
            f"| avg_price={buy_price} filled={filled}"
        )

    return buy_price, filled


# =====================
# SELL
# =====================
def do_sell(client, pair, amount):
    if amount is None:
        raise Exception("Amount None saat sell")

    amount = float(amount)
    if amount <= 0:
        raise Exception(f"Amount tidak valid: {amount}")

    amount = round(amount, 6)

    order = gate_api.Order(
        currency_pair=pair,
        type="market",
        side="sell",
        amount=str(amount)
    )
    result = client.create_order(order)

    time.sleep(2)
    detail = client.get_order(result.id, pair)
    return float(detail.avg_deal_price or 0)


# =====================
# MAIN BOT
# =====================
def run():
    client = get_client()
    print("=== START ===")

    if not market_ok(client):
        print("❌ Market bearish, skip")
        return

    balance = get_balance(client)
    print(f"💰 Balance: {balance:.2f} USDT")

    if balance < MIN_USDT_ORDER:
        print("❌ Balance terlalu kecil")
        return

    position = load_position()

    # =========
    # HOLD MODE
    # =========
    if position:
        pair      = position["pair"]
        buy_price = float(position.get("buy_price") or 0)
        amount    = position.get("amount")
        peak      = float(position.get("peak_price") or buy_price)

        # Validasi amount dari DB
        if amount is None or float(amount) <= 0:
            print(f"❌ Amount invalid dari DB: {amount}")
            tg(f"⚠️ <b>POSISI RUSAK</b>\nPair: {pair}\nAmount: {amount}\nAuto clear.")
            clear_position()
            return

        amount = float(amount)

        try:
            ticker        = client.list_tickers(currency_pair=pair)[0]
            current_price = float(ticker.last or 0)
        except Exception as e:
            print(f"⚠️ Gagal ambil harga {pair}: {e}")
            return

        if current_price <= 0:
            print(f"❌ Harga {pair} tidak valid")
            return

        peak     = max(peak, current_price)
        tp       = buy_price * (1 + TAKE_PROFIT)
        sl       = buy_price * (1 - STOP_LOSS)
        trailing = peak * (1 - TRAILING_GAP)

        print(
            f"HOLD {pair} | Now:{current_price:.6f} "
            f"TP:{tp:.6f} SL:{sl:.6f} Trail:{trailing:.6f}"
        )

        if current_price >= tp:
            sell_price = do_sell(client, pair, amount)
            if sell_price <= 0:
                sell_price = current_price
            profit = round((sell_price - buy_price) * amount, 4)
            save_trade(pair, buy_price, sell_price, amount, "TP")
            clear_position()
            print("🚀 TAKE PROFIT")
            tg(
                f"🚀 <b>TAKE PROFIT</b>\n"
                f"Pair: <b>{pair}</b>\n"
                f"Buy:  ${buy_price:.6f}\n"
                f"Sell: ${sell_price:.6f}\n"
                f"Amt:  {amount}\n"
                f"Profit: <b>+${profit:.4f}</b>"
            )

        elif current_price <= sl or current_price <= trailing:
            sell_price = do_sell(client, pair, amount)
            if sell_price <= 0:
                sell_price = current_price
            loss = round((sell_price - buy_price) * amount, 4)
            save_trade(pair, buy_price, sell_price, amount, "SL")
            clear_position()
            reason = "SL" if current_price <= sl else "TRAILING"
            print(f"❌ {reason}")
            tg(
                f"❌ <b>{reason}</b>\n"
                f"Pair: <b>{pair}</b>\n"
                f"Buy:  ${buy_price:.6f}\n"
                f"Sell: ${sell_price:.6f}\n"
                f"Amt:  {amount}\n"
                f"Loss: <b>${loss:.4f}</b>"
            )

        else:
            supabase.table("positions").update(
                {"peak_price": peak}
            ).eq("status", "open").execute()
            print(f"📊 Peak update: {peak:.6f}")

        return

    # ===========
    # ENTRY MODE
    # ===========
    pair, price, score = find_best(client)

    if not pair or score < 4:
        print("❌ No signal")
        return

    print(f"🔥 ENTRY {pair} | Score {score}")

    try:
        buy_price, amount = do_buy(client, pair, balance)

        save_position({
            "pair":       pair,
            "buy_price":  buy_price,
            "amount":     amount,
            "peak_price": buy_price,
            "status":     "open"
        })

        usdt_spent = round(amount * buy_price, 2)
        print(f"✅ BOUGHT {pair} | Price:{buy_price} | Amt:{amount}")
        tg(
            f"✅ <b>BUY</b>\n"
            f"Pair:  <b>{pair}</b>\n"
            f"Price: ${buy_price:.6f}\n"
            f"Amt:   {amount}\n"
            f"Modal: ${usdt_spent} USDT\n"
            f"Score: {score}"
        )

    except Exception as e:
        print(f"❌ Trade error: {e}")
        tg(f"⚠️ <b>TRADE ERROR</b>\nPair: {pair}\n{e}")


if __name__ == "__main__":
    run()
      
