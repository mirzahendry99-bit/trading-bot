import os
import time
import gate_api
from gate_api.exceptions import ApiException

# Konfigurasi
API_KEY = os.environ.get('GATE_API_KEY')
SECRET_KEY = os.environ.get('GATE_SECRET_KEY')

# Setting trading
SYMBOL = "BTC_USDT"
GRID_LEVELS = 5
GRID_SPACING = 0.01
ORDER_SIZE = 10

def setup_client():
    config = gate_api.Configuration(
        host="https://api.gateio.ws/api/v4",
        key=API_KEY,
        secret=SECRET_KEY
    )
    return gate_api.SpotApi(gate_api.ApiClient(config))

def get_current_price(client, symbol):
    tickers = client.list_tickers(currency_pair=symbol)
    return float(tickers[0].last)

def place_order(client, symbol, side, price, amount):
    order = gate_api.Order(
        currency_pair=symbol,
        type="limit",
        side=side,
        price=str(price),
        amount=str(amount)
    )
    return client.create_order(order)

def run_grid_bot():
    client = setup_client()
    print("Bot started...")
    base_price = get_current_price(client, SYMBOL)
    print(f"Base price: {base_price}")
    grid_prices = []
    for i in range(-GRID_LEVELS, GRID_LEVELS + 1):
        grid_prices.append(base_price * (1 + i * GRID_SPACING))
    print(f"Grid levels: {[round(p, 2) for p in grid_prices]}")
    while True:
        try:
            current_price = get_current_price(client, SYMBOL)
            print(f"Current price: {current_price}")
            for grid_price in grid_prices:
                if current_price <= grid_price * 0.995:
                    amount = ORDER_SIZE / grid_price
                    place_order(client, SYMBOL, "buy", grid_price, round(amount, 6))
                    print(f"Buy order placed at {grid_price}")
                elif current_price >= grid_price * 1.005:
                    amount = ORDER_SIZE / grid_price
                    place_order(client, SYMBOL, "sell", grid_price, round(amount, 6))
                    print(f"Sell order placed at {grid_price}")
            time.sleep(30)
        except ApiException as e:
            print(f"Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_grid_bot()
