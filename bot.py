import os
import gate_api
import pandas as pd
import numpy as np
import urllib.request
import json
from datetime import datetime
from supabase import create_client

print("BOT V11 RUNNING")

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
BASE_ORDER_USDT = 10
MIN_ORDER_USDT = 5
MAX_ORDER_USDT = 15

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

def get_coin_balance(client, currency):
    try:
        for acc in client.list_spot_accounts():
            if acc.currency == currency:
                return float(acc.available)
    except:
        pass
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

def get_candles(client, pair, interval="5m", limit=100):
    candles = client.list_candlesticks(currency_pair=pair, interval=interval, limit=limit)
    closes = np.array([float(c[2]) for c in candles])
    highs = np.array([float(c[6]) for c in candles])
    lows = np.array([float(c[5]) for c in candles])
    volumes = np.array([float(c[4]) for c in candles])
    return closes, highs, lows, volumes

def calc_rsi(closes, period=14):
    s = pd.Series(closes)
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / (loss + 1e-9)
    return float((100 - 100 / (1 + rs)).iloc[-1])

def calc_ema(closes, period):
    return float(pd.Series(closes).ewm(span=period).mean().iloc[-1])

def calc_macd(closes):
    s = pd.Series(closes)
    ema12 = s.ewm(span=12).mean()
    ema26 = s.ewm(span=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()
    return float(macd.iloc[-1]), float(signal.iloc[-1])

def calc_atr(closes, highs, lows, period=14):
    df = pd.DataFrame({"close": closes, "high": highs, "low": lows})
    df["prev_close"] = df["close"].shift(1)
    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(abs(df["high"] - df["prev_close"]), abs(df["low"] - df["prev_close"]))
    )
    return float(df["tr"].rolling(period).mean().iloc[-1])

def calc_bollinger(closes, period=20):
    s = pd.Series(closes)
    mid = s.rolling(period).mean()
    std = s.rolling(period).std()
    upper = mid + 2 * std
    lower = mid - 2 * std
    price = closes[-1]
    bb_pos = float((price - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1] + 1e-9))
    return bb_pos

def get_market_regime(client):
    try:
        closes_4h, _, _, _ = get_candles(client, "BTC_USDT", interval="4h", limit=50)
        closes_1h, _, _, _ = get_candles(client, "BTC_USDT", interval="1h", limit=50)
        ema20_4h = calc_ema(closes_4h, 20)
        ema50_4h = calc_ema(closes_4h, 50)
        rsi_4h = calc_rsi(closes_4h)
        rsi_1h = calc_rsi(closes_1h)
        btc_change = float(client.list_tickers(currency_pair="BTC_USDT")[0].change_percentage or 0)
        print(f"Market | EMA20:{ema20_4h:.0f} EMA50:{ema50_4h:.0f} RSI4H:{rsi_4h:.1f} RSI1H:{rsi_1h:.1f} BTC:{btc_change:.2f}%")
        if ema20_4h < ema50_4h and rsi_4h < 45:
            return "BEARISH"
        if rsi_4h > 75 and rsi_1h > 70:
            return "OVERBOUGHT"
        if abs(btc_change) < 0.5 and 45 < rsi_4h < 60:
            return "SIDEWAYS"
        if ema20_4h > ema50_4h and btc_change > -2:
            return "BULLISH"
        return "NEUTRAL"
    except Exception as e:
        print(f"Market regime error: {e}")
        return "NEUTRAL"

def calc_position_size(client, pair):
    try:
        closes, highs, lows, _ = get_candles(client, pair)
        atr = calc_atr(closes, highs, lows)
        price = closes[-1]
        volatility = atr / price
        if volatility > 0.03:
            size = MIN_ORDER_USDT
            label = "High vol"
        elif volatility < 0.01:
            size = MAX_ORDER_USDT
            label = "Low vol"
        else:
            size = BASE_ORDER_USDT
            label = "Normal vol"
        print(f"Position size: ${size} ({label}, vol:{volatility:.3f})")
        return size
    except:
        return BASE_ORDER_USDT

