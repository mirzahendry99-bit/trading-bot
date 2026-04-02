import os
import time
import gate_api
from gate_api.exceptions import ApiException

API_KEY = os.environ.get('GATE_API_KEY')
SECRET_KEY = os.environ.get('GATE_SECRET_KEY')

# Setting
PROFIT_TARGET = 0.02      # Target profit 2%
STOP_LOSS = 0.015         # Stop loss 1.5%
ORDER_USDT = 10           # Modal per trade (USDT)
TOP_COINS = 30            # Scan 30 koin teratas
MIN_VOLUME = 1000000      # Minimum volume 24 jam

def setup_client():
    config = gate_api.Configuration(
        host="https://api.gateio.ws/api/v4",
        key=API_KEY,
        secret=SECRET_KEY
    )
    return gate_api.SpotApi(gate_api.ApiClient(config))

def get_hot_coins(client):
    """Scan dan filter koin yang potensial"""
    tickers = client.list_tickers()
    candidates = []
    
    for ticker in tickers:
        try:
            # Filter hanya pair USDT
            if not ticker.currency_pair.endswith('_USDT'):
                continue
            
            volume = float(ticker.quote_volume or 0)
            change = float(ticker.change_percentage or 0)
            price = float(ticker.last or 0)
            
            # Filter: volume tinggi + harga naik 1-8%
            if volume > MIN_VOLUME and 1 < change < 8 and price > 0:
                candidates.append({
                    'pair': ticker.currency_pair,
                    'price': price,
                    'change': change,
                    'volume': volume
                })
        except:
            continue
    
    # Sort by volume tertinggi
    candidates.sort(key=lambda x: x['volume'], reverse=True)
    return candidates[:5]  # Ambil 5 terbaik

def place_buy_order(client, pair, price, usdt_amount):
    amount = round(usdt_amount / price, 6)
    order = gate_api.Order(
        currency_pair=pair,
        type="limit",
        side="buy",
        price=str(price),
        amount=str(amount),
        time_in_force="gtc"
    )
    return client.create_order(order)

def place_sell_order(client, pair, price, amount):
    order = gate_api.Order(
        currency_pair=pair,
        type="limit",
        side="sell",
        price=str(price),
        amount=str(amount),
        time_in_force="gtc"
    )
    return client.create_order(order)

def run_smart_bot():
    client = setup_client()
    print("Smart Scanning Bot started...")
    print(f"Scanning top coins...")
    
    hot_coins = get_hot_coins(client)
    
    if not hot_coins:
        print("Tidak ada koin yang memenuhi kriteria saat ini.")
        return
    
    print(f"Ditemukan {len(hot_coins)} koin potensial:")
    for coin in hot_coins:
        print(f"  {coin['pair']} | Harga: {coin['price']} | Naik: {coin['change']}% | Volume: {coin['volume']:.0f}")
    
    # Trade koin terbaik
    best = hot_coins[0]
    pair = best['pair']
    buy_price = best['price']
    
    print(f"\nMemilih: {pair} untuk di-trade")
    
    try:
        # Beli
        buy_order = place_buy_order(client, pair, buy_price, ORDER_USDT)
        amount_bought = float(buy_order.amount)
        print(f"Beli {pair}: {amount_bought} @ {buy_price}")
        
        # Monitor harga
        target_price = buy_price * (1 + PROFIT_TARGET)
        stop_price = buy_price * (1 - STOP_LOSS)
        print(f"Target jual: {target_price:.4f}")
        print(f"Stop loss: {stop_price:.4f}")
        
        while True:
            time.sleep(30)
            tickers = client.list_tickers(currency_pair=pair)
            current_price = float(tickers[0].last)
            print(f"Harga {pair} sekarang: {current_price}")
            
            if current_price >= target_price:
                place_sell_order(client, pair, amount_bought)
                profit = (current_price - buy_price) * amount_bought
                print(f"PROFIT! Jual {pair} @ {current_price} | Untung: ${profit:.2f}")
                break
            
            elif current_price <= stop_price:
                place_sell_order(client, pair, amount_bought)
                loss = (buy_price - current_price) * amount_bought
                print(f"STOP LOSS! Jual {pair} @ {current_price} | Rugi: ${loss:.2f}")
                break
                
    except ApiException as e:
        print(f"Error trade: {e}")

if __name__ == "__main__":
    run_smart_bot()
