import os
import json
import gate_api
from gate_api.exceptions import ApiException

API_KEY = os.environ.get('GATE_API_KEY')
SECRET_KEY = os.environ.get('GATE_SECRET_KEY')

# CONFIG
ORDER_USDT = 10
TAKE_PROFIT = 0.03     # 3%
STOP_LOSS = 0.02       # 2%
MIN_VOLUME = 300000

POSITION_FILE = "position.json"

def setup_client():
    config = gate_api.Configuration(
        host="https://api.gateio.ws/api/v4",
        key=API_KEY,
        secret=SECRET_KEY
    )
    return gate_api.SpotApi(gate_api.ApiClient(config))

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

def get_candidate(client):
    tickers = client.list_tickers()
    
    for t in tickers:
        try:
            if not t.currency_pair.endswith("_USDT"):
                continue

            volume = float(t.quote_volume or 0)
            change = float(t.change_percentage or 0)
            price = float(t.last or 0)

            # FILTER BARU (lebih realistis)
            if volume > MIN_VOLUME and -3 < change < 3:
                return t.currency_pair, price

        except:
            continue

    return None, None

def place_market_buy(client, pair, usdt):
    price = float(client.list_tickers(currency_pair=pair)[0].last)
    amount = round(usdt / price, 6)

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
    print("=== BOT V2 STARTED ===")

    position = load_position()

    # ======================
    # CHECK EXISTING POSITION
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
            print("TAKE PROFIT HIT 🚀")
            clear_position()

        elif current_price <= sl:
            place_market_sell(client, pair, amount)
            print("STOP LOSS HIT ❌")
            clear_position()

        else:
            print("No exit signal")

        return

    # ======================
    # ENTRY LOGIC
    # ======================
    pair, price = get_candidate(client)

    if not pair:
        print("No trade opportunity")
        return

    print(f"Entry candidate: {pair} @ {price}")

    try:
        order, buy_price, amount = place_market_buy(client, pair, ORDER_USDT)

        save_position({
            "pair": pair,
            "buy_price": buy_price,
            "amount": amount
        })

        print(f"BOUGHT {pair} @ {buy_price}")

    except ApiException as e:
        print(f"Trade error: {e}")

if __name__ == "__main__":
    run_bot()
