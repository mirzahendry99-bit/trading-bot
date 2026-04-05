import os
import gate_api
import pandas as pd
import numpy as np
import urllib.request
import json
from datetime import datetime
from supabase import create_client

print("BOT V12 FINAL RUNNING")

API_KEY        = os.environ.get('GATE_API_KEY')
SECRET_KEY     = os.environ.get('GATE_SECRET_KEY')
SUPABASE_URL   = os.environ.get('SUPABASE_URL')
SUPABASE_KEY   = os.environ.get('SUPABASE_KEY')
TG_TOKEN       = os.environ.get('TELEGRAM_TOKEN')
TG_CHAT_ID     = os.environ.get('TELEGRAM_CHAT_ID')

STOP_LOSS       = 0.025
TRAILING_GAP    = 0.015
TP1_PCT         = 0.05
TP2_PCT         = 0.10
TP3_PCT         = 0.20
MIN_VOLUME      = 700000
MIN_ORDER_USDT  = 5
MAX_ORDER_USDT  = 15
BASE_ORDER_USDT = 10
SAFE_HOURS_UTC  = list(range(0, 18))

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
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        body = json.dumps({"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        req  = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"TG error: {e}")

def setup_client():
    config = gate_api.Configuration(
        host="https://api.gateio.ws/api/v4",
        key=API_KEY, secret=SECRET_KEY
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

def is_trading_hour():
    hour = datetime.utcnow().hour
    if hour not in SAFE_HOURS_UTC:
        print(f"Jam {hour}:00 UTC — outside trading hours, skip entry")
        return False
    return True

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
    print(f"Trade: {result} | Profit: ${profit:.4f}")

def is_valid(pair):
    if not pair.endswith("_USDT"):
        return False
    for b in BLACKLIST:
        if b in pair:
            return False
    return True

def get_candles(client, pair, interval="5m", limit=100):
    candles = client.list_candlesticks(currency_pair=pair, interval=interval, limit=limit)
    closes  = np.array([float(c[2]) for c in candles])
    highs   = np.array([float(c[6]) for c in candles])
    lows    = np.array([float(c[5]) for c in candles])
    volumes = np.array([float(c[4]) for c in candles])
    return closes, highs, lows, volumes

def calc_rsi(closes, period=14):
    s     = pd.Series(closes)
    delta = s.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / (loss + 1e-9)
    return float((100 - 100 / (1 + rs)).iloc[-1])

def calc_ema(closes, period):
    return float(pd.Series(closes).ewm(span=period).mean().iloc[-1])

def calc_macd(closes):
    s      = pd.Series(closes)
    ema12  = s.ewm(span=12).mean()
    ema26  = s.ewm(span=26).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=9).mean()
    return float(macd.iloc[-1]), float(signal.iloc[-1])

def calc_atr(closes, highs, lows, period=14):
    df        = pd.DataFrame({"close": closes, "high": highs, "low": lows})
    df["prev"] = df["close"].shift(1)
    df["tr"]   = np.maximum(df["high"]-df["low"],
                 np.maximum(abs(df["high"]-df["prev"]), abs(df["low"]-df["prev"])))
    return float(df["tr"].rolling(period).mean().iloc[-1])

def calc_bollinger(closes, period=20):
    s     = pd.Series(closes)
    mid   = s.rolling(period).mean()
    std   = s.rolling(period).std()
    upper = mid + 2*std
    lower = mid - 2*std
    pos   = float((closes[-1] - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1] + 1e-9))
    return pos

def get_market_regime(client):
    try:
        closes_4h, _, _, _ = get_candles(client, "BTC_USDT", interval="4h", limit=50)
        closes_1h, _, _, _ = get_candles(client, "BTC_USDT", interval="1h", limit=50)
        closes_5m, _, _, _ = get_candles(client, "BTC_USDT", interval="5m", limit=6)
        ema20_4h   = calc_ema(closes_4h, 20)
        ema50_4h   = calc_ema(closes_4h, 50)
        rsi_4h     = calc_rsi(closes_4h)
        rsi_1h     = calc_rsi(closes_1h)
        btc_change = float(client.list_tickers(currency_pair="BTC_USDT")[0].change_percentage or 0)
        btc_mom    = closes_5m[-1] > closes_5m[0]
        print(f"BTC | EMA20:{ema20_4h:.0f} EMA50:{ema50_4h:.0f} RSI4H:{rsi_4h:.1f} 24h:{btc_change:.2f}% Mom:{'UP' if btc_mom else 'DOWN'}")
        if ema20_4h < ema50_4h and rsi_4h < 45:
            return "BEARISH", btc_mom
        if rsi_4h > 75 and rsi_1h > 70:
            return "OVERBOUGHT", btc_mom
        if ema20_4h > ema50_4h and btc_change > -2:
            return "BULLISH", btc_mom
        return "NEUTRAL", btc_mom
    except Exception as e:
        print(f"Regime error: {e}")
        return "NEUTRAL", True

def check_multiframe(client, pair):
    try:
        closes_1h, _, _, _ = get_candles(client, pair, interval="1h", limit=50)
        rsi_1h    = calc_rsi(closes_1h)
        ema20_1h  = calc_ema(closes_1h, 20)
        ema50_1h  = calc_ema(closes_1h, 50)
        macd_1h, sig_1h = calc_macd(closes_1h)
        closes_4h, _, _, _ = get_candles(client, pair, interval="4h", limit=50)
        ema20_4h  = calc_ema(closes_4h, 20)
        ema50_4h  = calc_ema(closes_4h, 50)
        rsi_4h    = calc_rsi(closes_4h)
        tf_score  = 0
        if rsi_1h < 60:         tf_score += 1
        if ema20_1h > ema50_1h: tf_score += 1
        if macd_1h > sig_1h:    tf_score += 1
        if ema20_4h > ema50_4h: tf_score += 2
        if rsi_4h < 65:         tf_score += 1
        confirmed = tf_score >= 4
        print(f"MTF {pair} | 1H RSI:{rsi_1h:.1f} MACD:{'bull' if macd_1h>sig_1h else 'bear'} | 4H RSI:{rsi_4h:.1f} trend:{'up' if ema20_4h>ema50_4h else 'dn'} | TF:{tf_score}")
        return confirmed, tf_score
    except Exception as e:
        print(f"MTF error {pair}: {e}")
        return False, 0

def calc_position_size(client, pair):
    try:
        closes, highs, lows, _ = get_candles(client, pair)
        atr        = calc_atr(closes, highs, lows)
        volatility = atr / closes[-1]
        if volatility > 0.03:
            size, label = MIN_ORDER_USDT, "High vol"
        elif volatility < 0.01:
            size, label = MAX_ORDER_USDT, "Low vol"
        else:
            size, label = BASE_ORDER_USDT, "Normal"
        print(f"Position size: ${size} ({label}, vol:{volatility:.3f})")
        return size
    except:
        return BASE_ORDER_USDT

def score_pair(client, pair):
    try:
        closes, highs, lows, volumes = get_candles(client, pair)
        if len(closes) < 50:
            return 0, None
        rsi    = calc_rsi(closes)
        ema20  = calc_ema(closes, 20)
        ema50  = calc_ema(closes, 50)
        macd, signal = calc_macd(closes)
        bb_pos = calc_bollinger(closes)
        ticker  = client.list_tickers(currency_pair=pair)[0]
        vol_24h = float(ticker.quote_volume or 0)
        change  = float(ticker.change_percentage or 0)
        price   = float(ticker.last or 0)
        if price <= 0 or vol_24h < MIN_VOLUME:
            return 0, None
        vol_avg   = np.mean(volumes[-20:])
        vol_now   = volumes[-1]
        vol_ratio = vol_now / (vol_avg + 1e-9)
        # Momentum confirmation — 2 candle terakhir naik
        momentum_ok = closes[-1] > closes[-2] > closes[-3]
        score = 0
        if rsi < 35:           score += 2
        elif rsi < 45:         score += 1
        if ema20 > ema50:      score += 2
        if macd > signal:      score += 2
        if vol_ratio > 1.5:    score += 1
        if vol_ratio > 5:      score += 2
        if bb_pos < 0.3:       score += 1
        if vol_24h > MIN_VOLUME: score += 1
        if change > 0:         score += 1
        if momentum_ok:        score += 1
        if change > 6:         score -= 2
        if rsi > 70:           score -= 2
        if not momentum_ok:    score -= 1
        print(f"{pair} RSI:{rsi:.1f} MACD:{'bull' if macd>signal else 'bear'} Vol:{vol_ratio:.1f}x Mom:{'ok' if momentum_ok else 'no'} Score:{score}")
        return score, price
    except:
        return 0, None

def find_best(client, regime):
    candidates = []
    min_score  = 5 if regime == "BULLISH" else 6
    for t in client.list_tickers():
        pair = t.currency_pair
        if not is_valid(pair):
            continue
        score, price = score_pair(client, pair)
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
        raise Exception(f"Amount {amount} < min {min_amount}")
    print(f"Buy {pair} | Amount:{amount} @ {price}")
    order = gate_api.Order(
        currency_pair=pair,
        type="market",
        side="buy",
        amount=str(amount)
    )
    result    = client.create_order(order)
    buy_price = float(result.avg_deal_price or price)
    filled    = float(result.amount or amount)
    print(f"Filled: {filled} @ {buy_price}")
    return buy_price, filled

def do_sell(client, pair, amount):
    currency       = pair.replace("_USDT", "")
    actual_balance = get_coin_balance(client, currency)
    _, precision   = get_min_amount(client, pair)
    if actual_balance <= 0:
        raise Exception(f"Saldo {currency} kosong")
    sell_amount = round(min(float(amount), actual_balance), precision)
    price       = float(client.list_tickers(currency_pair=pair)[0].last)
    print(f"Sell {pair} | Amount:{sell_amount}")
    order = gate_api.Order(
        currency_pair=pair,
        type="market",
        side="sell",
        amount=str(sell_amount)
    )
    result     = client.create_order(order)
    sell_price = float(result.avg_deal_price or price)
    return sell_price

def check_still_bullish(client, pair, regime):
    try:
        closes, _, _, volumes = get_candles(client, pair)
        rsi_now   = calc_rsi(closes)
        ema20_now = calc_ema(closes, 20)
        ema50_now = calc_ema(closes, 50)
        macd_now, sig_now = calc_macd(closes)
        vol_avg   = np.mean(volumes[-20:])
        vol_now   = volumes[-1]
        still_bullish = (
            rsi_now < 70 and
            ema20_now > ema50_now and
            macd_now > sig_now and
            vol_now >= vol_avg * 0.6 and
            regime not in ["BEARISH", "OVERBOUGHT"]
        )
        return still_bullish, rsi_now
    except:
        return False, 0

def should_send_hourly():
    return datetime.utcnow().minute < 32

def run():
    client = setup_client()
    print("=== BOT V12 FINAL START ===")
    print(f"UTC: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}")

    regime, btc_mom = get_market_regime(client)
    print(f"Market: {regime} | BTC Mom: {'UP' if btc_mom else 'DOWN'}")

    balance = get_balance(client)
    print(f"Balance: {balance:.2f} USDT")

    if balance < 1:
        print("Balance terlalu kecil")
        return

    position = load_position()

    # ===== HOLD MODE =====
    if position:
        pair      = position["pair"]
        buy_price = float(position.get("buy_price") or 0)
        amount    = float(position.get("amount") or 0)
        peak      = float(position.get("peak_price") or buy_price)
        tp1_hit   = bool(position.get("tp1_hit", False))
        tp2_hit   = bool(position.get("tp2_hit", False))

        if amount <= 0:
            print("Amount invalid, clear position")
            clear_position()
            return

        currency = pair.replace("_USDT", "")
        coin_bal = get_coin_balance(client, currency)
        if coin_bal <= 0:
            print(f"Saldo {currency} kosong — clear position")
            tg(f"Posisi {pair} di-clear karena saldo kosong")
            clear_position()
            return

        current_price = float(client.list_tickers(currency_pair=pair)[0].last)
        if current_price <= 0:
            return

        peak       = max(peak, current_price)
        sl         = buy_price * (1 - STOP_LOSS)
        trailing   = peak * (1 - TRAILING_GAP)
        tp1        = buy_price * (1 + TP1_PCT)
        tp2        = buy_price * (1 + TP2_PCT)
        tp3        = buy_price * (1 + TP3_PCT)
        profit_pct = round((current_price / buy_price - 1) * 100, 2)

        print(f"HOLD {pair} | Now:{current_price:.6f} PnL:{profit_pct}%")
        print(f"TP1:{tp1:.4f} TP2:{tp2:.4f} TP3:{tp3:.4f} SL:{sl:.4f} Trail:{trailing:.4f}")

        # STOP LOSS / TRAILING
        if current_price <= sl or current_price <= trailing:
            sell_price = do_sell(client, pair, amount)
            loss       = round((sell_price - buy_price) * amount, 4)
            loss_pct   = round((sell_price / buy_price - 1) * 100, 2)
            reason     = "STOP LOSS" if current_price <= sl else "TRAILING STOP"
            save_trade(pair, buy_price, sell_price, amount, reason)
            clear_position()
            print(f"{reason} | Loss: ${loss}")
            tg(
                f"{reason}\n"
                f"Pair: {pair}\n"
                f"Buy: ${buy_price:.6f}\n"
                f"Sell: ${sell_price:.6f}\n"
                f"Loss: ${loss:.4f} ({loss_pct}%)"
            )
            return

        # TP3 — Cek bullish atau jual semua
        if current_price >= tp3:
            still_bullish, rsi_now = check_still_bullish(client, pair, regime)
            if still_bullish:
                supabase.table("positions").update({"peak_price": peak}).eq("status", "open").execute()
                print(f"TP3 zone +{profit_pct}% — masih bullish RSI:{rsi_now:.1f}, hold...")
                tg(f"Sudah +{profit_pct}% — RSI:{rsi_now:.1f} masih bullish, hold terus!")
            else:
                sell_price = do_sell(client, pair, amount)
                profit     = round((sell_price - buy_price) * amount, 4)
                save_trade(pair, buy_price, sell_price, amount, "TP3_FULL")
                clear_position()
                print(f"TP3 FULL EXIT | Profit: ${profit}")
                tg(
                    f"TP3 FULL EXIT\n"
                    f"Pair: {pair}\n"
                    f"Buy: ${buy_price:.6f}\n"
                    f"Sell: ${sell_price:.6f}\n"
                    f"Profit: +${profit:.4f} (+{profit_pct}%)"
                )
            return

        # TP2
        if current_price >= tp2 and not tp2_hit:
            still_bullish, rsi_now = check_still_bullish(client, pair, regime)
            if not still_bullish:
                sell_price = do_sell(client, pair, amount)
                profit     = round((sell_price - buy_price) * amount, 4)
                save_trade(pair, buy_price, sell_price, amount, "TP2_EXIT")
                clear_position()
                print(f"TP2 EXIT | Profit: ${profit}")
                tg(
                    f"TP2 EXIT\n"
                    f"Pair: {pair}\n"
                    f"Buy: ${buy_price:.6f}\n"
                    f"Sell: ${sell_price:.6f}\n"
                    f"Profit: +${profit:.4f} (+{profit_pct}%)"
                )
                return
            else:
                supabase.table("positions").update({"peak_price": peak, "tp2_hit": True}).eq("status", "open").execute()
                print(f"TP2 zone +{profit_pct}% — masih bullish RSI:{rsi_now:.1f}, hold ke TP3...")
                tg(f"TP2 +{profit_pct}% — RSI:{rsi_now:.1f} masih bullish, hold ke TP3 (+20%)!")
                return

        # TP1
        if current_price >= tp1 and not tp1_hit:
            still_bullish, rsi_now = check_still_bullish(client, pair, regime)
            if not still_bullish:
                sell_price = do_sell(client, pair, amount)
                profit     = round((sell_price - buy_price) * amount, 4)
                save_trade(pair, buy_price, sell_price, amount, "TP1_EXIT")
                clear_position()
                print(f"TP1 EXIT | Profit: ${profit}")
                tg(
                    f"TP1 EXIT\n"
                    f"Pair: {pair}\n"
                    f"Buy: ${buy_price:.6f}\n"
                    f"Sell: ${sell_price:.6f}\n"
                    f"Profit: +${profit:.4f} (+{profit_pct}%)"
                )
                return
            else:
                supabase.table("positions").update({"peak_price": peak, "tp1_hit": True}).eq("status", "open").execute()
                print(f"TP1 zone +{profit_pct}% — masih bullish RSI:{rsi_now:.1f}, hold ke TP2...")
                tg(f"TP1 +{profit_pct}% — RSI:{rsi_now:.1f} masih bullish, hold ke TP2 (+10%)!")
                return

        # HOLDING
        supabase.table("positions").update({"peak_price": peak}).eq("status", "open").execute()
        print(f"Holding | Peak:{peak:.6f} | PnL:{profit_pct}%")

        if should_send_hourly():
            emoji = "📈" if profit_pct >= 0 else "📉"
            tg(
                f"{emoji} Update Posisi\n"
                f"Pair: {pair}\n"
                f"Buy: ${buy_price:.6f}\n"
                f"Now: ${current_price:.6f}\n"
                f"PnL: {profit_pct}%\n"
                f"Peak: ${peak:.6f}\n"
                f"TP1: ${round(tp1,6)} (+5%)\n"
                f"TP2: ${round(tp2,6)} (+10%)\n"
                f"TP3: ${round(tp3,6)} (+20%)\n"
                f"SL: ${round(sl,6)} (-2.5%)\n"
                f"Market: {regime}"
            )
        return

    # ===== ENTRY MODE =====
    if regime in ["BEARISH", "OVERBOUGHT"]:
        print(f"Regime {regime} — skip entry")
        return

    if not is_trading_hour():
        return

    if not btc_mom:
        print("BTC momentum DOWN — skip entry")
        return

    candidates = find_best(client, regime)

    if not candidates:
        print("No valid signal")
        return

    for score, price, pair in candidates:
        print(f"Checking {pair} | Score:{score}")
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
                "amount": amount, "peak_price": buy_price,
                "status": "open", "tp1_hit": False, "tp2_hit": False
            })

            tp1     = round(buy_price * (1 + TP1_PCT), 6)
            tp2     = round(buy_price * (1 + TP2_PCT), 6)
            tp3     = round(buy_price * (1 + TP3_PCT), 6)
            sl_p    = round(buy_price * (1 - STOP_LOSS), 6)
            spent   = round(amount * buy_price, 2)

            print(f"BOUGHT {pair} @ {buy_price} | Amount:{amount} | Spent:${spent}")
            tg(
                f"BUY\n"
                f"Pair: {pair}\n"
                f"Price: ${buy_price:.6f}\n"
                f"Amount: {amount}\n"
                f"Modal: ~${spent} USDT\n"
                f"TP1: ${tp1} (+5%)\n"
                f"TP2: ${tp2} (+10%)\n"
                f"TP3: ${tp3} (+20%)\n"
                f"SL: ${sl_p} (-2.5%)\n"
                f"Score: {score} | TF: {tf_score}\n"
                f"Market: {regime}"
            )
            break
        except Exception as e:
            print(f"Skip {pair} - {e}")
            continue

if __name__ == "__main__":
    run()