def check_multiframe(client, pair):
    try:
        closes_1h, _, _, _ = get_candles(client, pair, interval="1h", limit=50)
        rsi_1h = calc_rsi(closes_1h)
        ema20_1h = calc_ema(closes_1h, 20)
        ema50_1h = calc_ema(closes_1h, 50)
        macd_1h, signal_1h = calc_macd(closes_1h)
        closes_4h, _, _, _ = get_candles(client, pair, interval="4h", limit=50)
        ema20_4h = calc_ema(closes_4h, 20)
        ema50_4h = calc_ema(closes_4h, 50)
        rsi_4h = calc_rsi(closes_4h)
        tf_score = 0
        if rsi_1h < 60: tf_score += 1
        if ema20_1h > ema50_1h: tf_score += 1
        if macd_1h > signal_1h: tf_score += 1
        if ema20_4h > ema50_4h: tf_score += 2
        if rsi_4h < 65: tf_score += 1
        confirmed = tf_score >= 4
        print(f"MTF {pair} | 1H RSI:{rsi_1h:.1f} | 4H RSI:{rsi_4h:.1f} trend:{'up' if ema20_4h>ema50_4h else 'dn'} | TF:{tf_score}")
        return confirmed, tf_score
    except Exception as e:
        print(f"MTF error {pair}: {e}")
        return False, 0

def score_pair_5m(client, pair):
    try:
        closes, highs, lows, volumes = get_candles(client, pair)
        if len(closes) < 50:
            return 0, None
        rsi = calc_rsi(closes)
        ema20 = calc_ema(closes, 20)
        ema50 = calc_ema(closes, 50)
        macd, signal = calc_macd(closes)
        bb_pos = calc_bollinger(closes)
        ticker = client.list_tickers(currency_pair=pair)[0]
        vol_24h = float(ticker.quote_volume or 0)
        change = float(ticker.change_percentage or 0)
        price = float(ticker.last or 0)
        if price <= 0 or vol_24h < MIN_VOLUME:
            return 0, None
        vol_avg = np.mean(volumes[-20:])
        vol_now = volumes[-1]
        vol_ratio = vol_now / (vol_avg + 1e-9)
        score = 0
        if rsi < 35: score += 2
        elif rsi < 45: score += 1
        if ema20 > ema50: score += 2
        if macd > signal: score += 2
        if vol_ratio > 1.5: score += 1
        if vol_ratio > 5: score += 2
        if bb_pos < 0.3: score += 1
        if vol_24h > MIN_VOLUME: score += 1
        if change > 0: score += 1
        if change > 6: score -= 2
        if rsi > 70: score -= 2
        print(f"{pair} RSI:{rsi:.1f} MACD:{'bull' if macd>signal else 'bear'} Vol:{vol_ratio:.1f}x Score:{score}")
        return score, price
    except:
        return 0, None

def find_best(client, regime):
    candidates = []
    min_score = 5 if regime == "BULLISH" else 6
    for t in client.list_tickers():
        pair = t.currency_pair
        if not is_valid(pair):
            continue
        score, price = score_pair_5m(client, pair)
        if price and score >= min_score:
            candidates.append((score, price, pair))
    candidates.sort(reverse=True)
    print(f"Found {len(candidates)} candidates (min score:{min_score})")
    return candidates

def get_min_amount(client, pair):
    try:
        pairs = client.list_currency_pairs()
        for p in pairs:
            if p.id == pair:
                return float(p.min_base_amount or 0), int(p.amount_precision or 4)
    except:
        pass
    return 0, 4

