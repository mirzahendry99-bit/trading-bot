"""
╔══════════════════════════════════════════════════════════════════╗
║           ALTCOIN TRADING BOT v6.0 — Signal-Driven              ║
║                                                                  ║
║  Arsitektur v6:                                                  ║
║  - Entry   : Driven by Signal Bot Lite (signals_v2 Supabase)    ║
║              Bot ini tidak punya scoring engine sendiri.         ║
║              Keputusan entry 100% dari signal bot.               ║
║  - Pair    : Multi-pair altcoin (semua pair dari signals_v2)     ║
║  - Order   : Market order IOC (BUY & SELL)                      ║
║  - Exit    : TP1 (partial 50%) → TP2 (full) → SL               ║
║              SL geser ke breakeven setelah TP1 hit              ║
║  - Risk    : Dynamic risk % dari equity curve (ECC dari v4)     ║
║  - Safety  : Max daily loss, cooldown, crash guard (BTC)        ║
║  - Recover : Auto-recover orphan position per pair              ║
║                                                                  ║
║  Flow per run (dipanggil GitHub Actions setiap 30 menit):       ║
║  1. Cek semua open positions → evaluasi SL/TP                   ║
║  2. Cek signal baru di signals_v2 → eksekusi entry              ║
║  3. Update result di signals_v2 setelah close                   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import json
import time
import math
import urllib.request
import gate_api
from datetime import datetime, timedelta, timezone
from supabase import create_client

# ════════════════════════════════════════════════════════
#  ENV & SUPABASE
# ════════════════════════════════════════════════════════

API_KEY      = os.environ["GATE_API_KEY"]
SECRET_KEY   = os.environ["GATE_SECRET_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TG_TOKEN     = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID   = os.environ["CHAT_ID"]

# ── Trading Bot Supabase (positions, trade_history, bot_state) ──
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Signal Bot Supabase (signals_v2) — instance terpisah ────────
# Hanya digunakan untuk baca sinyal dan write-back hasil trade.
# Set SIGNAL_SUPABASE_URL + SIGNAL_SUPABASE_KEY di GitHub Secrets.
SIGNAL_SUPABASE_URL = os.environ["SIGNAL_SUPABASE_URL"]
SIGNAL_SUPABASE_KEY = os.environ["SIGNAL_SUPABASE_KEY"]
supabase_signal     = create_client(SIGNAL_SUPABASE_URL, SIGNAL_SUPABASE_KEY)

BOT_VERSION = "6.0.0"
WIB         = timezone(timedelta(hours=7))

# ════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════

# ── Risk management ──────────────────────────────────────
# [EQUITY $17] Parameter disesuaikan untuk modal kecil.
# Dengan $17, 1% risk = $0.17 → order size terlalu kecil dari MIN_ORDER $5.
# Dinaikkan ke 5% agar order size $5-10 yang realistis.
# MAX_ORDER_USDT dikap $10 = maks 58% equity — jangan lebih dari ini.
INITIAL_EQUITY_USDT = float(os.environ.get("INITIAL_EQUITY_USDT") or "17")
RISK_PCT_DEFAULT        = 0.05    # 5% equity per trade
RISK_PCT_FLOOR          = 0.03    # 3% saat drawdown
RISK_PCT_CAP            = 0.08    # 8% saat win streak bagus
EQUITY_LOOKBACK         = 5       # evaluasi 5 trade terakhir
MIN_ORDER_USDT          = 10.0    # minimum order Gate.io (actual minimum)
MAX_ORDER_USDT          = 15.0    # cap $15 per trade — sisakan buffer dari $17

# ── TP1 partial exit ─────────────────────────────────────
TP1_SELL_RATIO          = 0.50    # jual 50% saat TP1 hit

# ── Safety guards ─────────────────────────────────────────
MAX_DAILY_LOSS          = 5.0     # stop trading jika loss > $5/hari (30% dari $17)
MAX_OPEN_POSITIONS      = 1       # [EQUITY $17] hanya 1 posisi aktif — tidak cukup untuk multi-posisi
COOLDOWN_SL_CYCLES      = 3       # cooldown setelah SL
COOLDOWN_SMART_CYCLES   = 2       # cooldown setelah smart exit
BTC_CRASH_THRESHOLD     = -5.0    # % drop BTC 1h → blok entry
SIGNAL_MAX_AGE_HOURS    = 2       # sinyal lebih dari 2 jam diabaikan

# ── Recover filter ────────────────────────────────────────
# Abaikan koin dengan nilai < $1 saat auto-recover (dust protection)
MIN_POSITION_VALUE_USDT = 1.0

# Blacklist token yang sudah delisted di Gate.io — skip tanpa API call
# Update list ini jika ada token baru yang delisted
DELISTED_TOKENS: set = {
    "TEDDY", "FLOKICEO", "URO", "SHIBAI", "REKT", "MONG",
}

# ════════════════════════════════════════════════════════
#  UTILITIES
# ════════════════════════════════════════════════════════

def log(msg: str, level: str = "info"):
    ts  = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
    tag = {"info": "[INFO]", "warn": "[WARN]", "error": "[ERROR]"}.get(level, "[INFO]")
    print(f"{ts} {tag} {msg}")


def tg(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    body = json.dumps({
        "chat_id":                  TG_CHAT_ID,
        "text":                     msg,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=10)
            return
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt * 2)
            else:
                log(f"Telegram gagal: {e}", "warn")


def http_get(url: str, timeout: int = 6):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log(f"HTTP {url[:60]}: {e}", "warn")
    return None


def get_usdt_idr_rate() -> float:
    data = http_get("https://open.er-api.com/v6/latest/USD")
    if data and data.get("result") == "success":
        try:
            return float(data["rates"]["IDR"])
        except Exception:
            pass
    data = http_get("https://api.frankfurter.app/latest?from=USD&to=IDR")
    if data and "rates" in data:
        try:
            return float(data["rates"]["IDR"])
        except Exception:
            pass
    return 16300.0


def idr_fmt(usdt: float, rate: float) -> str:
    idr = usdt * rate
    if idr >= 1_000_000_000:
        return f"Rp{idr/1_000_000_000:.2f}M"
    elif idr >= 1_000_000:
        return f"Rp{idr/1_000_000:.2f}jt"
    elif idr >= 1_000:
        return f"Rp{idr:,.0f}"
    return f"Rp{idr:.2f}"


# ════════════════════════════════════════════════════════
#  GATE.IO CLIENT
# ════════════════════════════════════════════════════════

def setup_client():
    cfg = gate_api.Configuration(
        host="https://api.gateio.ws/api/v4",
        key=API_KEY, secret=SECRET_KEY
    )
    return gate_api.SpotApi(gate_api.ApiClient(cfg))


def gate_retry(fn, *args, retries=3, **kwargs):
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err = str(e).lower()
            delay = 5 if ("429" in err or "rate" in err) else 2 ** attempt
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                log(f"Gate API gagal {retries}x: {e}", "warn")
    return None


def get_usdt_balance(client) -> float:
    try:
        for acc in client.list_spot_accounts():
            if acc.currency == "USDT":
                return float(acc.available)
    except Exception as e:
        log(f"Balance USDT error: {e}", "warn")
    return 0.0


def get_coin_balance(client, currency: str) -> float:
    """Ambil balance coin tertentu (misal: PENGU, NEAR, HYPE)."""
    try:
        for acc in client.list_spot_accounts():
            if acc.currency == currency:
                return float(acc.available)
    except Exception as e:
        log(f"Balance {currency} error: {e}", "warn")
    return 0.0


def get_ticker_price(client, pair: str) -> float:
    try:
        tickers = gate_retry(client.list_tickers, currency_pair=pair)
        if tickers:
            return float(tickers[0].last or 0)
    except Exception as e:
        log(f"Ticker {pair} error: {e}", "warn")
    return 0.0


def get_pair_precision(client, pair: str) -> tuple:
    """Return (min_amount, amount_precision) untuk pair tertentu."""
    try:
        pairs = gate_retry(client.list_currency_pairs)
        if pairs:
            for p in pairs:
                if p.id == pair:
                    return float(p.min_base_amount or 0.001), int(p.amount_precision or 4)
    except Exception as e:
        log(f"Precision {pair} error: {e}", "warn")
    return 0.001, 4


# ════════════════════════════════════════════════════════
#  ORDER EXECUTION
# ════════════════════════════════════════════════════════

def do_buy(client, pair: str, order_usdt: float) -> tuple:
    """
    Market BUY untuk pair tertentu.
    Return: (buy_price, filled_amount) atau raise Exception.
    """
    price = get_ticker_price(client, pair)
    if price <= 0:
        raise Exception(f"Harga {pair} tidak valid")

    min_amount, precision = get_pair_precision(client, pair)
    amount = round(order_usdt / price, precision)

    if min_amount > 0 and amount < min_amount:
        raise Exception(f"Amount {amount} < min {min_amount} untuk {pair}")

    log(f"MARKET BUY {pair} | {amount} @ ${price:,.4f} | ${order_usdt:.2f} USDT")
    order = gate_api.Order(
        currency_pair=pair,
        type="market",
        side="buy",
        amount=str(amount),
        time_in_force="ioc",
    )
    result = gate_retry(client.create_order, order)
    if result is None:
        raise Exception(f"Buy order {pair} gagal")

    buy_price  = float(result.avg_deal_price or price)
    filled_qty = float(result.filled_total or 0)
    filled     = round(filled_qty / buy_price, precision) if filled_qty > 0 else amount

    log(f"✅ BUY filled: {filled} {pair.split('_')[0]} @ ${buy_price:,.4f}")
    return buy_price, filled


def do_sell(client, pair: str, amount: float, label: str = "SELL") -> float:
    """
    Market SELL untuk pair tertentu.
    Return: sell_price atau raise Exception.
    """
    currency   = pair.split("_")[0]
    coin_bal   = get_coin_balance(client, currency)
    _, precision = get_pair_precision(client, pair)

    if coin_bal <= 0:
        raise Exception(f"Saldo {currency} kosong")

    sell_amount = round(min(amount, coin_bal), precision)
    price       = get_ticker_price(client, pair)

    log(f"{label} {pair} | {sell_amount} {currency} @ ${price:,.4f}")
    order = gate_api.Order(
        currency_pair=pair,
        type="market",
        side="sell",
        amount=str(sell_amount),
        time_in_force="ioc",
    )
    result = gate_retry(client.create_order, order)
    if result is None:
        raise Exception(f"Sell order {pair} gagal")

    sell_price = float(result.avg_deal_price or price)
    log(f"✅ SELL filled: {sell_amount} {currency} @ ${sell_price:,.4f}")
    return sell_price


# ════════════════════════════════════════════════════════
#  SUPABASE — POSITIONS & TRADE HISTORY
# ════════════════════════════════════════════════════════

def load_open_positions() -> list:
    """Ambil semua posisi open dari Supabase."""
    try:
        res = supabase.table("positions") \
            .select("*").eq("status", "open").execute()
        return res.data or []
    except Exception as e:
        log(f"Load positions error: {e}", "warn")
        return []


def save_position(data: dict):
    try:
        supabase.table("positions").insert({**data, "status": "open"}).execute()
        log(f"📝 Position saved: {data.get('pair')} @ ${data.get('buy_price', 0):.4f}")
    except Exception as e:
        log(f"Save position error: {e}", "warn")


def update_position(position_id: int, data: dict):
    try:
        supabase.table("positions").update(data).eq("id", position_id).execute()
    except Exception as e:
        log(f"Update position error: {e}", "warn")


def close_position(position_id: int):
    try:
        supabase.table("positions").update({"status": "closed"}) \
            .eq("id", position_id).execute()
    except Exception as e:
        log(f"Close position error: {e}", "warn")


def save_trade(pair: str, buy_price: float, sell_price: float,
               amount: float, result: str, partial: bool = False,
               signal_id: str | None = None, notes: str = ""):
    try:
        profit = round((sell_price - buy_price) * amount, 6)
        supabase.table("trade_history").insert({
            "pair":       pair,
            "buy_price":  buy_price,
            "sell_price": sell_price,
            "amount":     amount,
            "profit":     profit,
            "result":     result,
            "partial":    partial,
            "notes":      notes,
            "signal_id":  signal_id,
            "closed_at":  datetime.now(timezone.utc).isoformat(),
        }).execute()
        log(f"📝 Trade saved: {pair} {result} | Profit: ${profit:.4f}")
        return profit
    except Exception as e:
        log(f"Save trade error: {e}", "warn")
        return (sell_price - buy_price) * amount


def update_signal_result(signal_id: str, result: str, pnl_usdt: float):
    """
    Write-back hasil trade ke signals_v2 di Supabase Signal Bot.
    Menggunakan supabase_signal agar Signal Bot bisa tracking WR-nya sendiri.
    """
    try:
        supabase_signal.table("signals_v2").update({
            "result":     result,
            "pnl_usdt":   round(pnl_usdt, 4),
            "closed_at":  datetime.now(timezone.utc).isoformat(),
        }).eq("id", signal_id).execute()
        log(f"📊 signals_v2 updated: {signal_id} → {result} (${pnl_usdt:.4f})")
    except Exception as e:
        log(f"Update signal result error: {e}", "warn")


# ════════════════════════════════════════════════════════
#  DAILY PnL & COOLDOWN
# ════════════════════════════════════════════════════════

def get_daily_pnl() -> float:
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        res   = supabase.table("trade_history") \
            .select("profit") \
            .gte("closed_at", f"{today}T00:00:00+00:00") \
            .execute()
        return sum(float(r["profit"] or 0) for r in (res.data or []))
    except Exception as e:
        log(f"Daily PnL error: {e}", "warn")
        return 0.0


def get_cooldown() -> int:
    try:
        res = supabase.table("bot_state") \
            .select("cooldown_remaining") \
            .eq("key", "altcoin_bot").execute()
        return int(res.data[0]["cooldown_remaining"]) if res.data else 0
    except Exception as e:
        log(f"Cooldown error: {e}", "warn")
        return 0


def set_cooldown(cycles: int):
    try:
        res = supabase.table("bot_state").select("key").eq("key", "altcoin_bot").execute()
        if res.data:
            supabase.table("bot_state") \
                .update({"cooldown_remaining": cycles}) \
                .eq("key", "altcoin_bot").execute()
        else:
            supabase.table("bot_state") \
                .insert({"key": "altcoin_bot", "cooldown_remaining": cycles}).execute()
    except Exception as e:
        log(f"Set cooldown error: {e}", "warn")


def decrement_cooldown():
    current = get_cooldown()
    if current > 0:
        set_cooldown(current - 1)
        log(f"⏳ Cooldown: {current} → {current-1} cycle tersisa")


# ════════════════════════════════════════════════════════
#  EQUITY CURVE CONTROL (dari v4 bot BTC)
# ════════════════════════════════════════════════════════

def get_dynamic_risk_pct() -> float:
    """
    Dynamic risk % berbasis equity curve dari N trade terakhir.
    Dipertahankan dari bot BTC v4 — logika yang sudah terbukti.
    """
    try:
        res = supabase.table("trade_history") \
            .select("profit") \
            .order("closed_at", desc=True) \
            .limit(EQUITY_LOOKBACK).execute()
        trades = [float(r["profit"] or 0) for r in (res.data or [])]
    except Exception:
        return RISK_PCT_DEFAULT

    if not trades:
        return RISK_PCT_DEFAULT

    losses_streak = 0
    for p in trades:
        if p < 0:
            losses_streak += 1
        else:
            break

    wins_streak = 0
    for p in trades:
        if p > 0:
            wins_streak += 1
        else:
            break

    if losses_streak >= 3:
        risk = RISK_PCT_FLOOR
        log(f"⚠️ ECC: {losses_streak} loss streak → risk floor {risk*100:.1f}%")
    elif losses_streak == 2:
        # FIX: Jangan turun ke 1% — di modal kecil itu di bawah MIN_ORDER_USDT
        # Gunakan floor yang sama agar order size tetap valid
        risk = RISK_PCT_FLOOR
        log(f"⚠️ ECC: 2 loss streak → risk floor {risk*100:.1f}% (modal kecil guard)")
    elif losses_streak == 1:
        # FIX: Sebelumnya 1.5% — masih terlalu kecil. Gunakan 50% dari default
        risk = max(RISK_PCT_DEFAULT * 0.6, RISK_PCT_FLOOR)
        log(f"⚠️ ECC: 1 loss streak → risk reduced {risk*100:.1f}%")
    elif wins_streak >= 3:
        boost = min(wins_streak - 2, 3) * 0.005
        risk  = min(RISK_PCT_DEFAULT + boost, RISK_PCT_CAP)
        log(f"📈 ECC: {wins_streak} win streak → risk boost {risk*100:.1f}%")
    else:
        risk = RISK_PCT_DEFAULT

    return risk


# ════════════════════════════════════════════════════════
#  BTC CRASH GUARD
# ════════════════════════════════════════════════════════

def check_btc_crash(client) -> bool:
    """
    Blok entry jika BTC drop > 5% dalam 1 jam.
    Altcoin biasanya ikut crash lebih dalam saat BTC crash.
    """
    try:
        candles = gate_retry(
            client.list_candlesticks,
            currency_pair="BTC_USDT",
            interval="1h",
            limit=2,
        )
        if candles and len(candles) >= 2:
            open_1h  = float(candles[-1][5])
            close_1h = float(candles[-1][2])
            change   = (close_1h - open_1h) / open_1h * 100
            if change <= BTC_CRASH_THRESHOLD:
                log(f"🛑 BTC crash detected: {change:.1f}% 1h — blok entry", "warn")
                return True
    except Exception as e:
        log(f"BTC crash check error: {e}", "warn")
    return False


# ════════════════════════════════════════════════════════
#  SIGNAL BOT INTEGRATION — baca signals_v2
# ════════════════════════════════════════════════════════

def get_pending_signals() -> list:
    """
    Ambil sinyal baru dari signals_v2 di Supabase Signal Bot.
    Menggunakan supabase_signal — client terpisah dari trading bot.
    Kriteria:
    - result IS NULL (belum ditutup)
    - side = 'BUY' (SELL disabled di Signal Bot)
    - sent_at dalam SIGNAL_MAX_AGE_HOURS jam terakhir
    - pair belum ada di open positions (dicek di caller)
    """
    try:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=SIGNAL_MAX_AGE_HOURS)).isoformat()
        res = supabase_signal.table("signals_v2") \
            .select("id, pair, side, entry, sl, tp1, tp2, score, tier, sent_at") \
            .is_("result", "null") \
            .eq("side", "BUY") \
            .gte("sent_at", cutoff) \
            .order("score", desc=True) \
            .execute()
        signals = res.data or []
        log(f"📡 {len(signals)} sinyal pending dari signals_v2")
        return signals
    except Exception as e:
        log(f"Get pending signals error: {e}", "warn")
        return []


def calc_order_size(equity: float, entry: float, sl: float,
                    risk_pct: float) -> float:
    """
    Hitung order size dalam USDT berdasarkan equity AKTUAL saat ini.
    Compounding: semakin besar equity, semakin besar order size otomatis.
    Dibatasi MIN_ORDER_USDT dan MAX_ORDER_USDT.
    Selalu sisakan buffer 20% dari equity untuk fee & slippage.
    """
    sl_pct = abs(entry - sl) / entry
    if sl_pct <= 0:
        return MIN_ORDER_USDT

    # Gunakan 80% equity sebagai modal aktif (20% buffer)
    tradeable  = equity * 0.80
    risk_usdt  = tradeable * risk_pct
    order_usdt = risk_usdt / sl_pct

    order_usdt = max(order_usdt, MIN_ORDER_USDT)
    order_usdt = min(order_usdt, MAX_ORDER_USDT)
    order_usdt = min(order_usdt, tradeable)  # tidak boleh melebihi modal aktif
    return round(order_usdt, 2)


# ════════════════════════════════════════════════════════
#  AUTO-RECOVER ORPHAN POSITION
# ════════════════════════════════════════════════════════

def auto_recover_orphan(client, open_positions: list):
    """
    Deteksi coin yang ada di wallet tapi tidak ada di open_positions.
    Jika ketemu, buat posisi recover dengan harga estimasi dari order history.
    """
    open_pairs = {p["pair"] for p in open_positions}

    try:
        accounts = client.list_spot_accounts()
    except Exception as e:
        log(f"List accounts error: {e}", "warn")
        return

    for acc in accounts:
        currency = acc.currency
        if currency == "USDT":
            continue

        # FIX: Skip token yang sudah diketahui delisted — tanpa API call sama sekali
        if currency in DELISTED_TOKENS:
            log(f"  [RECOVER] {currency} ada di blacklist delisted — skip")
            continue

        pair = f"{currency}_USDT"
        bal  = float(acc.available or 0)
        if bal <= 0:
            continue
        if pair in open_pairs:
            continue

        log(f"🔍 [RECOVER] {bal} {currency} ditemukan tanpa posisi — recover...")
        buy_price = 0.0

        # FIX: Tangkap INVALID_CURRENCY lebih awal — log sekali, tambah ke blacklist runtime
        try:
            orders = gate_retry(
                client.list_orders,
                currency_pair=pair,
                status="finished",
                side="buy",
                limit=5,
            )
            if orders:
                for o in orders:
                    avg = float(getattr(o, "avg_deal_price", 0) or 0)
                    if avg > 0:
                        buy_price = avg
                        break
        except Exception as e:
            err_str = str(e)
            if "INVALID_CURRENCY" in err_str or "delisted" in err_str.lower():
                log(f"  [RECOVER] {currency} delisted di Gate.io — skip & tambah blacklist", "warn")
                DELISTED_TOKENS.add(currency)
                continue
            log(f"Order history {pair} error: {e}", "warn")

        if buy_price <= 0:
            try:
                price = get_ticker_price(client, pair)
            except Exception as e:
                err_str = str(e)
                if "INVALID_CURRENCY" in err_str or "delisted" in err_str.lower():
                    log(f"  [RECOVER] {currency} delisted di Gate.io — skip & tambah blacklist", "warn")
                    DELISTED_TOKENS.add(currency)
                    continue
                price = 0.0
            if price > 0:
                buy_price = price
                log(f"  [RECOVER] Pakai harga live ${buy_price:.4f}")

        if buy_price <= 0:
            log(f"  [RECOVER] Tidak bisa tentukan harga beli {pair} — skip", "warn")
            continue

        # FIX: Filter dust — skip jika nilai posisi di bawah MIN_POSITION_VALUE_USDT
        position_value = bal * buy_price
        if position_value < MIN_POSITION_VALUE_USDT:
            log(f"  [RECOVER] {currency} terlalu kecil (${position_value:.6f}) — skip dust")
            continue

        # FIX: SL dinamis berbasis volatilitas harga — koin sub-cent lebih lebar
        # Harga > $1 → SL -3% | $0.01–$1 → SL -4% | < $0.01 → SL -5%
        if buy_price >= 1.0:
            sl_pct = 0.03
        elif buy_price >= 0.01:
            sl_pct = 0.04
        else:
            sl_pct = 0.05

        sl_price  = round(buy_price * (1 - sl_pct), 8)
        tp1_price = round(buy_price * 1.05, 8)
        tp2_price = round(buy_price * 1.10, 8)
        order_val = round(bal * buy_price, 2)

        save_position({
            "pair":       pair,
            "buy_price":  buy_price,
            "amount":     bal,
            "peak_price": buy_price,
            "sl_price":   sl_price,
            "tp1_price":  tp1_price,
            "tp2_price":  tp2_price,
            "tp1_hit":    False,
            "signal_id":  None,
            "notes":      f"AUTO-RECOVER | {bal} {currency} @ ${buy_price:.4f}",
        })
        tg(
            f"🔄 <b>Auto-Recover Posisi</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Pair   : <b>{pair}</b>\n"
            f"Amount : <b>{bal} {currency}</b> | Nilai: <b>${order_val:.2f}</b>\n"
            f"Buy    : <b>${buy_price:,.4f}</b>\n"
            f"SL     : <b>${sl_price:,.4f}</b> (-{sl_pct*100:.0f}%)\n"
            f"<i>⚠️ Verifikasi harga beli di Gate.io history.</i>"
        )
        log(f"✅ [RECOVER] {pair}: {bal} {currency} @ ${buy_price:.4f} (nilai: ${order_val:.2f})")


# ════════════════════════════════════════════════════════
#  HOLD MODE — evaluasi open positions
# ════════════════════════════════════════════════════════

def evaluate_position(client, pos: dict, idr_rate: float) -> str:
    """
    Evaluasi satu posisi open: cek SL, TP1, TP2.
    Return: 'sl' | 'tp1' | 'tp2' | 'hold'
    """
    pair       = pos["pair"]
    buy_price  = float(pos["buy_price"])
    amount     = float(pos["amount"])
    sl_price   = float(pos["sl_price"])
    tp1_price  = float(pos["tp1_price"])
    tp2_price  = float(pos["tp2_price"])
    tp1_hit    = bool(pos.get("tp1_hit", False))
    signal_id  = pos.get("signal_id")
    pos_id     = pos["id"]
    peak       = float(pos.get("peak_price") or buy_price)

    price = get_ticker_price(client, pair)
    if price <= 0:
        log(f"  ⚠️ Ticker {pair} gagal — skip evaluasi", "warn")
        return "hold"

    # FIX: Zombie position cleanup — tutup posisi jika nilai terlalu kecil untuk dijual
    # Ini mencegah dust posisi memblokir slot open selamanya
    position_value = amount * price
    if position_value < MIN_POSITION_VALUE_USDT:
        log(f"  🧹 {pair} zombie position (nilai: ${position_value:.6f}) — tutup tanpa sell")
        close_position(pos_id)
        log(f"  ✅ Zombie {pair} dibersihkan dari open positions")
        return "sl"  # return 'sl' agar dihitung sebagai closed di caller

    peak = max(peak, price)
    update_position(pos_id, {"peak_price": peak})

    profit_pct = (price / buy_price - 1) * 100
    currency   = pair.split("_")[0]
    log(f"  {pair} | Price:${price:,.4f} | PnL:{profit_pct:+.2f}% | "
        f"SL:${sl_price:.4f} TP1:${tp1_price:.4f} TP2:${tp2_price:.4f}")

    # ── STOP LOSS ──────────────────────────────────────
    if price <= sl_price:
        try:
            sell_price = do_sell(client, pair, amount, "STOP LOSS")
            profit     = round((sell_price - buy_price) * amount, 4)
            pct        = (sell_price / buy_price - 1) * 100
            save_trade(pair, buy_price, sell_price, amount, "SL",
                       signal_id=signal_id)
            if signal_id:
                update_signal_result(signal_id, "SL", profit)
            close_position(pos_id)
            set_cooldown(COOLDOWN_SL_CYCLES)
            tg(
                f"🔴 <b>STOP LOSS — {pair}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Buy  : ${buy_price:,.4f}\n"
                f"Sell : <b>${sell_price:,.4f}</b>\n"
                f"PnL  : <b>{profit:+.4f} USDT ({pct:+.2f}%)</b>\n"
                f"≈ {idr_fmt(abs(profit), idr_rate)}"
            )
            log(f"🔴 SL {pair} | Profit: ${profit:.4f}")
            return "sl"
        except Exception as e:
            log(f"  SL sell {pair} gagal: {e}", "error")
            return "hold"

    # ── TP2 — full exit ────────────────────────────────
    if price >= tp2_price:
        try:
            sell_price = do_sell(client, pair, amount, "TP2")
            profit     = round((sell_price - buy_price) * amount, 4)
            pct        = (sell_price / buy_price - 1) * 100
            save_trade(pair, buy_price, sell_price, amount, "TP2",
                       signal_id=signal_id)
            if signal_id:
                update_signal_result(signal_id, "TP2", profit)
            close_position(pos_id)
            tg(
                f"✅ <b>TP2 EXIT — {pair}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Buy  : ${buy_price:,.4f}\n"
                f"Sell : <b>${sell_price:,.4f}</b> ≈ {idr_fmt(sell_price, idr_rate)}\n"
                f"PnL  : <b>+{profit:.4f} USDT ({pct:+.2f}%)</b>\n"
                f"≈ {idr_fmt(profit, idr_rate)}"
            )
            log(f"✅ TP2 {pair} | Profit: ${profit:.4f}")
            return "tp2"
        except Exception as e:
            log(f"  TP2 sell {pair} gagal: {e}", "error")
            return "hold"

    # ── TP1 — partial exit 50% ─────────────────────────
    if price >= tp1_price and not tp1_hit:
        try:
            partial_amount = round(amount * TP1_SELL_RATIO, 8)
            sell_price     = do_sell(client, pair, partial_amount, "TP1 PARTIAL")
            partial_profit = round((sell_price - buy_price) * partial_amount, 4)
            pct            = (sell_price / buy_price - 1) * 100

            save_trade(pair, buy_price, sell_price, partial_amount,
                       "TP1", partial=True, signal_id=signal_id)

            remaining  = round(amount - partial_amount, 8)
            # Geser SL ke breakeven setelah TP1 hit
            new_sl     = round(buy_price * 1.002, 8)  # 0.2% di atas entry

            update_position(pos_id, {
                "tp1_hit":  True,
                "amount":   remaining,
                "sl_price": new_sl,    # SL → breakeven
            })
            tg(
                f"🥇 <b>TP1 PARTIAL — {pair}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Buy    : ${buy_price:,.4f}\n"
                f"Sell   : <b>${sell_price:,.4f}</b> (50% posisi)\n"
                f"PnL    : <b>+{partial_profit:.4f} USDT ({pct:+.2f}%)</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Sisa   : {remaining} {currency} → TP2:${tp2_price:,.4f}\n"
                f"SL baru: <b>${new_sl:,.4f}</b> (breakeven +0.2%)\n"
                f"≈ {idr_fmt(partial_profit, idr_rate)}"
            )
            log(f"🥇 TP1 {pair} | Partial: ${partial_profit:.4f} | SL → ${new_sl:.4f}")
            return "tp1"
        except Exception as e:
            log(f"  TP1 sell {pair} gagal: {e}", "error")
            return "hold"

    return "hold"


# ════════════════════════════════════════════════════════
#  ENTRY MODE — eksekusi sinyal baru
# ════════════════════════════════════════════════════════

def execute_signal(client, sig: dict, equity: float,
                   open_pairs: set, idr_rate: float) -> bool:
    """
    Eksekusi satu sinyal dari signals_v2.
    Return True jika berhasil entry, False jika skip.
    """
    pair      = sig["pair"]
    entry_ref = float(sig["entry"] or 0)
    sl_ref    = float(sig["sl"] or 0)
    tp1_ref   = float(sig["tp1"] or 0)
    tp2_ref   = float(sig["tp2"] or 0)
    score     = float(sig.get("score") or 0)
    tier      = sig.get("tier") or "B"
    signal_id = str(sig["id"])

    # Skip jika pair sudah punya posisi open
    if pair in open_pairs:
        log(f"   ⛔ {pair} — pair sudah ada di open positions")
        return False

    # Validasi data sinyal
    if entry_ref <= 0 or sl_ref <= 0 or tp1_ref <= 0 or tp2_ref <= 0:
        log(f"   ⛔ {pair} — data sinyal tidak lengkap (entry/sl/tp kosong)")
        return False

    # Ambil harga live dan cek apakah masih valid
    live_price = get_ticker_price(client, pair)
    if live_price <= 0:
        log(f"   ⛔ {pair} — tidak bisa ambil harga live")
        return False

    # Sinyal masih valid jika harga belum melebihi entry + 2% (slippage tolerance)
    price_drift = abs(live_price - entry_ref) / entry_ref
    if live_price > entry_ref * 1.02:
        log(f"   ⛔ {pair} — harga sudah naik {price_drift*100:.1f}% dari entry sinyal (skip)")
        return False
    if live_price < sl_ref:
        log(f"   ⛔ {pair} — harga ${live_price:.4f} sudah di bawah SL ${sl_ref:.4f}")
        return False

    # Hitung TP/SL dari harga live agar akurat
    # Pertahankan jarak % dari sinyal asli, tapi base dari harga aktual
    sl_pct  = abs(entry_ref - sl_ref) / entry_ref
    tp1_pct = abs(tp1_ref - entry_ref) / entry_ref
    tp2_pct = abs(tp2_ref - entry_ref) / entry_ref

    # Hitung RR dari data sinyal (tp2 vs sl)
    rr = round(tp2_pct / sl_pct, 2) if sl_pct > 0 else 0.0

    sl_actual  = round(live_price * (1 - sl_pct), 8)
    tp1_actual = round(live_price * (1 + tp1_pct), 8)
    tp2_actual = round(live_price * (1 + tp2_pct), 8)

    # Hitung order size
    risk_pct   = get_dynamic_risk_pct()
    order_usdt = calc_order_size(equity, live_price, sl_actual, risk_pct)

    log(f"   ✅ {pair} | Score:{score} RR:{rr} Tier:{tier} | "
        f"Entry:${live_price:.4f} SL:${sl_actual:.4f} "
        f"TP1:${tp1_actual:.4f} TP2:${tp2_actual:.4f} | ${order_usdt:.2f}")

    try:
        buy_price, filled = do_buy(client, pair, order_usdt)
    except Exception as e:
        log(f"   ❌ Buy {pair} gagal: {e}", "error")
        tg(f"❌ <b>Buy Gagal — {pair}</b>\n{e}")
        return False

    if buy_price <= 0 or filled <= 0:
        log(f"   ❌ {pair} — filled tidak valid", "error")
        return False

    # Recalc TP/SL dari harga fill aktual
    sl_final  = round(buy_price * (1 - sl_pct), 8)
    tp1_final = round(buy_price * (1 + tp1_pct), 8)
    tp2_final = round(buy_price * (1 + tp2_pct), 8)
    order_val = round(filled * buy_price, 2)

    save_position({
        "pair":       pair,
        "buy_price":  buy_price,
        "amount":     filled,
        "peak_price": buy_price,
        "sl_price":   sl_final,
        "tp1_price":  tp1_final,
        "tp2_price":  tp2_final,
        "tp1_hit":    False,
        "signal_id":  signal_id,
        "notes":      f"Score:{score} RR:{rr} Tier:{tier}",
    })

    tier_icon = "🥇" if tier == "A+" else "🥈" if tier == "A" else "🥉"
    currency  = pair.split("_")[0]
    tg(
        f"🟢 <b>BUY — {pair}</b> {tier_icon} Tier {tier}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Entry  : <b>${buy_price:,.4f}</b> ≈ {idr_fmt(buy_price, idr_rate)}\n"
        f"Amount : <b>{filled} {currency}</b> | Modal: <b>${order_val:.2f}</b>\n"
        f"Risk   : <b>{risk_pct*100:.1f}%</b> equity (ECC)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"TP1 : <b>${tp1_final:,.4f}</b> (+{tp1_pct*100:.1f}%) — <i>jual 50%</i>\n"
        f"TP2 : <b>${tp2_final:,.4f}</b> (+{tp2_pct*100:.1f}%)\n"
        f"SL  : <b>${sl_final:,.4f}</b> (-{sl_pct*100:.1f}%) | RR: <b>1:{rr:.1f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Score  : {score} | Signal ID: #{signal_id}\n"
        f"<i>⚠️ Bot akan jual otomatis di TP/SL.</i>"
    )
    log(f"🟢 BUY {pair} @ ${buy_price:.4f} | {filled} {currency} | "
        f"SL:${sl_final:.4f} TP1:${tp1_final:.4f} TP2:${tp2_final:.4f}")
    return True


# ════════════════════════════════════════════════════════
#  DAILY REPORT
# ════════════════════════════════════════════════════════

def send_daily_report(idr_rate: float):
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        res   = supabase.table("trade_history") \
            .select("profit, result, partial") \
            .gte("closed_at", f"{today}T00:00:00+00:00") \
            .execute()
        trades    = res.data or []
        total_pnl = sum(float(t["profit"] or 0) for t in trades)
        wins      = sum(1 for t in trades if float(t["profit"] or 0) > 0 and not t.get("partial"))
        losses    = sum(1 for t in trades if float(t["profit"] or 0) < 0)
        total_n   = wins + losses
        winrate   = (wins / total_n * 100) if total_n > 0 else 0.0
        emoji     = "✅" if total_pnl >= 0 else "❌"
        tg(
            f"📊 <b>Daily Report — Altcoin Bot v{BOT_VERSION}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Tanggal : {datetime.now(WIB).strftime('%d %b %Y')}\n"
            f"Trades  : {total_n} | W:{wins} L:{losses}\n"
            f"Win Rate: <b>{winrate:.1f}%</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Total PnL : {emoji} <b>{'+'if total_pnl>=0 else ''}{total_pnl:.4f} USDT</b>\n"
            f"≈ {idr_fmt(abs(total_pnl), idr_rate)}"
        )
    except Exception as e:
        log(f"Daily report error: {e}", "warn")


# ════════════════════════════════════════════════════════
#  MAIN RUN
# ════════════════════════════════════════════════════════

def run():
    log("=" * 55)
    log(f"🚀 ALTCOIN TRADING BOT v{BOT_VERSION} — "
        f"{datetime.now(WIB).strftime('%Y-%m-%d %H:%M WIB')}")
    log("=" * 55)

    client   = setup_client()
    idr_rate = get_usdt_idr_rate()
    log(f"💱 Kurs USD/IDR: Rp{idr_rate:,.0f}")

    balance = get_usdt_balance(client)
    log(f"💰 Balance USDT: ${balance:.2f} | "
        f"Growth: {((balance/INITIAL_EQUITY_USDT)-1)*100:+.1f}% dari modal awal ${INITIAL_EQUITY_USDT:.2f}")

    # Daily report jam 08:00 WIB
    now_wib = datetime.now(WIB)
    if now_wib.hour == 8 and now_wib.minute < 30:
        send_daily_report(idr_rate)

    # ── Step 1: Load open positions ───────────────────
    open_positions = load_open_positions()
    log(f"📂 Open positions: {len(open_positions)}/{MAX_OPEN_POSITIONS}")

    # ── Step 2: Auto-recover orphan positions ─────────
    log(f"\n── Auto-recover orphan positions ──")
    log(f"   Blacklist delisted: {len(DELISTED_TOKENS)} token ({', '.join(sorted(DELISTED_TOKENS))})")
    pre_recover_count = len(open_positions)
    auto_recover_orphan(client, open_positions)
    open_positions = load_open_positions()  # reload setelah recover
    recovered = len(open_positions) - pre_recover_count
    log(f"   Recover selesai: +{recovered} posisi baru | Total: {len(open_positions)}")

    # ── Step 3: Evaluasi semua open positions ─────────
    log(f"\n── Evaluasi {len(open_positions)} posisi open ──")
    closed_count = 0
    for pos in open_positions:
        result = evaluate_position(client, pos, idr_rate)
        if result in ("sl", "tp2"):
            closed_count += 1

    # Reload setelah evaluasi (beberapa posisi mungkin sudah close)
    open_positions = load_open_positions()
    open_pairs     = {p["pair"] for p in open_positions}
    log(f"   Selesai: {closed_count} ditutup | {len(open_positions)} masih open")

    # ── Step 4: Safety checks sebelum entry ───────────
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        log(f"⛔ Max posisi ({MAX_OPEN_POSITIONS}) tercapai — skip entry")
        return

    daily_pnl = get_daily_pnl()
    log(f"📉 Daily PnL: ${daily_pnl:.4f}")
    if daily_pnl <= -MAX_DAILY_LOSS:
        log(f"⛔ Max daily loss ${daily_pnl:.2f} — stop hari ini")
        tg(
            f"⛔ <b>Max Daily Loss Tercapai</b>\n"
            f"Loss hari ini: ${abs(daily_pnl):.2f}\n"
            f"Bot berhenti entry sampai besok."
        )
        return

    cooldown = get_cooldown()
    if cooldown > 0:
        decrement_cooldown()
        log(f"⏳ Cooldown aktif ({cooldown} cycle) — skip entry")
        return

    if check_btc_crash(client):
        tg(
            f"🛑 <b>BTC Crash Detected</b>\n"
            f"Drop > {abs(BTC_CRASH_THRESHOLD)}% dalam 1 jam.\n"
            f"Entry altcoin diblokir sampai kondisi stabil."
        )
        return

    # ── Step 5: Ambil sinyal baru dari Signal Bot ─────
    log(f"\n── Cek sinyal baru dari Signal Bot ──")
    signals = get_pending_signals()

    if not signals:
        log("📭 Tidak ada sinyal pending saat ini")
        return

    # Filter sinyal yang pairnya sudah open
    new_signals = [s for s in signals if s["pair"] not in open_pairs]
    log(f"   {len(new_signals)} sinyal valid (pair belum open)")

    if not new_signals:
        log("📭 Semua sinyal pair sudah punya posisi open")
        return

    # ── Step 6: Eksekusi sinyal ───────────────────────
    entries_done = 0
    max_entries  = MAX_OPEN_POSITIONS - len(open_positions)

    for sig in new_signals:
        if entries_done >= max_entries:
            log(f"⛔ Slot penuh — stop entry ({entries_done} dilakukan)")
            break

        # Refresh balance sebelum tiap entry
        balance = get_usdt_balance(client)
        if balance < MIN_ORDER_USDT:
            log(f"⚠️ Balance ${balance:.2f} tidak cukup — stop")
            break

        log(f"\n   → Coba entry: {sig['pair']} | "
            f"Score:{sig.get('score')} Tier:{sig.get('tier')}")

        ok = execute_signal(client, sig, balance, open_pairs, idr_rate)
        if ok:
            entries_done  += 1
            open_pairs.add(sig["pair"])
            time.sleep(1)  # jeda antar entry

    log(f"\n{'='*55}")
    log(f"✅ Run selesai — {entries_done} entry baru | "
        f"{len(open_positions)} posisi tetap open")
    log(f"{'='*55}")


if __name__ == "__main__":
    run()
