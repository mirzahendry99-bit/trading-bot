import os
import time
import json
import hmac
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import gate_api
import pandas as pd
import numpy as np
from supabase import create_client

print("🚀 BOT RUNNING")

API_KEY      = os.environ.get("GATE_API_KEY")
SECRET_KEY   = os.environ.get("GATE_SECRET_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

TAKE_PROFIT    = 0.05
STOP_LOSS      = 0.025
TRAILING_GAP   = 0.02
MIN_VOLUME     = 700000
MIN_USDT_ORDER = 4
BUY_RATIO      = 0.4

BLACKLIST = [
    "3S","3L","5S","5L",
    "TUSD","USDC","BUSD","DAI","FDUSD","USD1",
    "USDP","USDD","USDJ","ZUSD","GUSD","CUSD","SUSD",
    "STBL","FRAX","LUSD","USDN","STABLE","BARD"
]

BASE_URL = "https://api.gateio.ws/api/v4"

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
# GATE.IO SIGNING
# =====================
def gate_sign(method, path, query_string="", body=""):
    ts        = str(int(time.time()))
    body_hash = hashlib.sha512(body.encode("utf-8")).hexdigest()
    sign_str  = "\n".join([method, path, query_string, body_hash, ts])
    signature = hmac.new(
        SECRET_KEY.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha512
    ).hexdigest()
    return ts, signature


def gate_request(method, path, params=None, data=None):
    query_string = urllib.parse.urlencode(params) if params else ""
    body         = json.dumps(data) if data else ""
    ts, signature = gate_sign(method, path, query_string, body)

    url = BASE_URL + path
    if query_string:
        url += "?" + query_string

    headers = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "KEY":          API_KEY,
        "SIGN":         signature,
        "Timestamp":    ts
    }

    req = urllib.request.Request(
        url,
        data=body.encode("utf-8") if body else None,
        headers=headers,
        method=method
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8")
        raise Exception(f"Gate HTTP {e.code}: {err}")


def gate_get_balance():
    accounts = gate_request("GET", "/spot/accounts")
    for acc in accounts:
        if acc.get("currency") == "USDT":
            return float(acc.get("available", 0))
    return 0.0


def gate_get_price(pair):
    try:
        data = gate_request("GET", "/spot/tickers", params={"currency_pair": pair})
        if data:
            return float(data[0].get("last", 0))
    except Exception:
        pass
    return 0.0


def gate_create_order(pair, side, funds=None, amount=None):
    payload = {
        "currency_pair": pair,
        "type":          "market",
        "side":          side,
        "time_in_force": "ioc"
    }
    if funds is not None:
        payload["funds"] = str(round(float(funds), 2))
    if amount is not None:
        payload["amount"] = str(amount)
    return gate_request("POST", "/spot/orders", data=payload)


def gate_get_order(order_id, pair):
    return gate_request(
        "GET", f"/spot/orders/{order_id}",
        params={"currency_pair": pair}
    )


# =====================
# GATE CLIENT (scan)
# =====================
def get_client():
    cfg = gate_api.Configuration(
        host="https://api.gateio.ws/api/v4",
        key=API_KEY,
        secret=SECRET_KEY
    )
    return gate_api.SpotApi(gate_api.ApiClient(cfg))


# =====================
# SUPABASE
# =====================
def save_position(data):
    amt = data.get("amount")
    if not amt or float(amt) <= 0:
        raise Exception(f"Amount invalid: {amt}")
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
        "pair":       pair,
        "buy_price":  buy_price,
        "sell_price": sell_price,
        "amount":     amount,
        "profit":     profit,
        "result":     result
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


def is_valid(pair):
    if not pair.endswith("_USDT"):
        return False
    for b in BLACKLIST:
        if b in pair:
            return False
    return True


def get_pair_precision(client, pair):
    try:
        pairs = client.list_currency_pairs()
        for p in pairs:
            if p.id == pair:
                return int(p.amount_precision or 2)
    except Exception:
        pass
    return 2


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
        if rsi < 35:             score += 2
        if ema20 > ema50:        score += 2
        if change > 0:           score += 1
        if vol_spike:            score += 1
        if vol_24h > MIN_VOLUME: score += 1
        if change > 6:           score -= 2

        print(f"{pair} RSI:{rsi:.1f} Score:{score}")
        return score, price

    except Exception:
        return 0, None


def find_best(client):
    best_pair  = None
    best_score = 0
    best_price = 0

    for t in client.list_tickers():
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
# BUY
# =====================
def do_buy(pair, usdt_balance):
    funds = round(usdt_balance * BUY_RATIO * 0.95, 2)

    if funds < MIN_USDT_ORDER:
        raise Exception(f"Dana terlalu kecil: {funds} USDT")

    print(f"Buy {pair} | Funds:{funds} USDT")

    result    = gate_create_order(pair, "buy", funds=funds)
    order_id  = result.get("id")
    buy_price = float(result.get("avg_deal_price") or 0)
    filled    = float(result.get("filled_amount") or 0)

    if (buy_price <= 0 or filled <= 0) and order_id:
        time.sleep(3)
        try:
            detail    = gate_get_order(order_id, pair)
            buy_price = float(detail.get("avg_deal_price") or 0)
            filled    = float(detail.get("filled_amount") or 0)
        except Exception as e:
            print(f"⚠️ get_order error: {e}")

    if buy_price <= 0:
        buy_price = gate_get_price(pair)
    if filled <= 0 and buy_price > 0:
        filled = round(funds / buy_price, 6)

    if buy_price <= 0 or filled <= 0:
        raise Exception(f"Order tidak valid | price={buy_price} filled={filled}")

    return buy_price, filled


# =====================
# SELL
# =====================
def do_sell(client, pair, amount):
    if amount is None or float(amount) <= 0:
        raise Exception(f"Amount tidak valid: {amount}")

    precision  = get_pair_precision(client, pair)
    amount     = round(float(amount), precision)

    print(f"Sell {pair} | Amt:{amount}")

    result     = gate_create_order(pair, "sell", amount=amount)
    order_id   = result.get("id")
    sell_price = float(result.get("avg_deal_price") or 0)

    if sell_price <= 0 and order_id:
        time.sleep(2)
        try:
            detail     = gate_get_order(order_id, pair)
            sell_price = float(detail.get("avg_deal_price") or 0)
        except Exception:
            pass

    if sell_price <= 0:
        sell_price = gate_get_price(pair)

    return sell_price


# =====================
# MAIN
# =====================
def run():
    client = get_client()
    print("=== START ===")

    if not market_ok(client):
        print("❌ Market bearish, skip")
        return

    balance = gate_get_balance()
    print(f"💰 Balance: {balance:.2f} USDT")

    if balance < MIN_USDT_ORDER:
        print("❌ Balance terlalu kecil")
        return

    position = load_position()

    # HOLD MODE
    if position:
        pair      = position["pair"]
        buy_price = float(position.get("buy_price") or 0)
        amount    = position.get("amount")
        peak      = float(position.get("peak_price") or buy_price)

        if amount is None or float(amount) <= 0:
            print(f"❌ Amount invalid: {amount}")
            tg(f"⚠️ <b>POSISI RUSAK</b>\nPair: {pair}\nAuto clear.")
            clear_position()
            return

        amount        = float(amount)
        current_price = gate_get_price(pair)

        if current_price <= 0:
            print(f"❌ Harga {pair} tidak valid")
            return

        peak     = max(peak, current_price)
        tp       = buy_price * (1 + TAKE_PROFIT)
        sl       = buy_price * (1 - STOP_LOSS)
        trailing = peak * (1 - TRAILING_GAP)

        print(f"HOLD {pair} | Now:{current_price:.6f} TP:{tp:.6f} SL:{sl:.6f} Trail:{trailing:.6f}")

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
                f"Profit: <b>+${profit:.4f}</b>"
            )

        elif current_price <= sl or current_price <= trailing:
            sell_price = do_sell(client, pair, amount)
            if sell_price <= 0:
                sell_price = current_price
            loss   = round((sell_price - buy_price) * amount, 4)
            reason = "SL" if current_price <= sl else "TRAILING"
            save_trade(pair, buy_price, sell_price, amount, reason)
            clear_position()
            print(f"❌ {reason}")
            tg(
                f"❌ <b>{reason}</b>\n"
                f"Pair: <b>{pair}</b>\n"
                f"Buy:  ${buy_price:.6f}\n"
                f"Sell: ${sell_price:.6f}\n"
                f"Loss: <b>${loss:.4f}</b>"
            )

        else:
            supabase.table("positions").update(
                {"peak_price": peak}
            ).eq("status", "open").execute()
            print(f"📊 Peak: {peak:.6f}")

        return

    # ENTRY MODE
    pair, price, score = find_best(client)

    if not pair or score < 4:
        print("❌ No signal")
        return

    print(f"🔥 ENTRY {pair} | Score {score}")

    try:
        buy_price, amount = do_buy(pair, balance)

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
            f"Modal: ~${usdt_spent} USDT\n"
            f"Score: {score}"
        )

    except Exception as e:
        print(f"❌ Trade error: {e}")
        tg(f"⚠️ <b>TRADE ERROR</b>\nPair: {pair}\n{e}")


if __name__ == "__main__":
    run()
