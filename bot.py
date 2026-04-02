import os
import json
import gate_api

API_KEY = os.environ.get('GATE_API_KEY')
SECRET_KEY = os.environ.get('GATE_SECRET_KEY')

ORDER_PERCENT = 0.7   # Pakai 70% saldo
TAKE_PROFIT = 0.03
STOP_LOSS = 0.02
MIN_VOLUME = 300000

POSITION_FILE = "position.json"

def setup_client():
    config = gate_api.Configuration(
        host="https://api.gateio.ws/api/v4",
        key=API_KEY,
        secret=SECRET_KEY
    )
    return gate_api.SpotApi(gate_api.ApiClient(config))

def get_balance(client):
    accounts = client.list_spot_accounts()
    for acc in accounts:
        if acc.currency == "USDT":
            return float(acc.available)
    return 0

def save_position(data):
    with open(POSITION_FILE, "w") as f:
        json.dump(data, f)

def load_position():
    if not os.path.exists(POSITION_FILE):
        return None
    with open(POSITION_FILE, "r") as f:
        return json.load(f)

def clear_position():
    if os.path.exists(POSITION_FILE):
        os.remove(POSITION_FILE)

def is_valid_pair(pair):
    bad = ["3S", "3L", "5S", "5L"]
    return not any(x in pair for x in bad)

def get_candidate(client):
    tickers = client.list_tickers()

    for t in tickers:
        try:
            pair = t.currency_pair

            if not pair.endswith("_USDT"):
                continue

            if not is_valid_pair(pair):
                continue

            volume = float(t.quote_volume or 0)
            change = float(t.change_percentage or 0)
            price = float(t.last or 0)

            if volume > MIN_VOLUME and -2 < change < 4:
                return pair, price

        except:
            continue

    return None, None

def place_market_buy(client, pair, usdt):
    price = float(client.list_tickers(currency_pair=pair)[0].last)
    amount = round((usdt * 0.97) / price, 6)

    order = gate_api.Order(
        currency_pair=pair,
        type="market",
        side="buy",
        amount=str(amount)
    )
    return client.create_order(order), price, amount

def place_market_sell(client, pair, amount):
    order = gate_api.Order(
        currency_pair=pair,
        type="market",
        side="sell",
        amount=str(amount)
    )
    return client.create_order(order)

def run_bot():
    client = setup_client()
    print("=== FINAL BOT STARTED ===")

    balance = get_balance(client)
    print(f"Saldo USDT: {balance}")

    if balance < 5:
        print("Saldo terlalu kecil, skip")
        return

    position = load_position()

    # ======================
    # CHECK POSITION
    # ======================
    if position:
        pair = position["pair"]
        buy_price = position["buy_price"]
        amount = position["amount"]

        current_price = float(client.list_tickers(currency_pair=pair)[0].last)

        print(f"Holding {pair}")
        print(f"Buy: {buy_price} | Now: {current_price}")

        tp = buy_price * (1 + TAKE_PROFIT)
        sl = buy_price * (1 - STOP_LOSS)

        if current_price >= tp:
            place_market_sell(client, pair, amount)
            print("TAKE PROFIT 🚀")
            clear_position()

        elif current_price <= sl:
            place_market_sell(client, pair, amount)
            print("STOP LOSS ❌")
            clear_position()

        else:
            print("Hold posisi")

        return

    # ======================
    # ENTRY
    # ======================
    pair, price = get_candidate(client)

    if not pair:
        print("No entry signal")
        return

    usdt = balance * ORDER_PERCENT

    print(f"Entry: {pair} @ {price} | Size: {usdt}")

    try:
        order, buy_price, amount = place_market_buy(client, pair, usdt)

        save_position({
            "pair": pair,
            "buy_price": buy_price,
            "amount": amount
        })

        print(f"BOUGHT {pair} @ {buy_price}")

    except Exception as e:
        print(f"Trade error: {e}")

if __name__ == "__main__":
    run_bot()