def do_buy(client, pair, order_usdt):
    price = float(client.list_tickers(currency_pair=pair)[0].last)
    if price <= 0:
        raise Exception(f"Harga {pair} tidak valid")
    min_amount, precision = get_min_amount(client, pair)
    amount = round(order_usdt / price, precision)
    if min_amount > 0 and amount < min_amount:
        raise Exception(f"Amount {amount} < minimum {min_amount}")
    print(f"Buy {pair} | Amount: {amount} @ {price}")
    order = gate_api.Order(
        currency_pair=pair,
        type="market",
        side="buy",
        amount=str(amount),
        time_in_force="ioc"
    )
    result = client.create_order(order)
    buy_price = float(result.avg_deal_price or price)
    filled = float(result.amount or amount)
    print(f"Filled: {filled} @ {buy_price}")
    return buy_price, filled

def do_sell(client, pair, amount):
    currency = pair.replace("_USDT", "")
    actual_balance = get_coin_balance(client, currency)
    _, precision = get_min_amount(client, pair)

    if actual_balance <= 0:
        raise Exception(f"Tidak ada saldo {currency} untuk dijual")

    sell_amount = round(min(float(amount), actual_balance), precision)
    price = float(client.list_tickers(currency_pair=pair)[0].last)
    print(f"Sell {pair} | Amount: {sell_amount} (balance: {actual_balance})")

    order = gate_api.Order(
        currency_pair=pair,
        type="market",
        side="sell",
        amount=str(sell_amount)
    )
    result = client.create_order(order)
    sell_price = float(result.avg_deal_price or price)
    return sell_price

def check_still_bullish(client, pair):
    try:
        closes, _, _, volumes = get_candles(client, pair)
        rsi_now = calc_rsi(closes)
        ema20_now = calc_ema(closes, 20)
        ema50_now = calc_ema(closes, 50)
        macd_now, signal_now = calc_macd(closes)
        vol_avg = np.mean(volumes[-20:])
        vol_now = volumes[-1]
        still_bullish = (
            rsi_now < 68 and
            ema20_now > ema50_now and
            macd_now > signal_now and
            vol_now >= vol_avg * 0.7
        )
        return still_bullish, rsi_now
    except:
        return False, 0

def should_send_hourly():
    return datetime.utcnow().minute < 32

