import os
import json
import gate_api
import pandas as pd
import numpy as np

API_KEY = os.environ.get('GATE_API_KEY')
SECRET_KEY = os.environ.get('GATE_SECRET_KEY')

# ===== CONFIG =====
BASE_RISK = 0.02            # risk per trade (2%)
MAX_ALLOC = 0.7             # max % balance used
TAKE_PROFIT = 0.05          # 5%
STOP_LOSS = 0.025           # 2.5%
TRAILING_GAP = 0.02         # 2% trailing
MIN_VOLUME = 700000

POSITION_FILE = "position.json"

# ===== CLIENT =====
def client():
    cfg = gate_api.Configuration(
        host="https://api.gateio.ws/api/v4",
        key=API_KEY,
        secret=SECRET_KEY
    )
    return gate_api.SpotApi(gate_api.ApiClient(cfg))

# ===== STATE =====
def save_pos(p): open(POSITION_FILE, "w").write(json.dumps(p))
def load_pos(): return json.load(open(POSITION_FILE)) if os.path.exists(POSITION_FILE) else None
def clear_pos(): 
    if os.path.exists(POSITION_FILE): os.remove(POSITION_FILE)

# ===== HELPERS =====
def usdt_balance(c):
    for a in c.list_spot_accounts():
        if a.currency == "USDT": return float(a.available)
    return 0

def valid_pair(p):
    return p.endswith("_USDT") and not any(x in p for x in ["3S","3L","5S","5L"])

def candles(c, pair, interval="5m", limit=100):
    cs = c.list_candlesticks(currency_pair=pair, interval=interval, limit=limit)
    closes = np.array([float(x[2]) for x in cs])
    vols   = np.array([float(x[5]) for x in cs])
    return closes, vols

def rsi(arr, n=14):
    s = pd.Series(arr)
    d = s.diff()
    gain = d.clip(lower=0).rolling(n).mean()
    loss = (-d.clip(upper=0)).rolling(n).mean()
    rs = gain / (loss + 1e-9)
    return float((100 - 100/(1+rs)).iloc[-1])

def ema(arr, n):
    return float(pd.Series(arr).ewm(span=n, adjust=False).mean().iloc[-1])

def pct_change(arr):
    return float((arr[-1] - arr[-2]) / arr[-2] * 100)

# ===== MARKET REGIME =====
def market_ok(c):
    try:
        btc = c.list_tickers(currency_pair="BTC_USDT")[0]
        change = float(btc.change_percentage or 0)
        closes, _ = candles(c, "BTC_USDT", "5m", 50)
        trend = ema(closes, 20) > ema(closes, 50)
        print(f"BTC change: {change:.2f}% | trend_up: {trend}")
        return (change > -2) and trend
    except:
        return False

# ===== SCORING ENGINE =====
def score_pair(c, pair):
    try:
        closes, vols = candles(c, pair, "5m", 80)
        last = closes[-1]

        r = rsi(closes)
        e20 = ema(closes, 20)
        e50 = ema(closes, 50)
        mom = pct_change(closes)
        vol_spike = vols[-1] > (np.mean(vols[-20:]) * 1.5)

        ticker = c.list_tickers(currency_pair=pair)[0]
        vol24 = float(ticker.quote_volume or 0)
        chg24 = float(ticker.change_percentage or 0)

        score = 0

        # Mean-reversion + early reversal
        if r < 35: score += 2
        # Trend alignment
        if e20 > e50: score += 2
        # Micro momentum confirm
        if mom > 0: score += 1
        # Volume validation
        if vol_spike: score += 1
        if vol24 > MIN_VOLUME: score += 1
        # Avoid chasing pumps
        if chg24 > 6: score -= 2

        print(f"{pair} | RSI:{r:.1f} EMA20>{e50:.4f}? {e20>e50} mom:{mom:.2f}% vol_spike:{vol_spike} score:{score}")
        return score, last
    except:
        return -999, None

def best_candidate(c):
    best = (None, 0, 0.0)
    for t in c.list_tickers():
        p = t.currency_pair
        if not valid_pair(p): continue
        s, price = score_pair(c, p)
        if s > best[1]:
            best = (p, s, price)
    return best  # (pair, score, price)

# ===== EXECUTION =====
def mkt_buy(c, pair, usdt):
    price = float(c.list_tickers(currency_pair=pair)[0].last)
    amount = round((usdt * 0.97) / price, 6)
    o = gate_api.Order(currency_pair=pair, type="market", side="buy", amount=str(amount))
    return c.create_order(o), price, amount

def mkt_sell(c, pair, amount):
    o = gate_api.Order(currency_pair=pair, type="market", side="sell", amount=str(amount))
    return c.create_order(o)

# ===== POSITION MGMT =====
def manage_position(c, pos):
    pair = pos["pair"]
    buy = pos["buy_price"]
    amt = pos["amount"]
    peak = pos.get("peak_price", buy)

    cur = float(c.list_tickers(currency_pair=pair)[0].last)
    tp = buy * (1 + TAKE_PROFIT)
    sl = buy * (1 - STOP_LOSS)

    # update peak for trailing
    peak = max(peak, cur)
    trail = peak * (1 - TRAILING_GAP)

    print(f"HOLD {pair} | buy:{buy} now:{cur} tp:{tp:.4f} sl:{sl:.4f} trail:{trail:.4f}")

    if cur >= tp:
        mkt_sell(c, pair, amt)
        print("TAKE PROFIT 🚀")
        clear_pos()
        return

    if cur <= sl or cur <= trail:
        mkt_sell(c, pair, amt)
        print("STOP / TRAILING HIT ❌")
        clear_pos()
        return

    # keep position (update peak)
    pos["peak_price"] = peak
    save_pos(pos)

# ===== SIZING =====
def size_from_score(balance, score):
    # simple adaptive sizing (proxy Kelly)
    # score 3 -> 40%, 4 -> 55%, >=5 -> 70%
    if score >= 5: alloc = 0.7
    elif score == 4: alloc = 0.55
    else: alloc = 0.4
    alloc = min(alloc, MAX_ALLOC)
    return balance * alloc

# ===== MAIN =====
def run():
    c = client()
    print("=== BOT V4 ENGINE START ===")

    if not market_ok(c):
        print("Market risk-off, skip")
        return

    bal = usdt_balance(c)
    print(f"Balance: {bal}")
    if bal < 5:
        print("Balance too small")
        return

    pos = load_pos()

    # ---- MANAGE EXISTING ----
    if pos:
        manage_position(c, pos)
        return

    # ---- FIND ENTRY ----
    pair, score, price = best_candidate(c)

    if not pair or score < 3:
        print("No high-quality setup")
        return

    usdt = size_from_score(bal, score)
    print(f"ENTRY {pair} | score:{score} | alloc:{usdt:.2f}")

    try:
        _, buy_price, amt = mkt_buy(c, pair, usdt)
        save_pos({
            "pair": pair,
            "buy_price": buy_price,
            "amount": amt,
            "peak_price": buy_price
        })
        print(f"BOUGHT {pair} @ {buy_price}")
    except Exception as e:
        print(f"Trade error: {e}")

if __name__ == "__main__":
    run()