def run():
    client = setup_client()
    print("=== BOT V11 START ===")

    regime = get_market_regime(client)
    print(f"Market Regime: {regime}")

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
            print("Amount invalid, clear position")
            clear_position()
            return

        currency = pair.replace("_USDT", "")
        coin_balance = get_coin_balance(client, currency)
        if coin_balance <= 0:
            print(f"Saldo {currency} kosong — clear position")
            tg(f"Posisi {pair} di-clear karena saldo kosong")
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
        profit_pct = round((current_price / buy_price - 1) * 100, 2)

        print(f"HOLD {pair} | Now:{current_price:.6f} TP:{tp:.6f} SL:{sl:.6f} PnL:{profit_pct}%")

        if current_price >= tp:
            still_bullish, rsi_now = check_still_bullish(client, pair)
            if still_bullish and regime != "OVERBOUGHT":
                supabase.table("positions").update({"peak_price": peak}).eq("status", "open").execute()
                print(f"Profit {profit_pct}% masih bullish RSI:{rsi_now:.1f}, hold...")
                tg(f"Profit {profit_pct}% — masih bullish!\nPair: {pair}\nRSI: {rsi_now:.1f}\nHold kejar lebih tinggi...")
            else:
                sell_price = do_sell(client, pair, amount)
                profit = round((sell_price - buy_price) * amount, 4)
                save_trade(pair, buy_price, sell_price, amount, "TAKE_PROFIT")
                clear_position()
                print(f"TAKE PROFIT | Profit: ${profit}")
                tg(f"TAKE PROFIT\nPair: {pair}\nBuy: ${buy_price:.6f}\nSell: ${sell_price:.6f}\nProfit: +${profit:.4f} (+{profit_pct}%)")

        elif current_price <= sl or current_price <= trailing:
            sell_price = do_sell(client, pair, amount)
            loss = round((sell_price - buy_price) * amount, 4)
            loss_pct = round((sell_price / buy_price - 1) * 100, 2)
            reason = "STOP LOSS" if current_price <= sl else "TRAILING STOP"
            save_trade(pair, buy_price, sell_price, amount, reason)
            clear_position()
            print(f"{reason} | Loss: ${loss}")
            tg(f"{reason}\nPair: {pair}\nBuy: ${buy_price:.6f}\nSell: ${sell_price:.6f}\nLoss: ${loss:.4f} ({loss_pct}%)")

        else:
            supabase.table("positions").update({"peak_price": peak}).eq("status", "open").execute()
            print(f"Holding | Peak: {peak:.6f} | PnL: {profit_pct}%")
            if should_send_hourly():
                emoji = "📈" if profit_pct >= 0 else "📉"
                tg(
                    f"{emoji} Update Posisi\n"
                    f"Pair: {pair}\n"
                    f"Buy: ${buy_price:.6f}\n"
                    f"Now: ${current_price:.6f}\n"
                    f"PnL: {profit_pct}%\n"
                    f"Peak: ${peak:.6f}\n"
                    f"TP1: ${round(buy_price*1.05,6)} (+5%)\n"
                    f"TP2: ${round(buy_price*1.10,6)} (+10%)\n"
                    f"TP3: ${round(buy_price*1.20,6)} (+20%)\n"
                    f"SL: ${round(buy_price*(1-STOP_LOSS),6)}\n"
                    f"Market: {regime}"
                )
        return

    if regime in ["BEARISH", "OVERBOUGHT"]:
        print(f"Regime {regime} — skip entry")
        tg(f"Market {regime} — skip entry, jaga modal")
        return

    candidates = find_best(client, regime)

    if not candidates:
        print("No valid signal")
        return

    for score, price, pair in candidates:
        print(f"Checking {pair} | Score: {score}")
        mtf_ok, tf_score = check_multiframe(client, pair)
        if not mtf_ok:
            print(f"Skip {pair} — MTF tidak konfirmasi (TF:{tf_score})")
            continue
        order_usdt = calc_position_size(client, pair)
        if balance < order_usdt:
            order_usdt = max(MIN_ORDER_USDT, round(balance * 0.9, 2))
        print(f"ENTRY {pair} | Score:{score} | TF:{tf_score} | Size:${order_usdt}")
        try:
            buy_price, amount = do_buy(client, pair, order_usdt)
            if buy_price <= 0 or amount <= 0:
                print(f"Skip {pair} - order invalid")
                continue
            save_position({
                "pair": pair, "buy_price": buy_price,
                "amount": amount, "peak_price": buy_price, "status": "open"
            })
            tp1 = round(buy_price * 1.05, 6)
            tp2 = round(buy_price * 1.10, 6)
            tp3 = round(buy_price * 1.20, 6)
            sl_price = round(buy_price * (1 - STOP_LOSS), 6)
            usdt_spent = round(amount * buy_price, 2)
            print(f"BOUGHT {pair} @ {buy_price} | Amount:{amount} | Spent:${usdt_spent}")
            tg(
                f"BUY\n"
                f"Pair: {pair}\n"
                f"Price: ${buy_price:.6f}\n"
                f"Amount: {amount}\n"
                f"Modal: ~${usdt_spent} USDT\n"
                f"TP1: ${tp1} (+5%)\n"
                f"TP2: ${tp2} (+10%)\n"
                f"TP3: ${tp3} (+20%)\n"
                f"SL: ${sl_price} (-{int(STOP_LOSS*100)}%)\n"
                f"Score: {score} | TF: {tf_score}\n"
                f"Market: {regime}"
            )
            break
        except Exception as e:
            print(f"Skip {pair} - {e}")
            continue

if __name__ == "__main__":
    run()
