"""
╔══════════════════════════════════════════════════════════════════╗
║           BTC TRADING BOT v5.2 (Patch + Auto-Recover)                          ║
║                                                                  ║
║  [PATCH v5.1] Bug Fix — Gate.io Order Execution:               ║
║                                                                  ║
║  [FIX #1] Market order (buy & sell) sekarang eksplisit pakai   ║
║           time_in_force="ioc" (Immediate or Cancel).            ║
║           Root cause: gate_api library inject default "gtc"     ║
║           yang tidak didukung Gate.io untuk market order →      ║
║           error "TimeInForce gtc is not support for market"     ║
║                                                                  ║
║  [FIX #2] Empty Order ID guard di do_buy_limit():              ║
║           order_id di-strip dan divalidasi sebelum cancel.      ║
║           Root cause: cancel dipanggil dengan ID kosong →       ║
║           error "Empty order ID, BTC_USDT"                      ║
║           Fallback ke market order jika ID kosong saat create.  ║
║                                                                  ║
║  [FIX #3] do_buy() filled amount dihitung dari filled_total     ║
║           bukan result.amount (lebih akurat untuk market order) ║
║                                                                  ║
║  ─────── Semua fitur v5.0 dipertahankan ─────────────────     ║
║  [v5 #1] Multi-Timeframe OB Confluence (HTF Bias)              ║
║          Weekly OB → Daily OB → 4H OB → 1H entry              ║
║          Entry hanya saat 4H OB + 1H OB aligned = sniper      ║
║          HTF bias filter mencegah entry melawan macro trend    ║
║          Confluence score: 4H+1H OB aligned = +3 bonus        ║
║                                                                  ║
║  [v5 #2] HTF Liquidity Sweep (Weekly/Daily)                    ║
║          Sweep daily/weekly equal lows = institutional trap    ║
║          Big move capture: sweep HTF → reversal setup          ║
║          Score boost +2 jika daily EQL tersapu                 ║
║                                                                  ║
║  [v5 #3] Layered Position Entry (Scaling In)                   ║
║          Entry 1: 40% size @ OB zone                           ║
║          Entry 2: 35% size @ FVG retest (jika tersedia)        ║
║          Entry 3: 25% size @ deeper discount / EQL zone        ║
║          Max 3 layer per siklus, VWAP-averaged cost basis      ║
║                                                                  ║
║  [v5 #4] Adaptive Score Weight (Rule-Based Learning)           ║
║          Bobot sinyal dievaluasi dari 20 trade terakhir        ║
║          Sinyal dengan win rate tinggi dapat bobot lebih       ║
║          Sinyal underperform → bobot dikurangi 20%             ║
║          Tanpa ML dependency (deterministik, auditabel)        ║
║                                                                  ║
║  [v5 #5] Cooldown Refinement + Re-entry Guard                  ║
║          Anti re-entry dalam 2 jam setelah SL/Smart Exit       ║
║          Cooldown berbeda untuk SL vs smart exit               ║
║                                                                  ║
║  ─────── Dipertahankan dari v4 ──────────────────────          ║
║  [v4 #1] True Order Block + Fair Value Gap (FVG)               ║
║  [v4 #2] HTF Liquidity Map (Equal Highs / Equal Lows)          ║
║  [v4 #3] Funding Rate Dynamic Weight (Nonlinear)               ║
║  [v4 #4] Equity Curve Control (Dynamic Risk %)                 ║
║  [v4 #5] Multi-Scenario Exit                                    ║
║  [v3 #1-5] Demand Zone, Vol Filter, FR+OI, Limit, Sweep        ║
║  [v2 #1-10] Semua fitur v2 (ATR SL, partial TP, dll)          ║
║                                                                  ║
║  Arsitektur v5:                                                  ║
║  - Pair    : BTC/USDT only (portfolio layer = roadmap v6)      ║
║  - Entry   : 6-layer decision engine:                           ║
║              HTF Bias → MTF OB Confluence → Location           ║
║              → Context (Vol/FR) → Risk → Layered Execute       ║
║  - Order   : Layered limit (3 entries) → timeout → market     ║
║  - Exit    : SL → Trailing → Multi-Scenario → TP1→TP2→TP3     ║
║  - Risk    : Dynamic % equity (ECC) + Adaptive Signal Weight   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import json
import time
import math
import urllib.request
import numpy as np
import pandas as pd
import gate_api
from datetime import datetime, timedelta, timezone
from supabase import create_client

# ════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════

API_KEY      = os.environ.get("GATE_API_KEY")
SECRET_KEY   = os.environ.get("GATE_SECRET_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT_ID   = os.environ.get("CHAT_ID")

_missing = [k for k, v in {
    "GATE_API_KEY":    API_KEY,
    "GATE_SECRET_KEY": SECRET_KEY,
    "SUPABASE_URL":    SUPABASE_URL,
    "SUPABASE_KEY":    SUPABASE_KEY,
    "TELEGRAM_TOKEN":  TG_TOKEN,
    "CHAT_ID":         TG_CHAT_ID,
}.items() if not v]
if _missing:
    raise EnvironmentError(f"ENV belum diset: {', '.join(_missing)}")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Target pair ───────────────────────────────────────
PAIR = "BTC_USDT"

# ── Modal & Position Sizing (base) ───────────────────
# [v4 #4] RISK_PCT sekarang hanya default; nilai aktual dari get_dynamic_risk_pct()
RISK_PCT        = 0.02    # default 2% equity per trade
MIN_ORDER_USDT  = 5.0
MAX_ORDER_USDT  = 50.0

# ── TP / SL ──────────────────────────────────────────
ATR_SL_MULT     = 1.5
ATR_PERIOD      = 14

TP1_PCT         = 0.05
TP2_PCT         = 0.10
TP3_PCT         = 0.20
TRAILING_GAP    = 0.015
TP1_SELL_RATIO  = 0.50

# ── Risk-Reward ──────────────────────────────────────
MIN_RR          = 1.5

# ── Entry Score ──────────────────────────────────────
MIN_SCORE_ENTRY = 6

# ── Safety Guards ─────────────────────────────────────
BTC_CRASH_1H    = -5.0
MAX_DAILY_LOSS  = 30.0
COOLDOWN_CYCLES = 2

# ── [v3 #2] Volatility Regime ────────────────────────
VOLATILITY_MIN_ATR_PCT = 0.30

# ── [v4 #3] Funding Rate — Nonlinear Thresholds ──────
FR_EXTREME_LONG  =  0.10   # > 0.10%/8h → BLOCK entry
FR_HIGH_LONG     =  0.05   # > 0.05%/8h → score -3
FR_MILD_LONG     =  0.02   # > 0.02%/8h → score -1
FR_NEUTRAL_HIGH  =  0.02   # batas atas neutral
FR_NEUTRAL_LOW   = -0.01   # batas bawah neutral
FR_MILD_SHORT    = -0.01   # < -0.01%/8h → score +1
FR_STRONG_SHORT  = -0.05   # < -0.05%/8h → score +2

# ── [v4 #4] Equity Curve Control ─────────────────────
EQUITY_MIN_RISK_PCT        = 0.005  # floor: 0.5% risk saat drawdown parah
EQUITY_MAX_RISK_PCT        = 0.035  # cap: 3.5% risk saat win streak bagus
EQUITY_RISK_LOOKBACK       = 5      # lihat 5 trade terakhir
EQUITY_WIN_STREAK_BOOST    = 3      # mulai boost setelah 3 win berturut

# ── [v4 #5] Multi-Scenario Exit ───────────────────────
EXIT_STRUCTURE_MIN_PROFIT  = 1.0    # % profit minimum untuk structure exit
EXIT_MOMENTUM_MIN_PROFIT   = 0.5    # % profit minimum untuk momentum exit
EXIT_RSI_THRESHOLD         = 45     # RSI di bawah ini = momentum hilang

# ── [v3 #4] Limit Order ──────────────────────────────
LIMIT_ORDER_OFFSET_PCT = 0.0005
LIMIT_ORDER_TIMEOUT    = 45

# ── [v3 #1] Demand Zone Validation ───────────────────
DEMAND_VOL_SPIKE_RATIO  = 1.4
DEMAND_RECOVERY_ATR_PCT = 0.4

# ── [v4 #1] Order Block ───────────────────────────────
OB_STRONG_THRESHOLD_ATR = 0.25   # body candle harus > 25% ATR untuk dianggap kuat
OB_NEAR_DISTANCE_PCT    = 0.02   # price dalam 2% dari OB = "near OB"

# ── [v4 #2] HTF Liquidity Map ─────────────────────────
LIQ_TOLERANCE_PCT       = 0.0015  # 0.15% range untuk menganggap level "equal"
LIQ_MIN_TOUCHES         = 2       # minimum 2 kali sentuhan untuk jadi liquidity pool
LIQ_EQL_NEAR_PCT        = 1.0     # dalam 1% dari EQL = "sitting on EQL"
LIQ_EQH_NEAR_PCT        = 3.0     # dalam 3% dari EQH = "near EQH" (resistance)

# ── [v5 #1] Multi-Timeframe OB ────────────────────────
MTF_OB_CONFLUENCE_BONUS  = 3    # bonus score jika 4H + 1H OB aligned
MTF_HTF_BLOCK_BEARISH    = True # blok entry jika Daily OB bearish

# ── [v5 #2] HTF Liquidity Sweep ───────────────────────
HTF_SWEEP_LOOKBACK_DAILY = 30   # jumlah candle daily untuk deteksi EQL
HTF_SWEEP_SCORE_BOOST    = 2    # score bonus jika daily EQL tersapu
HTF_SWEEP_TOLERANCE_PCT  = 0.002  # 0.2% tolerance untuk daily EQL

# ── [v5 #3] Layered Entry (Scaling In) ────────────────
LAYER_ENABLED            = True
LAYER_1_RATIO            = 0.40   # 40% dari order_usdt di OB
LAYER_2_RATIO            = 0.35   # 35% di FVG retest
LAYER_3_RATIO            = 0.25   # 25% di EQL / deeper discount
LAYER_2_DISCOUNT_PCT     = 0.005  # 0.5% di bawah entry 1
LAYER_3_DISCOUNT_PCT     = 0.012  # 1.2% di bawah entry 1
MAX_LAYERS               = 3      # maksimal 3 layer per posisi

# ── [v5 #4] Adaptive Signal Weight ────────────────────
ADAPTIVE_LOOKBACK        = 20     # evaluasi dari N trade terakhir
ADAPTIVE_BOOST_THRESHOLD = 0.65   # sinyal dengan WR ≥ 65% → +20% bobot
ADAPTIVE_PENALTY_THRESHOLD = 0.40  # sinyal dengan WR ≤ 40% → -20% bobot

# ── [v5 #5] Cooldown Refinement ───────────────────────
COOLDOWN_SL              = 3    # cooldown setelah SL (lebih lama)
COOLDOWN_SMART_EXIT      = 2    # cooldown setelah smart exit (v4 #5)
REENTRY_GUARD_MINUTES    = 120  # tidak ada entry ulang dalam 2 jam

# ── Timezone ─────────────────────────────────────────
WIB = timezone(timedelta(hours=7))


# ════════════════════════════════════════════════════════
#  IN-MEMORY FALLBACK
# ════════════════════════════════════════════════════════
_mem_position: dict | None = None
_mem_daily_pnl: float = 0.0
_mem_cooldown: int = 0
_mem_dynamic_risk: float = RISK_PCT   # [v4 #4] fallback untuk dynamic risk
_mem_adaptive_weights: dict = {}       # [v5 #4] cache bobot sinyal adaptif
_mem_last_entry_time: float = 0.0     # [v5 #5] timestamp entry terakhir


# ════════════════════════════════════════════════════════
#  UTILITIES
# ════════════════════════════════════════════════════════

def log(msg: str):
    ts = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
    print(f"{ts} | {msg}")


def tg(msg: str):
    """Kirim pesan ke Telegram dengan retry 2x."""
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
                log(f"⚠️ Telegram gagal: {e}")


def http_get(url: str, timeout: int = 6):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log(f"⚠️ HTTP {url[:60]}: {e}")
        return None


def get_usdt_idr_rate() -> float:
    try:
        data = http_get("https://open.er-api.com/v6/latest/USD")
        if data and data.get("result") == "success":
            return float(data["rates"]["IDR"])
    except Exception:
        pass
    try:
        data = http_get("https://api.frankfurter.app/latest?from=USD&to=IDR")
        if data and "rates" in data:
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
            if attempt < retries - 1:
                delay = 2 ** attempt
                if "429" in err or "rate" in err:
                    delay = 5
                time.sleep(delay)
            else:
                log(f"⚠️ Gate API gagal setelah {retries}x: {e}")
    return None


# ════════════════════════════════════════════════════════
#  BALANCE
# ════════════════════════════════════════════════════════

def get_usdt_balance(client) -> float:
    try:
        for acc in client.list_spot_accounts():
            if acc.currency == "USDT":
                return float(acc.available)
    except Exception as e:
        log(f"⚠️ Balance error: {e}")
    return 0.0


def get_btc_balance(client) -> float:
    try:
        for acc in client.list_spot_accounts():
            if acc.currency == "BTC":
                return float(acc.available)
    except Exception as e:
        log(f"⚠️ BTC balance error: {e}")
    return 0.0


# ════════════════════════════════════════════════════════
#  [v5.2] AUTO-RECOVER ORPHAN POSITION
#  Deteksi BTC di wallet tapi tidak ada posisi di Supabase
# ════════════════════════════════════════════════════════

def auto_recover_orphan_position(client) -> bool:
    """
    Cek apakah ada BTC balance tapi tidak ada posisi open di Supabase.
    Jika ya, cari harga beli dari order history Gate.io dan auto-insert posisi.
    Returns True jika posisi berhasil di-recover.
    """
    existing = load_position()
    if existing:
        return False

    btc_bal = get_btc_balance(client)
    if btc_bal < 0.00001:
        return False

    log(f"\U0001f50d [RECOVER] {btc_bal} BTC ditemukan tanpa posisi di Supabase — recover...")

    buy_price = 0.0
    try:
        orders = gate_retry(
            client.list_orders,
            currency_pair=PAIR,
            status="finished",
            side="buy",
            limit=5,
        )
        if orders:
            for o in orders:
                avg = float(getattr(o, "avg_deal_price", 0) or 0)
                if avg > 0:
                    buy_price = avg
                    log(f"  [RECOVER] Order history: buy @ ${buy_price:,.2f}")
                    break
    except Exception as e:
        log(f"\u26a0\ufe0f [RECOVER] Gagal ambil order history: {e}")

    if buy_price <= 0:
        try:
            _tickers = gate_retry(client.list_tickers, currency_pair=PAIR)
            if _tickers:
                buy_price = float(_tickers[0].last or 0)
                log(f"  [RECOVER] Pakai harga live ${buy_price:,.2f} sebagai estimasi")
        except Exception:
            pass

    if buy_price <= 0:
        log("\u26a0\ufe0f [RECOVER] Tidak bisa tentukan harga beli — skip")
        return False

    sl_price = round(buy_price * (1 - 0.025), 2)

    save_position({
        "pair":       PAIR,
        "buy_price":  buy_price,
        "amount":     btc_bal,
        "peak_price": buy_price,
        "sl_price":   sl_price,
        "status":     "open",
        "tp1_hit":    False,
        "tp2_hit":    False,
        "notes":      f"AUTO-RECOVER | {btc_bal} BTC @ ${buy_price:,.2f}",
    })

    log(f"\u2705 [RECOVER] Posisi recover: {btc_bal} BTC @ ${buy_price:,.2f} SL:${sl_price:,.2f}")
    tg(
        f"\U0001f504 <b>Auto-Recover Posisi</b>\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"Ditemukan {btc_bal} BTC tanpa posisi tercatat.\n"
        f"Buy (est) : <b>${buy_price:,.2f}</b>\n"
        f"Amount    : <b>{btc_bal} BTC</b>\n"
        f"SL        : <b>${sl_price:,.2f}</b> (-2.5%)\n"
        f"<i>\u26a0\ufe0f Verifikasi harga beli di Gate.io history.</i>"
    )
    return True


# ════════════════════════════════════════════════════════
#  SUPABASE — dengan in-memory fallback
# ════════════════════════════════════════════════════════

def load_position() -> dict | None:
    global _mem_position
    try:
        res = supabase.table("positions").select("*").eq("status", "open").execute()
        pos = res.data[0] if res.data else None
        _mem_position = pos
        return pos
    except Exception as e:
        log(f"⚠️ Load position error: {e} — pakai memory")
        return _mem_position


def save_position(data: dict):
    global _mem_position
    _mem_position = {**data, "status": "open"}
    try:
        supabase.table("positions").update({"status": "closed"}).eq("status", "open").execute()
        supabase.table("positions").insert(data).execute()
    except Exception as e:
        log(f"⚠️ Save position error: {e} — tersimpan di memory saja")


def update_position(data: dict):
    global _mem_position
    if _mem_position:
        _mem_position.update(data)
    try:
        supabase.table("positions").update(data).eq("status", "open").execute()
    except Exception as e:
        log(f"⚠️ Update position error: {e}")


def clear_position():
    global _mem_position
    _mem_position = None
    try:
        supabase.table("positions").update({"status": "closed"}).eq("status", "open").execute()
    except Exception as e:
        log(f"⚠️ Clear position error: {e}")


def save_trade(buy_price: float, sell_price: float, amount: float,
               result: str, partial: bool = False, notes: str = ""):
    try:
        profit = round((sell_price - buy_price) * amount, 6)
        supabase.table("trade_history").insert({
            "pair":       PAIR,
            "buy_price":  buy_price,
            "sell_price": sell_price,
            "amount":     amount,
            "profit":     profit,
            "result":     result,
            "partial":    partial,
            "notes":      notes,
            "closed_at":  datetime.now(timezone.utc).isoformat(),
        }).execute()
        log(f"📝 Trade saved: {result} | Profit: ${profit:.4f} | Partial: {partial}")
        return profit
    except Exception as e:
        log(f"⚠️ Save trade error: {e}")
        return (sell_price - buy_price) * amount


# ════════════════════════════════════════════════════════
#  DAILY PnL & COOLDOWN
# ════════════════════════════════════════════════════════

def get_daily_pnl() -> float:
    global _mem_daily_pnl
    try:
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        res = supabase.table("trade_history") \
            .select("profit") \
            .gte("closed_at", f"{today_utc}T00:00:00+00:00") \
            .execute()
        total = sum(float(r["profit"] or 0) for r in res.data)
        _mem_daily_pnl = total
        return total
    except Exception as e:
        log(f"⚠️ Daily PnL error: {e} — memory: ${_mem_daily_pnl:.2f}")
        return _mem_daily_pnl


def get_cooldown() -> int:
    global _mem_cooldown
    try:
        res = supabase.table("bot_state") \
            .select("cooldown_remaining") \
            .eq("key", "btc_bot") \
            .execute()
        val = int(res.data[0]["cooldown_remaining"]) if res.data else 0
        _mem_cooldown = val
        return val
    except Exception as e:
        log(f"⚠️ Cooldown error: {e} — memory: {_mem_cooldown}")
        return _mem_cooldown


def set_cooldown(cycles: int):
    global _mem_cooldown
    _mem_cooldown = cycles
    try:
        res = supabase.table("bot_state").select("key").eq("key", "btc_bot").execute()
        if res.data:
            supabase.table("bot_state") \
                .update({"cooldown_remaining": cycles}) \
                .eq("key", "btc_bot").execute()
        else:
            supabase.table("bot_state") \
                .insert({"key": "btc_bot", "cooldown_remaining": cycles}).execute()
    except Exception as e:
        log(f"⚠️ Set cooldown error: {e}")


def decrement_cooldown():
    current = get_cooldown()
    if current > 0:
        set_cooldown(current - 1)
        log(f"⏳ Cooldown: {current} → {current - 1} cycle tersisa")


# ════════════════════════════════════════════════════════
#  [v4 #4] EQUITY CURVE CONTROL
#  Dynamic risk % berbasis streak dan performance
# ════════════════════════════════════════════════════════

def get_dynamic_risk_pct() -> float:
    """
    [v4 #4] Hitung risk % yang tepat berdasarkan equity curve.

    Pro systems tidak menggunakan risk flat — mereka menyesuaikan
    exposure berdasarkan kondisi performa saat ini:

    - 3+ loss berturut → floor 0.5% (lindungi sisa modal)
    - 2 loss berturut  → risk 1.0%
    - 1 loss           → risk 1.5%
    - Normal           → risk 2.0% (default)
    - 3+ win berturut  → risk naik bertahap (cap 3.5%)

    Ini mencegah death spiral saat bad run, dan mengizinkan
    compounding saat sistem dalam performa baik.
    Hanya menghitung full trades (partial TP tidak dihitung).
    """
    global _mem_dynamic_risk
    try:
        res = supabase.table("trade_history") \
            .select("profit, partial") \
            .order("closed_at", desc=True) \
            .limit(EQUITY_RISK_LOOKBACK + 5) \
            .execute()

        # Filter hanya full trades (bukan partial TP)
        full_trades = [
            float(t["profit"] or 0)
            for t in res.data
            if not t.get("partial", False)
        ][:EQUITY_RISK_LOOKBACK]

        if not full_trades:
            return RISK_PCT

        # Hitung streak dari trade terbaru
        win_streak  = 0
        loss_streak = 0

        for pnl in full_trades:
            if pnl > 0:
                if loss_streak == 0:
                    win_streak += 1
                else:
                    break
            elif pnl < 0:
                if win_streak == 0:
                    loss_streak += 1
                else:
                    break

        # Tentukan risk berdasarkan streak
        if loss_streak >= 3:
            dynamic = EQUITY_MIN_RISK_PCT         # hard floor
            label   = f"LOSS_STREAK≥3 → {dynamic*100:.1f}%"
        elif loss_streak == 2:
            dynamic = RISK_PCT * 0.50             # 1.0%
            label   = f"LOSS_STREAK=2 → {dynamic*100:.1f}%"
        elif loss_streak == 1:
            dynamic = RISK_PCT * 0.75             # 1.5%
            label   = f"LOSS_STREAK=1 → {dynamic*100:.1f}%"
        elif win_streak >= EQUITY_WIN_STREAK_BOOST:
            # Boost bertahap: 15% per win tambahan di atas threshold
            extra   = win_streak - EQUITY_WIN_STREAK_BOOST + 1
            factor  = min(1.75, 1.0 + extra * 0.15)
            dynamic = min(EQUITY_MAX_RISK_PCT, RISK_PCT * factor)
            label   = f"WIN_STREAK={win_streak} → {dynamic*100:.1f}%"
        else:
            dynamic = RISK_PCT
            label   = f"NORMAL → {dynamic*100:.1f}%"

        log(f"  [ECC] Equity Curve Control: {label} | W:{win_streak} L:{loss_streak}")
        _mem_dynamic_risk = dynamic
        return dynamic

    except Exception as e:
        log(f"⚠️ Dynamic risk error: {e} — pakai default {RISK_PCT*100:.1f}%")
        return _mem_dynamic_risk


# ════════════════════════════════════════════════════════
#  CANDLES & INDICATORS
#  [v4 #1] get_candles sekarang juga mengembalikan opens
#  Gate.io v4 format: [ts, vol, close, high, low, open, ...]
# ════════════════════════════════════════════════════════

def get_candles(client, interval: str, limit: int):
    """
    Returns: (closes, highs, lows, volumes, opens)
    Index 5 = open price (Gate.io v4 spot candlestick format).
    Fallback ke close jika index 5 tidak tersedia.
    """
    raw = gate_retry(
        client.list_candlesticks,
        currency_pair=PAIR, interval=interval, limit=limit
    )
    if not raw or len(raw) < 30:
        return None
    closes  = np.array([float(c[2]) for c in raw])
    highs   = np.array([float(c[3]) for c in raw])
    lows    = np.array([float(c[4]) for c in raw])
    volumes = np.array([float(c[1]) for c in raw])
    # Open price — index 5 ada di Gate.io v4; fallback ke close jika tidak ada
    opens   = np.array([float(c[5]) if len(c) > 5 else float(c[2]) for c in raw])
    return closes, highs, lows, volumes, opens


def calc_rsi(closes, period=14) -> float:
    s    = pd.Series(closes)
    d    = s.diff()
    gain = d.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    return float((100 - 100 / (1 + gain / (loss + 1e-9))).iloc[-1])


def calc_ema(closes, period) -> float:
    return float(pd.Series(closes).ewm(span=period, adjust=False).mean().iloc[-1])


def calc_macd(closes):
    s      = pd.Series(closes)
    macd   = s.ewm(span=12, adjust=False).mean() - s.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean()
    return float(macd.iloc[-1]), float(signal.iloc[-1])


def calc_atr(closes, highs, lows, period=14) -> float:
    tr = [max(highs[i] - lows[i],
              abs(highs[i] - closes[i-1]),
              abs(lows[i]  - closes[i-1]))
          for i in range(1, len(closes))]
    return float(pd.Series(tr).rolling(period).mean().iloc[-1])


# ════════════════════════════════════════════════════════
#  STRUCTURE ANALYSIS — [v2 #10]
# ════════════════════════════════════════════════════════

def detect_structure(closes, highs, lows, lookback=30) -> dict:
    """HH/HL = BULLISH, LH/LL = BEARISH, else NEUTRAL."""
    n    = min(len(closes), lookback)
    h    = highs[-n:]
    l    = lows[-n:]
    step = max(1, n // 5)
    sh   = [max(h[i:i+step]) for i in range(0, n - step, step)]
    sl_  = [min(l[i:i+step]) for i in range(0, n - step, step)]

    if len(sh) < 2 or len(sl_) < 2:
        return {"bias": "NEUTRAL", "hh": False, "hl": False}

    hh = sh[-1]  > sh[-2]
    hl = sl_[-1] > sl_[-2]
    lh = sh[-1]  < sh[-2]
    ll = sl_[-1] < sl_[-2]

    if hh and hl:
        bias = "BULLISH"
    elif lh and ll:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    return {"bias": bias, "hh": hh, "hl": hl, "lh": lh, "ll": ll}


# ════════════════════════════════════════════════════════
#  [v3 #2] VOLATILITY REGIME FILTER
# ════════════════════════════════════════════════════════

def get_volatility_regime(atr_1h: float, price: float) -> dict:
    """ATR/price < 0.30% → LOW_VOL → skip entry."""
    atr_pct = (atr_1h / price) * 100
    if atr_pct < VOLATILITY_MIN_ATR_PCT:
        regime, tradeable = "LOW_VOL", False
    elif atr_pct < 1.2:
        regime, tradeable = "NORMAL", True
    else:
        regime, tradeable = "HIGH_VOL", True
    return {"regime": regime, "atr_pct": round(atr_pct, 3), "tradeable": tradeable}


# ════════════════════════════════════════════════════════
#  [v3 #1] VALIDATED DEMAND ZONE
# ════════════════════════════════════════════════════════

def find_validated_demand_zone(closes, lows, volumes, atr_1h: float,
                                lookback: int = 30) -> dict:
    """
    Demand zone valid = swing low + (volume spike OR impulsive recovery).
    strength 2 = keduanya; 1 = satu; 0 = fallback (unvalidated).
    """
    n = min(len(closes), lookback)
    recent_lows   = lows[-n:]
    recent_closes = closes[-n:]
    recent_vols   = volumes[-n:]
    vol_avg       = float(np.mean(recent_vols)) + 1e-9

    valid_zones = []
    for i in range(1, len(recent_lows) - 2):
        is_swing = (
            recent_lows[i] < recent_lows[i - 1] and
            recent_lows[i] < recent_lows[i + 1]
        )
        if not is_swing:
            continue
        vol_spike = recent_vols[i] > vol_avg * DEMAND_VOL_SPIKE_RATIO
        impulsive = (
            (i + 1 < len(recent_closes)) and
            (recent_closes[i + 1] - recent_closes[i]) > atr_1h * DEMAND_RECOVERY_ATR_PCT
        )
        strength = (1 if vol_spike else 0) + (1 if impulsive else 0)
        if strength > 0:
            valid_zones.append({"level": float(recent_lows[i]), "strength": strength})

    if not valid_zones:
        swings = [
            recent_lows[i] for i in range(1, len(recent_lows) - 1)
            if recent_lows[i] < recent_lows[i-1] and recent_lows[i] < recent_lows[i+1]
        ]
        level = float(max(swings)) if swings else float(np.min(recent_lows))
        return {"level": level, "strength": 0, "validated": False}

    best = max(valid_zones, key=lambda z: z["level"])
    return {"level": best["level"], "strength": best["strength"], "validated": True}


# ════════════════════════════════════════════════════════
#  [v3 #5] LIQUIDITY SWEEP DETECTION
# ════════════════════════════════════════════════════════

def detect_liquidity_sweep(highs, lows, closes, lookback: int = 24) -> dict:
    """
    Wick di bawah swing low + close di atas = stop hunt → bullish reversal.
    """
    n           = min(len(closes), lookback)
    recent_lows = lows[-n:]

    swing_lows = [
        recent_lows[i]
        for i in range(1, len(recent_lows) - 3)
        if recent_lows[i] < recent_lows[i-1] and recent_lows[i] < recent_lows[i+1]
    ]
    if not swing_lows:
        return {"sweep_detected": False, "level": 0.0, "wick_depth_pct": 0.0}

    nearest_sl = max(swing_lows)
    for i in range(-3, 0):
        if lows[i] < nearest_sl and closes[i] > nearest_sl:
            return {
                "sweep_detected": True,
                "level":          round(nearest_sl, 2),
                "wick_depth_pct": round((nearest_sl - lows[i]) / nearest_sl * 100, 3),
            }
    return {"sweep_detected": False, "level": round(nearest_sl, 2), "wick_depth_pct": 0.0}


# ════════════════════════════════════════════════════════
#  [v4 #1] TRUE ORDER BLOCK + FAIR VALUE GAP
#  Deteksi akumulasi institusional yang sebenarnya
# ════════════════════════════════════════════════════════

def detect_orderblock(closes, highs, lows, opens, atr: float,
                      lookback: int = 50) -> dict:
    """
    [v4 #1] True Bullish Order Block Detection (SMC).

    Algoritma:
    1. Cari impulse bullish: 3 candle naik berturut dengan body > 25% ATR
    2. OB = candle bearish terakhir sebelum impulse itu
       (lokasi smart money accumulate sebelum harga naik kuat)
    3. FVG (Fair Value Gap) = gap/imbalance di dalam impulse:
       low[impulse+1] > high[impulse-1] → zone yang belum "diisi"
       Price cenderung kembali ke FVG sebelum lanjut naik

    Scoring:
    - Price dekat OB (dalam 2%):  +2
    - FVG ada dalam impulse:      +1 bonus

    Perbedaan OB vs demand zone biasa:
    - Demand zone = swing low (bisa karena apa saja)
    - Order block = titik di mana institusi PASTI ada posisi besar
      karena impulse setelahnya membuktikan ada yang beli besar
    """
    n = min(len(closes), lookback)
    c = closes[-n:]
    h = highs[-n:]
    l = lows[-n:]
    o = opens[-n:]

    strong_thresh = atr * OB_STRONG_THRESHOLD_ATR
    current       = c[-1]

    # Cari impulse bullish terbaru (dari candle terbaru ke belakang)
    for i in range(len(c) - 4, 2, -1):
        end = min(i + 3, len(c))
        if end - i < 3:
            continue

        all_bullish = all(c[j] > o[j] for j in range(i, end))
        all_strong  = all((c[j] - o[j]) > strong_thresh for j in range(i, end))

        if not (all_bullish and all_strong):
            continue

        # Impulse ditemukan mulai dari index i
        # Cari candle bearish terakhir sebelum impulse (max 5 candle ke belakang)
        for j in range(i - 1, max(-1, i - 6), -1):
            if j < 0:
                break
            if o[j] > c[j]:   # candle bearish
                ob_high = float(h[j])
                ob_low  = float(l[j])

                # Fair Value Gap: gap antara high[i-1] dan low[i+1] dalam impulse
                fvg = False
                fvg_high = fvg_low = None
                if i > 0 and i + 1 < len(l):
                    if l[i + 1] > h[i - 1]:
                        fvg      = True
                        fvg_high = round(float(l[i + 1]), 2)
                        fvg_low  = round(float(h[i - 1]), 2)

                # Tentukan jarak price ke OB
                dist_to_ob = (current - ob_high) / current * 100

                in_ob_zone = ob_low <= current <= ob_high
                near_ob    = (
                    in_ob_zone or
                    (0 <= dist_to_ob <= OB_NEAR_DISTANCE_PCT * 100)
                )

                log(f"  [OB] Detected: ${ob_low:,.0f}–${ob_high:,.0f} "
                    f"FVG:{fvg} Dist:{dist_to_ob:.2f}% NearOB:{near_ob}")

                return {
                    "detected":   True,
                    "ob_high":    round(ob_high, 2),
                    "ob_low":     round(ob_low, 2),
                    "fvg":        fvg,
                    "fvg_high":   fvg_high,
                    "fvg_low":    fvg_low,
                    "in_ob_zone": in_ob_zone,
                    "near_ob":    near_ob,
                    "dist_pct":   round(dist_to_ob, 2),
                }

    return {
        "detected":   False,
        "ob_high":    0.0,
        "ob_low":     0.0,
        "fvg":        False,
        "fvg_high":   None,
        "fvg_low":    None,
        "in_ob_zone": False,
        "near_ob":    False,
        "dist_pct":   999.0,
    }


# ════════════════════════════════════════════════════════
#  [v4 #2] HTF LIQUIDITY MAP — EQUAL HIGHS / EQUAL LOWS
#  4H levels yang disentuh 2x+ = institutional liquidity pool
# ════════════════════════════════════════════════════════

def detect_htf_liquidity(highs, lows, closes, lookback: int = 60) -> dict:
    """
    [v4 #2] HTF Liquidity Pool Detection (4H data).

    Equal Highs (EQH): level resistance yang disentuh ≥2 kali
    → liquidity pool berupa buy stop di atas level ini
    → target sweep / TP zona jika bullish momentum kuat

    Equal Lows (EQL): level support yang disentuh ≥2 kali
    → liquidity pool berupa sell stop di bawah level ini
    → price sering sweep ke sini sebelum naik (demand activation)

    Logika scoring:
    - Sitting on EQL (< 1% di atas EQL): +2 — harga dekat demand liquidity
    - Near EQH (< 3% di bawah EQH):     -1 — ada resistance besar di depan

    Informasi ini juga berguna untuk menilai TP3 realistis:
    jika EQH ada di antara TP2 dan TP3, TP3 mungkin perlu direvisi.
    """
    n         = min(len(closes), lookback)
    h         = highs[-n:]
    l         = lows[-n:]
    current   = closes[-1]
    tolerance = LIQ_TOLERANCE_PCT

    def cluster_levels(arr):
        """Kelompokkan level yang saling berdekatan (dalam tolerance %)."""
        visited = set()
        clusters = []
        for i in range(len(arr)):
            if i in visited:
                continue
            group = [
                arr[j] for j in range(len(arr))
                if abs(arr[j] - arr[i]) / (arr[i] + 1e-9) < tolerance
            ]
            if len(group) >= LIQ_MIN_TOUCHES:
                level = float(np.mean(group))
                clusters.append(level)
                for j in range(len(arr)):
                    if abs(arr[j] - level) / (level + 1e-9) < tolerance:
                        visited.add(j)
        return clusters

    eqh_levels = cluster_levels(h)
    eql_levels = cluster_levels(l)

    # Nearest EQH above current price
    eqh_above   = [lv for lv in eqh_levels if lv > current]
    nearest_eqh = round(min(eqh_above), 2) if eqh_above else None
    dist_to_eqh = round((nearest_eqh - current) / current * 100, 2) if nearest_eqh else None

    # Nearest EQL below current price
    eql_below   = [lv for lv in eql_levels if lv < current]
    nearest_eql = round(max(eql_below), 2) if eql_below else None
    dist_to_eql = round((current - nearest_eql) / current * 100, 2) if nearest_eql else None

    sitting_on_eql = bool(
        nearest_eql and dist_to_eql is not None and dist_to_eql < LIQ_EQL_NEAR_PCT
    )
    near_eqh = bool(
        nearest_eqh and dist_to_eqh is not None and dist_to_eqh < LIQ_EQH_NEAR_PCT
    )

    if nearest_eql or nearest_eqh:
        log(f"  [LIQ] EQL:${nearest_eql:,.0f}({dist_to_eql:.1f}%) "
            f"EQH:${nearest_eqh:,.0f}({dist_to_eqh:.1f}%) "
            f"SitEQL:{sitting_on_eql} NearEQH:{near_eqh}")

    return {
        "nearest_eqh":    nearest_eqh,
        "nearest_eql":    nearest_eql,
        "dist_to_eqh":    dist_to_eqh,
        "dist_to_eql":    dist_to_eql,
        "sitting_on_eql": sitting_on_eql,
        "near_eqh":       near_eqh,
        "total_eqh":      len(eqh_above),
        "total_eql":      len(eql_below),
    }


# ════════════════════════════════════════════════════════
#  [v4 #3] FUNDING RATE — DYNAMIC NONLINEAR WEIGHT
# ════════════════════════════════════════════════════════

def calc_funding_rate_score(fr_pct: float) -> tuple[int, str]:
    """
    [v4 #3] Konversi funding rate % ke score dan label — nonlinear.

    Sebelumnya (v3): fixed +1 / -2 → terlalu sederhana.
    Sekarang: tier bertingkat sesuai tingkat keparahan crowding.

    Extreme crowding → score = -99 (BLOCK entry) karena risiko
    liquidation cascade ke bawah sangat tinggi. Ini keputusan biner,
    bukan sekadar pengurangan score.
    """
    if fr_pct > FR_EXTREME_LONG:
        return -99, f"FR_EXTREME({fr_pct:+.4f}%)⛔"
    elif fr_pct > FR_HIGH_LONG:
        return -3,  f"FR_High({fr_pct:+.4f}%)❌"
    elif fr_pct > FR_MILD_LONG:
        return -1,  f"FR_Mild({fr_pct:+.4f}%)⚠️"
    elif fr_pct >= FR_NEUTRAL_LOW:
        return  0,  f"FR_OK({fr_pct:+.4f}%)"
    elif fr_pct >= FR_STRONG_SHORT:
        return +1,  f"FR_Short({fr_pct:+.4f}%)✅"
    else:
        return +2,  f"FR_StrongShort({fr_pct:+.4f}%)✅✅"


# ════════════════════════════════════════════════════════
#  [v5 #1] MULTI-TIMEFRAME OB CONFLUENCE
#  HTF Bias = Daily/Weekly OB alignment dengan 4H dan 1H
# ════════════════════════════════════════════════════════

def detect_mtf_ob_confluence(
    ob_1h: dict, ob_4h: dict,
    closes_daily, highs_daily, lows_daily, opens_daily, atr_daily: float
) -> dict:
    """
    [v5 #1] Multi-Timeframe Order Block Confluence.

    Masalah v4: OB hanya dari satu timeframe aktif.
    Hasilnya: entry kadang melawan bias HTF (macro bearish tapi
    masuk karena 1H OB — kalah oleh aliran institusi lebih besar).

    Solusi v5: Wajibkan konfirmasi hierarki:
      Daily OB → 4H OB → 1H entry

    Logic:
    1. Detect OB dari daily candles (HTF macro bias)
    2. Jika daily OB bullish (price near/in daily OB zone):
       → macro bias = BULLISH, izinkan 4H+1H alignment
    3. Jika 4H OB + 1H OB keduanya terdeteksi dan near:
       → confluence = True → bonus score +3
    4. Jika daily OB bearish (price > daily OB zone dari atas):
       → macro bias = BEARISH → hard block entry

    Ini menghasilkan "sniper entry" dengan multiple timeframe backing.
    Satu entry yang bagus lebih baik dari 5 entry yang mediocre.
    """
    ob_daily = detect_orderblock(closes_daily, highs_daily, lows_daily,
                                  opens_daily, atr_daily, lookback=60)

    current = closes_daily[-1] if len(closes_daily) > 0 else 0.0

    # Evaluasi macro bias dari daily OB
    daily_bullish_bias = False
    daily_bearish_block = False

    if ob_daily["detected"]:
        if ob_daily["near_ob"] or ob_daily["in_ob_zone"]:
            daily_bullish_bias = True
        elif current < ob_daily["ob_low"]:
            # Price jauh di bawah daily OB → belum nyampe akumulasi
            daily_bullish_bias = False
        elif current > ob_daily["ob_high"] * 1.05:
            # Price sudah jauh melewati OB dari atas → OB sudah habis
            daily_bearish_block = True

    # Cek confluence: keduanya 4H dan 1H OB terdeteksi dan near
    mtf_confluence = (
        ob_4h.get("detected") and ob_4h.get("near_ob") and
        ob_1h.get("detected") and ob_1h.get("near_ob")
    )

    # Skor berdasarkan kondisi
    confluence_score = 0
    label = "MTF_NEUTRAL"

    if daily_bearish_block and MTF_HTF_BLOCK_BEARISH:
        confluence_score = -99
        label = "MTF_DAILY_BLOCK❌"
    elif mtf_confluence and daily_bullish_bias:
        confluence_score = MTF_OB_CONFLUENCE_BONUS + 1  # +4 jika ada daily backing
        label = "MTF_FULL_ALIGN✅✅✅"
    elif mtf_confluence:
        confluence_score = MTF_OB_CONFLUENCE_BONUS  # +3 tanpa daily
        label = "MTF_4H1H_ALIGN✅✅"
    elif ob_4h.get("near_ob") and daily_bullish_bias:
        confluence_score = 2
        label = "MTF_DAILY4H_ALIGN✅"
    elif ob_1h.get("near_ob") and daily_bullish_bias:
        confluence_score = 1
        label = "MTF_DAILY1H✅"

    log(f"  [MTF] Daily OB:{ob_daily['detected']} Bias:{'BULL' if daily_bullish_bias else 'BEAR' if daily_bearish_block else 'NEUT'} "
        f"Confluence:{mtf_confluence} Score:{confluence_score} | {label}")

    return {
        "confluence_score":  confluence_score,
        "mtf_confluence":    mtf_confluence,
        "daily_bullish_bias": daily_bullish_bias,
        "daily_bearish_block": daily_bearish_block,
        "daily_ob_high":     ob_daily.get("ob_high", 0.0),
        "daily_ob_low":      ob_daily.get("ob_low", 0.0),
        "label":             label,
    }


# ════════════════════════════════════════════════════════
#  [v5 #2] HTF LIQUIDITY SWEEP — Daily/Weekly Level
#  Big move capture: sweep daily EQL → reversal signal
# ════════════════════════════════════════════════════════

def detect_htf_liquidity_sweep(
    highs_daily, lows_daily, closes_daily, lookback: int = 30
) -> dict:
    """
    [v5 #2] HTF Liquidity Sweep — Daily & Weekly equal lows.

    Perbedaan dengan v4 sweep (1H): itu hanya menangkap stop hunt lokal.
    Sweep daily EQL adalah institutional trap skala besar — ini yang
    mendahului big moves (5–15% rally dalam 3–5 hari).

    Cara kerja:
    1. Cluster daily lows yang saling berdekatan (dalam 0.2%) → EQL daily
    2. Jika 3 candle terakhir ada yang wick di bawah EQL tersebut
       DAN close kembali di atas EQL → sweep confirmed
    3. Ini artinya: sell stops di bawah daily EQL sudah di-grab institusi
       → reversal biasanya kuat dan cepat

    Score: +2 jika daily sweep confirmed (lebih besar dari 1H sweep)
    Ini dipakai bersamaan dengan v4 1H sweep untuk konfirmasi berlapis.
    """
    default = {"htf_sweep_detected": False, "htf_sweep_level": 0.0,
                "htf_sweep_depth_pct": 0.0, "htf_sweep_score": 0}

    try:
        n = min(len(closes_daily), lookback)
        d_lows   = lows_daily[-n:]
        d_closes = closes_daily[-n:]
        d_highs  = highs_daily[-n:]

        # Cluster daily swing lows → equal lows
        swing_lows = [
            d_lows[i] for i in range(1, len(d_lows) - 2)
            if d_lows[i] < d_lows[i - 1] and d_lows[i] < d_lows[i + 1]
        ]
        if len(swing_lows) < 2:
            return default

        # Cari cluster: dua swing low dalam 0.2% satu sama lain
        eql_candidates = []
        for i in range(len(swing_lows)):
            for j in range(i + 1, len(swing_lows)):
                diff_pct = abs(swing_lows[i] - swing_lows[j]) / (swing_lows[i] + 1e-9)
                if diff_pct < HTF_SWEEP_TOLERANCE_PCT:
                    eql_level = (swing_lows[i] + swing_lows[j]) / 2
                    eql_candidates.append(eql_level)

        if not eql_candidates:
            return default

        # Gunakan EQL tertinggi yang paling relevan (terdekat ke harga saat ini)
        current = d_closes[-1]
        eql_below = [lv for lv in eql_candidates if lv < current]
        if not eql_below:
            return default

        nearest_eql = max(eql_below)

        # Cek 3 candle terakhir: ada yang wick di bawah EQL dan close di atas?
        for i in range(-3, 0):
            if d_lows[i] < nearest_eql and d_closes[i] > nearest_eql:
                depth_pct = round((nearest_eql - d_lows[i]) / nearest_eql * 100, 3)
                log(f"  [HTF_SWEEP] Daily EQL:${nearest_eql:,.0f} "
                    f"Swept! Depth:{depth_pct:.3f}% +{HTF_SWEEP_SCORE_BOOST}pts")
                return {
                    "htf_sweep_detected":  True,
                    "htf_sweep_level":     round(nearest_eql, 2),
                    "htf_sweep_depth_pct": depth_pct,
                    "htf_sweep_score":     HTF_SWEEP_SCORE_BOOST,
                }

        return default

    except Exception as e:
        log(f"⚠️ HTF sweep error: {e}")
        return default


# ════════════════════════════════════════════════════════
#  [v5 #4] ADAPTIVE SIGNAL WEIGHT
#  Rule-based learning: bobot sinyal dari histori trade
# ════════════════════════════════════════════════════════

def get_adaptive_signal_weights() -> dict:
    """
    [v5 #4] Adaptive Signal Weight — tanpa ML, deterministik, auditabel.

    Prinsip:
    Sinyal yang secara historis berkorelasi dengan trade profit
    mendapat bobot lebih tinggi. Sinyal yang sering muncul di trade
    rugi mendapat bobot dikurangi.

    Implementasi sederhana tapi efektif:
    1. Ambil 20 trade terakhir dari Supabase (notes + profit)
    2. Parse sinyal dari field `notes` tiap trade
    3. Hitung win rate per sinyal
    4. Bobot = 1.0 (default) → 1.2 (WR ≥ 65%) → 0.8 (WR ≤ 40%)

    Ini bukan ML — ini adalah systematic signal evaluation.
    Hasilnya: bot tidak bergantung pada bobot hardcoded yang ditulis
    programmer, tapi adapts berdasarkan apa yang benar-benar bekerja.

    Contoh output:
    {"EMA4H": 1.2, "OB1H": 1.2, "FVG": 0.8, "Sweep": 1.0, ...}
    """
    global _mem_adaptive_weights
    default_weights = {
        "EMA4H": 1.0, "MACD4H": 1.0, "RSI4H": 1.0, "HHHL": 1.0,
        "OB1H": 1.0, "OB4H": 1.0, "FVG": 1.0, "EQL": 1.0,
        "Demand": 1.0, "Sweep": 1.0, "FR": 1.0, "MTF": 1.0,
    }
    try:
        res = supabase.table("trade_history") \
            .select("profit, notes") \
            .order("closed_at", desc=True) \
            .limit(ADAPTIVE_LOOKBACK) \
            .execute()

        if not res.data:
            return default_weights

        # Hitung win/total per signal keyword
        signal_stats: dict[str, list[int]] = {}  # signal → [wins, total]

        for trade in res.data:
            notes  = trade.get("notes") or ""
            profit = float(trade.get("profit") or 0)
            win    = 1 if profit > 0 else 0

            # Parse sinyal: split by "|" dan ambil prefix sebelum ":"
            for token in notes.split("|"):
                token = token.strip()
                # Normalisasi ke sinyal key
                key = None
                if token.startswith("EMA4H"):  key = "EMA4H"
                elif token.startswith("MACD4H"): key = "MACD4H"
                elif token.startswith("RSI4H"):  key = "RSI4H"
                elif "HH/HL" in token:           key = "HHHL"
                elif token.startswith("OB1H"):   key = "OB1H"
                elif token.startswith("OB4H"):   key = "OB4H"
                elif "FVG" in token:             key = "FVG"
                elif "EQL" in token:             key = "EQL"
                elif "Demand" in token:          key = "Demand"
                elif "Sweep" in token:           key = "Sweep"
                elif token.startswith("FR"):     key = "FR"
                elif "MTF" in token:             key = "MTF"

                if key:
                    if key not in signal_stats:
                        signal_stats[key] = [0, 0]
                    signal_stats[key][0] += win
                    signal_stats[key][1] += 1

        # Hitung adaptive weight per sinyal
        weights = dict(default_weights)
        for sig, (wins, total) in signal_stats.items():
            if total < 3:
                continue  # sample terlalu sedikit → pakai default
            wr = wins / total
            if wr >= ADAPTIVE_BOOST_THRESHOLD:
                weights[sig] = 1.2
            elif wr <= ADAPTIVE_PENALTY_THRESHOLD:
                weights[sig] = 0.8
            # else: tetap 1.0

        log(f"  [ADAPT] Signal weights: " +
            " ".join(f"{k}:{v:.1f}" for k, v in weights.items() if v != 1.0))
        _mem_adaptive_weights = weights
        return weights

    except Exception as e:
        log(f"⚠️ Adaptive weight error: {e} — pakai default")
        return _mem_adaptive_weights if _mem_adaptive_weights else default_weights


def apply_adaptive_weight(score_delta: int, signal_key: str, weights: dict) -> int:
    """
    Terapkan adaptive weight ke score delta.
    Input: score_delta = nilai tambahan asli (e.g. +2)
    Output: score_delta yang disesuaikan (dibulatkan ke int)
    """
    w = weights.get(signal_key, 1.0)
    return round(score_delta * w)


# ════════════════════════════════════════════════════════
#  [v5 #5] RE-ENTRY GUARD
#  Blok re-entry dalam 2 jam setelah exit untuk hindari
#  revenge trading / whipsaw
# ════════════════════════════════════════════════════════

def check_reentry_guard() -> bool:
    """
    [v5 #5] Re-entry guard: cegah entry ulang dalam REENTRY_GUARD_MINUTES
    setelah exit terakhir. Ini penting setelah:
    - SL (reversal mungkin belum selesai)
    - Smart exit (kondisi market sedang berubah)

    Returns True jika re-entry masih diblok.
    """
    global _mem_last_entry_time
    try:
        res = supabase.table("trade_history") \
            .select("closed_at, result") \
            .order("closed_at", desc=True) \
            .limit(1) \
            .execute()

        if not res.data:
            return False

        last_exit = res.data[0]
        result    = last_exit.get("result", "")

        # Guard hanya aktif setelah SL atau Smart Exit
        guard_triggers = {"STOP LOSS", "STRUCTURE_BREAK", "MOMENTUM_LOSS", "FUNDING_EXTREME"}
        if not any(t in result for t in guard_triggers):
            return False

        closed_at_str = last_exit.get("closed_at", "")
        if not closed_at_str:
            return False

        closed_at = datetime.fromisoformat(closed_at_str.replace("Z", "+00:00"))
        elapsed_min = (datetime.now(timezone.utc) - closed_at).total_seconds() / 60

        if elapsed_min < REENTRY_GUARD_MINUTES:
            remaining = int(REENTRY_GUARD_MINUTES - elapsed_min)
            log(f"  [GUARD] Re-entry guard aktif: {remaining} menit tersisa | Trigger: {result}")
            return True

    except Exception as e:
        log(f"⚠️ Re-entry guard error: {e}")

    return False


def get_funding_rate_oi() -> dict:
    """
    Fetch funding rate dan open interest dari Gate.io Futures (public API).
    Funding rate dikembalikan dalam % per 8 jam.
    """
    default = {"funding_rate_pct": 0.0, "open_interest": 0.0, "ok": False, "label": "N/A"}
    try:
        contract_data = http_get(
            "https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT",
            timeout=6
        )
        if not contract_data:
            return default

        fr_pct = round(float(contract_data.get("funding_rate", 0) or 0) * 100, 4)

        ticker_data = http_get(
            "https://api.gateio.ws/api/v4/futures/usdt/tickers?contract=BTC_USDT",
            timeout=6
        )
        oi = 0.0
        if ticker_data and isinstance(ticker_data, list) and len(ticker_data) > 0:
            oi = float(ticker_data[0].get("total_size", 0) or 0)

        _, label = calc_funding_rate_score(fr_pct)
        log(f"  [FR] Rate:{fr_pct:+.4f}%/8h | OI:{oi:,.0f} contracts | {label}")

        return {"funding_rate_pct": fr_pct, "open_interest": oi, "ok": True, "label": label}

    except Exception as e:
        log(f"⚠️ Funding rate error: {e}")
        return default


# ════════════════════════════════════════════════════════
#  CRASH PROTECTION — [v2 #4]
# ════════════════════════════════════════════════════════

def check_crash(client) -> bool:
    try:
        data = get_candles(client, "1h", 5)
        if data is None:
            return False
        closes = data[0]
        chg_1h = (closes[-1] - closes[-2]) / closes[-2] * 100
        if chg_1h < BTC_CRASH_1H:
            log(f"🛑 BTC crash: {chg_1h:.2f}% dalam 1h — halt entry")
            return True
        return False
    except Exception as e:
        log(f"⚠️ Crash check error: {e}")
        return False


# ════════════════════════════════════════════════════════
#  ANALISIS BTC — ENTRY SCORING v4.0
#  5-layer decision engine: Trend → Location → Context → Risk
# ════════════════════════════════════════════════════════

def analyze_btc(client, balance: float) -> dict:
    """
    Entry scoring v5.0 — multi-dimensional edge framework:

    Layer 1 — HTF Bias (v5):   Daily OB macro context
    Layer 2 — Trend (v2):      EMA, RSI, MACD, Structure
    Layer 3 — Location (v5):   MTF OB Confluence + FVG + EQL + Demand + Sweep
    Layer 4 — Context (v4):    Volatility regime, FR dynamic weight
    Layer 5 — Risk (v2):       ATR SL, RR ≥ 1.5
    Layer 6 — Sizing (v5):     Dynamic risk (ECC) + Adaptive weights + Layer entry
    """
    result = {"valid": False, "score": 0, "reason": ""}

    # ── [v5 #5] Re-entry guard dulu ──────────────────
    if check_reentry_guard():
        result["reason"] = "Re-entry guard aktif (< 2 jam setelah SL/SmartExit)"
        return result

    # ── Daily Data (v5 #1: HTF Bias) ─────────────────
    data_daily = get_candles(client, "1d", 60)
    if data_daily is None:
        result["reason"] = "Candle daily gagal"
        return result
    closes_daily, highs_daily, lows_daily, vols_daily, opens_daily = data_daily
    atr_daily = calc_atr(closes_daily, highs_daily, lows_daily)

    # ── 4H Data ──────────────────────────────────────
    data_4h = get_candles(client, "4h", 100)
    if data_4h is None:
        result["reason"] = "Candle 4h gagal"
        return result
    closes_4h, highs_4h, lows_4h, vols_4h, opens_4h = data_4h

    rsi_4h          = calc_rsi(closes_4h)
    ema20_4h        = calc_ema(closes_4h, 20)
    ema50_4h        = calc_ema(closes_4h, 50)
    macd_4h, sig_4h = calc_macd(closes_4h)
    atr_4h          = calc_atr(closes_4h, highs_4h, lows_4h)
    struct_4h       = detect_structure(closes_4h, highs_4h, lows_4h, lookback=40)

    # ── 1H Data ──────────────────────────────────────
    data_1h = get_candles(client, "1h", 100)
    if data_1h is None:
        result["reason"] = "Candle 1h gagal"
        return result
    closes_1h, highs_1h, lows_1h, vols_1h, opens_1h = data_1h

    rsi_1h          = calc_rsi(closes_1h)
    ema20_1h        = calc_ema(closes_1h, 20)
    ema50_1h        = calc_ema(closes_1h, 50)
    macd_1h, sig_1h = calc_macd(closes_1h)
    atr_1h          = calc_atr(closes_1h, highs_1h, lows_1h)

    # ── Harga live ───────────────────────────────────
    ticker = gate_retry(client.list_tickers, currency_pair=PAIR)
    if not ticker:
        result["reason"] = "Ticker gagal"
        return result
    price      = float(ticker[0].last or 0)
    change_24h = float(ticker[0].change_percentage or 0)
    if math.isnan(change_24h):
        change_24h = 0.0

    # ── [v3 #2] Volatility Regime — hard block dulu ──
    vol_regime = get_volatility_regime(atr_1h, price)
    if not vol_regime["tradeable"]:
        result["reason"]   = (
            f"Low volatility (ATR {vol_regime['atr_pct']:.3f}% < "
            f"{VOLATILITY_MIN_ATR_PCT}%) — market flat/ranging"
        )
        result["regime"]   = "LOW_VOL"
        result["vol_info"] = vol_regime
        return result

    # ── Fetch semua data eksternal ────────────────────
    demand      = find_validated_demand_zone(closes_1h, lows_1h, vols_1h, atr_1h)
    demand_zone = demand["level"]
    sweep       = detect_liquidity_sweep(highs_1h, lows_1h, closes_1h)
    ob_1h       = detect_orderblock(closes_1h, highs_1h, lows_1h, opens_1h, atr_1h, lookback=50)
    ob_4h       = detect_orderblock(closes_4h, highs_4h, lows_4h, opens_4h, atr_4h, lookback=60)
    liq         = detect_htf_liquidity(highs_4h, lows_4h, closes_4h, lookback=60)
    fr_data     = get_funding_rate_oi()

    # ── [v5 #1] MTF OB Confluence ────────────────────
    mtf = detect_mtf_ob_confluence(ob_1h, ob_4h,
                                    closes_daily, highs_daily, lows_daily,
                                    opens_daily, atr_daily)

    # ── [v5 #2] HTF Liquidity Sweep (Daily) ──────────
    htf_sweep = detect_htf_liquidity_sweep(highs_daily, lows_daily, closes_daily,
                                            lookback=HTF_SWEEP_LOOKBACK_DAILY)

    # ── [v5 #4] Adaptive Signal Weights ──────────────
    aw = get_adaptive_signal_weights()

    dist_to_demand = (price - demand_zone) / price * 100
    vol_avg_1h     = float(np.mean(vols_1h[-11:-1]))
    vol_ratio      = float(vols_1h[-1]) / (vol_avg_1h + 1e-9)
    momentum_up    = closes_1h[-1] > closes_1h[-2] > closes_1h[-3]

    # ════════════════════════════════════════════════
    #  SCORING ENGINE v5
    # ════════════════════════════════════════════════
    score = 0
    notes = []

    # ─── Layer 1: HTF Bias + MTF OB [v5 #1] ─────────
    if mtf["confluence_score"] == -99:
        score = -99
        notes.append(mtf["label"])
    else:
        cs = apply_adaptive_weight(mtf["confluence_score"], "MTF", aw)
        if cs != 0:
            score += cs
            notes.append(f"{mtf['label']}({cs:+d})")

    # ─── Layer 2: Trend (4H) ─────────────────────────
    if score != -99:
        if ema20_4h > ema50_4h:
            delta = apply_adaptive_weight(3, "EMA4H", aw)
            score += delta; notes.append(f"EMA4H↑({delta:+d})")
        if rsi_4h < 65:
            delta = apply_adaptive_weight(2, "RSI4H", aw)
            score += delta; notes.append(f"RSI4H:{rsi_4h:.0f}({delta:+d})")
        if macd_4h > sig_4h:
            delta = apply_adaptive_weight(2, "MACD4H", aw)
            score += delta; notes.append(f"MACD4H↑({delta:+d})")
        if struct_4h["bias"] == "BULLISH":
            delta = apply_adaptive_weight(2, "HHHL", aw)
            score += delta; notes.append(f"HH/HL✅({delta:+d})")
        elif struct_4h["bias"] == "BEARISH":
            score -= 3; notes.append("LH/LL❌")

        # 1H confirmation
        if ema20_1h > ema50_1h:
            score += 1; notes.append("EMA1H↑")
        if rsi_1h < 60:
            score += 1; notes.append(f"RSI1H:{rsi_1h:.0f}")
        if macd_1h > sig_1h:
            score += 1; notes.append("MACD1H↑")
        if vol_ratio > 1.5:
            score += 1; notes.append(f"Vol:{vol_ratio:.1f}x")
        if momentum_up:
            score += 1; notes.append("Mom↑")

        # ─── Layer 3a: True Order Block [v4 #1] ──────
        ob_used = None
        if ob_1h["detected"] and ob_1h["near_ob"]:
            delta = apply_adaptive_weight(2, "OB1H", aw)
            score += delta; notes.append(f"OB1H✅(d:{ob_1h['dist_pct']:.1f}%,{delta:+d})")
            if ob_1h["fvg"]:
                fvg_delta = apply_adaptive_weight(1, "FVG", aw)
                score += fvg_delta; notes.append(f"FVG✅({fvg_delta:+d})")
            ob_used = ob_1h
        elif ob_4h["detected"] and ob_4h["near_ob"]:
            delta = apply_adaptive_weight(2, "OB4H", aw)
            score += delta; notes.append(f"OB4H✅(d:{ob_4h['dist_pct']:.1f}%,{delta:+d})")
            if ob_4h["fvg"]:
                fvg_delta = apply_adaptive_weight(1, "FVG", aw)
                score += fvg_delta; notes.append(f"FVG✅({fvg_delta:+d})")
            ob_used = ob_4h

        # ─── Layer 3b: HTF Liquidity Map [v4 #2] ─────
        if liq["sitting_on_eql"]:
            delta = apply_adaptive_weight(2, "EQL", aw)
            score += delta; notes.append(f"EQL✅({liq['dist_to_eql']:.1f}%,{delta:+d})")
        if liq["near_eqh"]:
            score -= 1; notes.append(f"NearEQH⚠️({liq['dist_to_eqh']:.1f}%)")

        # ─── Layer 3c: Validated Demand [v3 #1] ──────
        if dist_to_demand < 1.5:
            if demand["validated"] and demand["strength"] == 2:
                delta = apply_adaptive_weight(3, "Demand", aw)
                score += delta; notes.append(f"StrongDemand✅✅({dist_to_demand:.1f}%,{delta:+d})")
            elif demand["validated"] and demand["strength"] == 1:
                delta = apply_adaptive_weight(2, "Demand", aw)
                score += delta; notes.append(f"ValidDemand✅({dist_to_demand:.1f}%,{delta:+d})")
            else:
                score += 1; notes.append(f"WeakDemand⚠️({dist_to_demand:.1f}%)")
        elif dist_to_demand > 5.0:
            score -= 1; notes.append(f"FarSupport({dist_to_demand:.1f}%)")

        # ─── Layer 3d: 1H Liquidity Sweep [v3 #5] ────
        if sweep["sweep_detected"]:
            delta = apply_adaptive_weight(2, "Sweep", aw)
            score += delta; notes.append(f"Sweep1H✅(wick:{sweep['wick_depth_pct']:.2f}%,{delta:+d})")

        # ─── [v5 #2] HTF Liquidity Sweep (Daily) ─────
        if htf_sweep["htf_sweep_detected"]:
            score += htf_sweep["htf_sweep_score"]
            notes.append(f"HTFSweep✅(D:{htf_sweep['htf_sweep_level']:,.0f})")

        # ─── Layer 4: Funding Rate Dynamic [v4 #3] ───
        if fr_data["ok"]:
            fr_score, fr_note = calc_funding_rate_score(fr_data["funding_rate_pct"])
            if fr_score == -99:
                score = -99
                notes.append(fr_note)
            else:
                delta = apply_adaptive_weight(fr_score, "FR", aw) if fr_score != 0 else 0
                score += delta
                notes.append(fr_note if delta == fr_score else f"{fr_note}(w:{delta:+d})")
        else:
            notes.append("FR:N/A")

        # ─── Penalti umum ─────────────────────────────
        if rsi_4h > 75:
            score -= 3; notes.append("RSI4H_OB!")
        if rsi_1h > 72:
            score -= 2; notes.append("RSI1H_OB!")
        if change_24h > 8:
            score -= 2; notes.append(f"+{change_24h:.0f}%24H!")
        if not momentum_up:
            score -= 1

        # ─── Regime hard block ─────────────────────────
        regime = vol_regime["regime"]
        if ema20_4h < ema50_4h and rsi_4h < 45:
            regime = "BEARISH"
            score  = -99
            notes.append("BEARISH!")
        elif rsi_4h > 75 and rsi_1h > 70:
            regime = "OVERBOUGHT"
            score  = -99
            notes.append("OVERBOUGHT!")
    else:
        regime = "MTF_BLOCK"
        ob_used = None

    log(f"BTC Score:{score} | {' | '.join(notes)}")
    log(f"  4H: RSI:{rsi_4h:.1f} EMA:{ema20_4h:.0f}/{ema50_4h:.0f} Struct:{struct_4h['bias']}")
    log(f"  1H: RSI:{rsi_1h:.1f} Vol:{vol_ratio:.1f}x Mom:{'↑' if momentum_up else '↓'}")
    log(f"  [v5] MTF:{mtf['label']} HTFSweep:{htf_sweep['htf_sweep_detected']} "
        f"OB_1H:{ob_1h['detected']} OB_4H:{ob_4h['detected']} "
        f"EQL:{liq['sitting_on_eql']} EQH:{liq['near_eqh']}")

    if score < MIN_SCORE_ENTRY:
        result.update({
            "reason":   f"Score {score} < {MIN_SCORE_ENTRY}",
            "score":    score,
            "notes":    " | ".join(notes),
            "regime":   regime,
            "vol_info": vol_regime,
        })
        return result

    # ── [v2 #1] ATR-based SL ─────────────────────────
    sl_price = price - (atr_1h * ATR_SL_MULT)
    sl_pct   = max(0.01, min(0.05, (price - sl_price) / price))
    sl_price = round(price * (1 - sl_pct), 2)

    tp1_price = round(price * (1 + TP1_PCT), 2)
    tp2_price = round(price * (1 + TP2_PCT), 2)
    tp3_price = round(price * (1 + TP3_PCT), 2)

    # ── [v2 #2] RR check ─────────────────────────────
    sl_dist  = price - sl_price
    tp1_dist = tp1_price - price
    rr       = tp1_dist / sl_dist if sl_dist > 0 else 0

    if rr < MIN_RR:
        result.update({
            "reason":   f"RR {rr:.2f} < {MIN_RR}",
            "score":    score,
            "notes":    " | ".join(notes),
            "regime":   regime,
            "vol_info": vol_regime,
        })
        return result

    # ── [v4 #4] Dynamic risk sizing ──────────────────
    active_risk = get_dynamic_risk_pct()
    risk_amount = balance * active_risk
    order_usdt  = round(risk_amount / sl_pct, 2)
    order_usdt  = max(MIN_ORDER_USDT, min(MAX_ORDER_USDT, order_usdt))

    # ── [v5 #3] Layer entry sizing ───────────────────
    # Kalkulasi ukuran per layer — dipakai di do_buy_layered()
    layer_sizes = []
    if LAYER_ENABLED:
        layer_sizes = [
            round(order_usdt * LAYER_1_RATIO, 2),
            round(order_usdt * LAYER_2_RATIO, 2),
            round(order_usdt * LAYER_3_RATIO, 2),
        ]
        # Layer 2 entry price: FVG low jika ada, else LAYER_2_DISCOUNT dari entry 1
        layer_2_price = None
        ob_info_used  = ob_used or {}
        if ob_info_used.get("fvg") and ob_info_used.get("fvg_low"):
            layer_2_price = ob_info_used["fvg_low"]
        else:
            layer_2_price = round(price * (1 - LAYER_2_DISCOUNT_PCT), 2)

        # Layer 3: EQL level jika dekat, else deeper discount
        layer_3_price = None
        if liq.get("nearest_eql") and liq.get("dist_to_eql", 999) < 2.0:
            layer_3_price = liq["nearest_eql"]
        else:
            layer_3_price = round(price * (1 - LAYER_3_DISCOUNT_PCT), 2)
    else:
        layer_2_price = layer_3_price = None

    log(f"  SL:{sl_pct*100:.2f}% | RR:1:{rr:.2f} | Risk:{active_risk*100:.1f}% | "
        f"Size:${order_usdt:.2f} | Layers:{layer_sizes}")

    # OB context untuk Telegram
    ob_info = ob_used or {"ob_high": 0.0, "ob_low": 0.0, "fvg": False}

    return {
        "valid":            True,
        "score":            score,
        "price":            price,
        "rsi_4h":           round(rsi_4h, 1),
        "rsi_1h":           round(rsi_1h, 1),
        "ema20_4h":         round(ema20_4h, 2),
        "ema50_4h":         round(ema50_4h, 2),
        "atr_1h":           round(atr_1h, 2),
        "sl_price":         sl_price,
        "sl_pct":           round(sl_pct * 100, 2),
        "tp1_price":        tp1_price,
        "tp2_price":        tp2_price,
        "tp3_price":        tp3_price,
        "rr":               round(rr, 2),
        "regime":           regime,
        "struct_4h":        struct_4h["bias"],
        "change_24h":       round(change_24h, 2),
        "order_usdt":       order_usdt,
        "notes":            " | ".join(notes),
        "reason":           "",
        # v3 fields
        "vol_regime":       vol_regime["regime"],
        "vol_atr_pct":      vol_regime["atr_pct"],
        "demand_strength":  demand["strength"],
        "demand_validated": demand["validated"],
        "demand_dist_pct":  round(dist_to_demand, 2),
        "sweep_detected":   sweep["sweep_detected"],
        "sweep_depth":      sweep["wick_depth_pct"],
        "funding_rate_pct": fr_data.get("funding_rate_pct", 0.0),
        "funding_label":    fr_data.get("label", "N/A"),
        # v4 fields
        "ob_high":          ob_info.get("ob_high", 0.0),
        "ob_low":           ob_info.get("ob_low", 0.0),
        "ob_fvg":           ob_info.get("fvg", False),
        "ob_in_zone":       ob_info.get("in_ob_zone", False),
        "nearest_eqh":      liq.get("nearest_eqh"),
        "nearest_eql":      liq.get("nearest_eql"),
        "sitting_on_eql":   liq.get("sitting_on_eql", False),
        "near_eqh":         liq.get("near_eqh", False),
        "active_risk_pct":  round(active_risk * 100, 2),
        # v5 fields
        "mtf_label":        mtf["label"],
        "mtf_confluence":   mtf["mtf_confluence"],
        "daily_ob_high":    mtf.get("daily_ob_high", 0.0),
        "daily_ob_low":     mtf.get("daily_ob_low", 0.0),
        "htf_sweep":        htf_sweep["htf_sweep_detected"],
        "htf_sweep_level":  htf_sweep.get("htf_sweep_level", 0.0),
        "layer_sizes":      layer_sizes,
        "layer_2_price":    layer_2_price,
        "layer_3_price":    layer_3_price,
    }


# ════════════════════════════════════════════════════════
#  [v4 #5] MULTI-SCENARIO EXIT
#  Exit di luar TP/SL/Trailing — berdasarkan kondisi market
# ════════════════════════════════════════════════════════

def check_exit_scenarios(client, buy_price: float, current_price: float,
                          profit_pct: float) -> dict:
    """
    [v4 #5] Multi-Scenario Exit — 3 kondisi exit tambahan:

    Scenario 1 — Structure Break:
      4H market structure berubah ke BEARISH (LH/LL terkonfirmasi).
      Market telah secara fundamental berbalik arah.
      Hanya exit jika masih profit > EXIT_STRUCTURE_MIN_PROFIT%.
      Rasional: tidak ada alasan hold posisi long di bearish structure.

    Scenario 2 — Momentum Loss (triple confluence):
      RSI 1H < 45 + MACD 1H crossed below + Price < EMA20 1H.
      Ketiga harus terpenuhi bersamaan (mengurangi false positive).
      Hanya exit jika masih profit > EXIT_MOMENTUM_MIN_PROFIT%.

    Scenario 3 — Funding Rate Extreme Mid-Trade:
      FR tiba-tiba melonjak ke > 0.10% saat sudah dalam posisi.
      Crowding extrem meningkatkan risiko liquidation cascade.
      Ini exit preventif sebelum crowding memicu reversal besar.

    Mengapa exit "smart" ini penting:
    Trailing stop dan TP adalah target statis. Tapi market berubah
    secara dinamis. Scenario exit bereaksi terhadap kondisi real-time,
    bukan hanya level harga.
    """
    default = {"should_exit": False, "reason": "", "scenario": None}
    try:
        data_4h = get_candles(client, "4h", 50)
        data_1h = get_candles(client, "1h", 30)

        if data_4h is None or data_1h is None:
            return default

        closes_4h, highs_4h, lows_4h, _, _ = data_4h
        closes_1h, _, _, _, _               = data_1h

        struct   = detect_structure(closes_4h, highs_4h, lows_4h, lookback=30)
        rsi_1h   = calc_rsi(closes_1h)
        macd_1h, sig_1h = calc_macd(closes_1h)
        ema20_1h = calc_ema(closes_1h, 20)

        # Scenario 1: Structure Break
        if struct["bias"] == "BEARISH" and profit_pct > EXIT_STRUCTURE_MIN_PROFIT:
            return {
                "should_exit": True,
                "reason":      f"4H Structure → BEARISH (LH/LL) | PnL:{profit_pct:.1f}%",
                "scenario":    "STRUCTURE_BREAK",
            }

        # Scenario 2: Momentum Loss (semua 3 harus terpenuhi)
        momentum_dead = (
            rsi_1h < EXIT_RSI_THRESHOLD and
            macd_1h < sig_1h and
            closes_1h[-1] < ema20_1h
        )
        if momentum_dead and profit_pct > EXIT_MOMENTUM_MIN_PROFIT:
            return {
                "should_exit": True,
                "reason":      f"Momentum Dead: RSI:{rsi_1h:.0f} MACD↓ <EMA20 | PnL:{profit_pct:.1f}%",
                "scenario":    "MOMENTUM_LOSS",
            }

        # Scenario 3: Funding Rate jadi extreme mid-trade
        fr_data = get_funding_rate_oi()
        if fr_data["ok"]:
            fr_score, fr_note = calc_funding_rate_score(fr_data["funding_rate_pct"])
            if fr_score == -99 and profit_pct > EXIT_MOMENTUM_MIN_PROFIT:
                return {
                    "should_exit": True,
                    "reason":      f"FR Extreme mid-trade: {fr_note} | PnL:{profit_pct:.1f}%",
                    "scenario":    "FUNDING_EXTREME",
                }

    except Exception as e:
        log(f"⚠️ Exit scenario error: {e}")

    return default


def is_still_bullish(client) -> tuple:
    """Cek apakah BTC masih bullish — dipakai saat hold untuk keputusan TP."""
    try:
        data_1h = get_candles(client, "1h", 50)
        data_4h = get_candles(client, "4h", 50)
        if data_1h is None or data_4h is None:
            return False, 0.0

        closes_1h, highs_1h, lows_1h, vols_1h, _ = data_1h
        closes_4h, _, _, _, _                     = data_4h

        rsi_1h       = calc_rsi(closes_1h)
        rsi_4h       = calc_rsi(closes_4h)
        ema20_1h     = calc_ema(closes_1h, 20)
        ema50_1h     = calc_ema(closes_1h, 50)
        macd_1h, sig = calc_macd(closes_1h)
        vol_avg      = float(np.mean(vols_1h[-11:-1]))
        vol_ok       = float(vols_1h[-1]) > vol_avg * 0.6

        bullish = (
            rsi_1h < 72 and rsi_4h < 75 and
            ema20_1h > ema50_1h and
            macd_1h > sig and vol_ok
        )
        return bullish, rsi_1h
    except Exception as e:
        log(f"⚠️ is_still_bullish error: {e}")
        return False, 0.0


# ════════════════════════════════════════════════════════
#  ORDER EXECUTION
# ════════════════════════════════════════════════════════

def get_btc_precision(client) -> tuple:
    try:
        pairs = gate_retry(client.list_currency_pairs)
        if pairs:
            for p in pairs:
                if p.id == PAIR:
                    return float(p.min_base_amount or 0.00001), int(p.amount_precision or 6)
    except Exception:
        pass
    return 0.00001, 6


def do_buy(client, order_usdt: float) -> tuple:
    """Market order — fallback dari limit order."""
    # [FIX] Guard: gate_retry bisa return None jika semua retry gagal → TypeError jika langsung [0]
    _tickers = gate_retry(client.list_tickers, currency_pair=PAIR)
    if not _tickers:
        raise Exception("Ticker gagal — tidak bisa ambil harga")
    price = float(_tickers[0].last or 0)
    if price <= 0:
        raise Exception("Harga BTC tidak valid")
    min_amount, precision = get_btc_precision(client)
    amount = round(order_usdt / price, precision)
    if min_amount > 0 and amount < min_amount:
        raise Exception(f"Amount {amount} < min {min_amount}")
    log(f"MARKET BUY {PAIR} | {amount} BTC @ ${price:,.2f}")
    # [FIX] Gate.io Spot market order harus pakai time_in_force="ioc"
    # Jika dikosongkan, gate_api library bisa inject default "gtc" yang tidak
    # didukung untuk market order → error "TimeInForce gtc is not support for market order"
    order  = gate_api.Order(
        currency_pair=PAIR,
        type="market",
        side="buy",
        amount=str(amount),
        time_in_force="ioc",
    )
    result = gate_retry(client.create_order, order)
    if result is None:
        raise Exception("Order gagal")
    buy_price = float(result.avg_deal_price or price)
    filled    = float(result.filled_total and float(result.filled_total) / buy_price or amount)
    log(f"✅ Market filled: {filled} BTC @ ${buy_price:,.2f}")
    return buy_price, filled


def do_buy_limit(client, order_usdt: float) -> tuple:
    """
    [v3 #4] Limit order 0.05% di bawah market → timeout 45s → fallback market.
    Partial fill diterima sebelum fallback.
    """
    ticker = gate_retry(client.list_tickers, currency_pair=PAIR)
    if not ticker:
        raise Exception("Ticker gagal")
    last_price = float(ticker[0].last or 0)
    if last_price <= 0:
        raise Exception("Harga BTC tidak valid")

    min_amount, precision = get_btc_precision(client)
    limit_price = round(last_price * (1 - LIMIT_ORDER_OFFSET_PCT), 2)
    amount      = round(order_usdt / limit_price, precision)
    if min_amount > 0 and amount < min_amount:
        raise Exception(f"Amount {amount} < min {min_amount}")

    log(f"LIMIT BUY {PAIR} | {amount} BTC @ ${limit_price:,.2f} (-{LIMIT_ORDER_OFFSET_PCT*100:.2f}%)")
    limit_order = gate_api.Order(
        currency_pair=PAIR, type="limit", side="buy",
        amount=str(amount), price=str(limit_price), time_in_force="gtc",
    )
    result = gate_retry(client.create_order, limit_order)
    if result is None:
        raise Exception("Limit order gagal")

    order_id = getattr(result, "id", None)
    # [FIX] Normalkan order_id: pastikan string non-kosong sebelum dipakai
    if order_id is not None:
        order_id = str(order_id).strip()
    if not order_id:
        # Gate.io kadang tidak return ID — fallback langsung ke market
        log("⚠️ Order ID kosong dari Gate.io → fallback market")
        return do_buy(client, order_usdt)
    log(f"⏳ Limit order {order_id} — tunggu fill {LIMIT_ORDER_TIMEOUT}s...")

    deadline = time.time() + LIMIT_ORDER_TIMEOUT
    while time.time() < deadline:
        time.sleep(5)
        try:
            status = gate_retry(client.get_order, PAIR, order_id)
        except Exception:
            status = None
        if status is None:
            break
        if status.status == "closed":
            buy_price = float(status.avg_deal_price or limit_price)
            filled    = float(status.amount or amount)
            log(f"✅ Limit filled: {filled} BTC @ ${buy_price:,.2f}")
            return buy_price, filled
        if status.status == "cancelled":
            log("⚠️ Limit cancelled externally → fallback market")
            break

    # [FIX] Guard cancel: hanya cancel jika order_id valid (non-empty string)
    # Error "Empty order ID, BTC_USDT" terjadi karena cancel dipanggil dengan ID kosong
    if order_id:
        try:
            gate_retry(client.cancel_order, PAIR, order_id)
            log(f"❌ Limit timeout — cancel {order_id}")
        except Exception as e:
            log(f"⚠️ Cancel error: {e}")
    else:
        log("⚠️ Skip cancel — order ID tidak valid")

    try:
        status = gate_retry(client.get_order, PAIR, order_id)
        if status:
            left          = float(getattr(status, "left", 0) or 0)
            partial_fill  = round(amount - left, precision)
            if partial_fill > (min_amount or 0.00001):
                buy_price = float(status.avg_deal_price or limit_price)
                log(f"⚠️ Partial fill: {partial_fill} BTC @ ${buy_price:,.2f}")
                return buy_price, partial_fill
    except Exception:
        pass

    log("🔄 Fallback ke market order...")
    return do_buy(client, order_usdt)


def do_buy_layered(client, analysis: dict) -> tuple:
    """
    [v5 #3] Layered Position Entry — Scaling In.

    Pro trading desks tidak masuk dengan satu order besar karena:
    1. Slippage lebih tinggi
    2. Cost basis tidak optimal jika harga turun lebih dulu
    3. Tidak bisa average down di zona konfirmasi berikutnya

    Strategi 3-layer:
    - Layer 1 (40%): Entry langsung di harga saat ini (OB zone / dekat)
      → Limit order offset -0.05%
    - Layer 2 (35%): Entry di FVG low (jika ada) atau -0.5% dari Layer 1
      → Ini target retest gap sebelum melanjutkan naik
    - Layer 3 (25%): Entry di EQL daily/4H atau -1.2% dari Layer 1
      → Ini zona final sebelum SL — area dengan confluence paling kuat

    Jika layer 2 atau 3 tidak terisi dalam LIMIT_ORDER_TIMEOUT detik,
    bot tetap maju dengan layer yang sudah terisi (tidak tunggu semua).

    Return: (avg_buy_price, total_amount_filled)
    VWAP dari semua layer yang terisi = cost basis optimal.
    """
    layer_sizes   = analysis.get("layer_sizes", [])
    layer_2_price = analysis.get("layer_2_price")
    layer_3_price = analysis.get("layer_3_price")

    if not LAYER_ENABLED or not layer_sizes:
        # Fallback ke single limit order
        return do_buy_limit(client, analysis["order_usdt"])

    min_amount, precision = get_btc_precision(client)

    filled_amounts = []
    filled_prices  = []

    # ── Layer 1: Masuk di market / limit offset ───────
    log(f"  [L1] Layer 1: ${layer_sizes[0]:.2f} USDT @ limit -0.05%")
    try:
        p1, a1 = do_buy_limit(client, layer_sizes[0])
        filled_amounts.append(a1)
        filled_prices.append(p1)
        log(f"  [L1] ✅ Layer 1 filled: {a1} BTC @ ${p1:,.2f}")
    except Exception as e:
        log(f"  [L1] ❌ Layer 1 gagal: {e}")
        # Jika layer 1 gagal, abort semua
        raise

    # ── Layer 2: Limit di FVG / discount zone ─────────
    if len(layer_sizes) >= 2 and layer_2_price:
        log(f"  [L2] Layer 2: ${layer_sizes[1]:.2f} USDT @ ${layer_2_price:,.2f}")
        try:
            amount2 = round(layer_sizes[1] / layer_2_price, precision)
            if amount2 >= (min_amount or 0.00001):
                order2 = gate_api.Order(
                    currency_pair=PAIR, type="limit", side="buy",
                    amount=str(amount2), price=str(layer_2_price),
                    time_in_force="gtc",
                )
                res2    = gate_retry(client.create_order, order2)
                res2_id = getattr(res2, "id", None) or "" if res2 else ""
                if res2 and res2_id:
                    deadline2 = time.time() + 30
                    l2_filled = False
                    while time.time() < deadline2:
                        time.sleep(5)
                        try:
                            st2 = gate_retry(client.get_order, PAIR, res2_id)
                        except Exception:
                            st2 = None
                        if st2 and st2.status == "closed":
                            p2 = float(st2.avg_deal_price or layer_2_price)
                            a2 = float(st2.amount or amount2)
                            filled_amounts.append(a2)
                            filled_prices.append(p2)
                            log(f"  [L2] ✅ Layer 2 filled: {a2} BTC @ ${p2:,.2f}")
                            l2_filled = True
                            break
                    if not l2_filled:
                        try:
                            gate_retry(client.cancel_order, PAIR, res2_id)
                        except Exception:
                            pass
                        log(f"  [L2] ⏭ Layer 2 timeout → skip")
                else:
                    log(f"  [L2] ⚠️ Order ID kosong → skip layer 2")
        except Exception as e:
            log(f"  [L2] ⚠️ Layer 2 error: {e} → skip")

    # ── Layer 3: Limit di EQL / deeper discount ───────
    if len(layer_sizes) >= 3 and layer_3_price:
        log(f"  [L3] Layer 3: ${layer_sizes[2]:.2f} USDT @ ${layer_3_price:,.2f}")
        try:
            amount3 = round(layer_sizes[2] / layer_3_price, precision)
            if amount3 >= (min_amount or 0.00001):
                order3 = gate_api.Order(
                    currency_pair=PAIR, type="limit", side="buy",
                    amount=str(amount3), price=str(layer_3_price),
                    time_in_force="gtc",
                )
                res3    = gate_retry(client.create_order, order3)
                res3_id = getattr(res3, "id", None) or "" if res3 else ""
                if res3 and res3_id:
                    deadline3 = time.time() + 30
                    l3_filled = False
                    while time.time() < deadline3:
                        time.sleep(5)
                        try:
                            st3 = gate_retry(client.get_order, PAIR, res3_id)
                        except Exception:
                            st3 = None
                        if st3 and st3.status == "closed":
                            p3 = float(st3.avg_deal_price or layer_3_price)
                            a3 = float(st3.amount or amount3)
                            filled_amounts.append(a3)
                            filled_prices.append(p3)
                            log(f"  [L3] ✅ Layer 3 filled: {a3} BTC @ ${p3:,.2f}")
                            l3_filled = True
                            break
                    if not l3_filled:
                        try:
                            gate_retry(client.cancel_order, PAIR, res3_id)
                        except Exception:
                            pass
                        log(f"  [L3] ⏭ Layer 3 timeout → skip")
                else:
                    log(f"  [L3] ⚠️ Order ID kosong → skip layer 3")
        except Exception as e:
            log(f"  [L3] ⚠️ Layer 3 error: {e} → skip")

    # ── VWAP cost basis ───────────────────────────────
    total_amount = sum(filled_amounts)
    if total_amount <= 0:
        raise Exception("Semua layer gagal — tidak ada BTC yang dibeli")

    # VWAP: weighted average price dari semua layer yang terisi
    vwap_price = sum(p * a for p, a in zip(filled_prices, filled_amounts)) / total_amount
    layers_filled = len(filled_amounts)

    log(f"  [LAYER] Total: {total_amount:.6f} BTC | VWAP: ${vwap_price:,.2f} | "
        f"{layers_filled}/{min(MAX_LAYERS, len(layer_sizes))} layers filled")

    return round(vwap_price, 2), round(total_amount, 6)


def do_sell(client, amount: float, label: str = "SELL") -> float:
    btc_bal      = get_btc_balance(client)
    _, precision = get_btc_precision(client)
    if btc_bal <= 0:
        raise Exception("Saldo BTC kosong")
    sell_amount = round(min(amount, btc_bal), precision)
    # [FIX] Guard: gate_retry bisa return None → crash jika langsung [0]
    _tickers = gate_retry(client.list_tickers, currency_pair=PAIR)
    if not _tickers:
        raise Exception("Ticker gagal saat sell — tidak bisa ambil harga")
    price = float(_tickers[0].last or 0)
    log(f"{label} {PAIR} | {sell_amount} BTC @ ${price:,.2f}")
    # [FIX] Sama seperti do_buy — market order harus pakai "ioc" bukan default
    order  = gate_api.Order(
        currency_pair=PAIR,
        type="market",
        side="sell",
        amount=str(sell_amount),
        time_in_force="ioc",
    )
    result = gate_retry(client.create_order, order)
    if result is None:
        raise Exception("Sell order gagal")
    sell_price = float(result.avg_deal_price or price)
    log(f"✅ Sold {sell_amount} BTC @ ${sell_price:,.2f}")
    return sell_price


# ════════════════════════════════════════════════════════
#  TELEGRAM MESSAGES
# ════════════════════════════════════════════════════════

def _ob_label(analysis: dict) -> str:
    if not analysis.get("ob_high", 0):
        return "—"
    zone = f"${analysis['ob_low']:,.0f}–${analysis['ob_high']:,.0f}"
    fvg  = " +FVG" if analysis.get("ob_fvg") else ""
    pin  = " 📍IN ZONE" if analysis.get("ob_in_zone") else ""
    return f"{zone}{fvg}{pin}"


def _demand_label(strength: int, validated: bool) -> str:
    if not validated:
        return "⚠️ Weak (unvalidated)"
    return "✅✅ Strong" if strength == 2 else "✅ Valid (1 criterion)"


def msg_buy(buy_price, amount, analysis, idr_rate):
    tp1 = analysis["tp1_price"]
    tp2 = analysis["tp2_price"]
    tp3 = analysis["tp3_price"]
    sl  = analysis["sl_price"]

    eqh_txt = f"${analysis['nearest_eqh']:,.0f}" if analysis.get("nearest_eqh") else "—"
    eql_txt = f"${analysis['nearest_eql']:,.0f}" if analysis.get("nearest_eql") else "—"
    mtf_txt = analysis.get("mtf_label", "—")
    htf_txt = f"✅ ${analysis.get('htf_sweep_level',0):,.0f}" if analysis.get("htf_sweep") else "—"
    layers  = analysis.get("layer_sizes", [])
    layer_txt = (
        f"L1:${layers[0]:.1f} | L2:${layers[1]:.1f} | L3:${layers[2]:.1f}"
        if len(layers) == 3 else "Single"
    )

    return (
        f"🟢 <b>BTC BUY EXECUTED v5</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Entry  : <b>${buy_price:,.2f}</b> <i>≈ {idr_fmt(buy_price, idr_rate)}</i>\n"
        f"Amount : <b>{amount} BTC</b> | Modal: <b>${analysis['order_usdt']:.2f}</b>\n"
        f"Risk   : <b>{analysis['active_risk_pct']:.2f}%</b> equity (ECC)\n"
        f"Layers : {layer_txt}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"TP1 : <b>${tp1:,.2f}</b> (+5%) — <i>jual 50%</i>\n"
        f"TP2 : <b>${tp2:,.2f}</b> (+10%)\n"
        f"TP3 : <b>${tp3:,.2f}</b> (+20%)\n"
        f"SL  : <b>${sl:,.2f}</b> (-{analysis['sl_pct']}%) | R/R: <b>1:{analysis['rr']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Score   : {analysis['score']} | Struct: {analysis['struct_4h']}\n"
        f"MTF     : {mtf_txt}\n"
        f"OB Zone : {_ob_label(analysis)}\n"
        f"EQH↑    : {eqh_txt} | EQL↓: {eql_txt}\n"
        f"HTFSwp  : {htf_txt}\n"
        f"Demand  : {_demand_label(analysis.get('demand_strength',0), analysis.get('demand_validated',False))}\n"
        f"Sweep   : {'✅' if analysis.get('sweep_detected') else '—'} | "
        f"FR: {analysis.get('funding_label','N/A')}\n"
        f"Regime  : {analysis.get('vol_regime','—')} ({analysis.get('vol_atr_pct',0):.3f}%)\n"
        f"Signal  : {analysis['notes']}\n"
        f"<i>⚠️ Bot akan jual otomatis di TP/SL/Scenario exit.</i>"
    )


def msg_sell(reason, buy_price, sell_price, amount, profit, profit_pct,
             idr_rate, partial=False):
    emoji    = "✅" if profit >= 0 else "❌"
    part_txt = " (50% posisi)" if partial else ""
    return (
        f"{emoji} <b>{reason}</b>{part_txt}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Buy   : <b>${buy_price:,.2f}</b>\n"
        f"Sell  : <b>${sell_price:,.2f}</b> <i>≈ {idr_fmt(sell_price, idr_rate)}</i>\n"
        f"Amount: {amount} BTC\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"PnL   : <b>{'+'if profit>=0 else ''}{profit:.4f} USDT ({profit_pct:+.2f}%)</b>\n"
        f"<i>{'Sisa 50% posisi masih hold.' if partial else 'Bot siap cari entry berikutnya.'}</i>"
    )


def msg_scenario_exit(reason, buy_price, sell_price, amount, profit, profit_pct,
                      idr_rate, scenario: str):
    emoji = "🔵"
    icons = {
        "STRUCTURE_BREAK": "📉",
        "MOMENTUM_LOSS":   "⚡",
        "FUNDING_EXTREME": "💸",
    }
    icon = icons.get(scenario, "🔵")
    return (
        f"{icon} <b>SMART EXIT — {scenario}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Alasan : {reason}\n"
        f"Buy    : <b>${buy_price:,.2f}</b>\n"
        f"Sell   : <b>${sell_price:,.2f}</b> <i>≈ {idr_fmt(sell_price, idr_rate)}</i>\n"
        f"Amount : {amount} BTC\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"PnL    : <b>{'+'if profit>=0 else ''}{profit:.4f} USDT ({profit_pct:+.2f}%)</b>\n"
        f"<i>Exit berbasis kondisi market — bukan TP/SL level.</i>"
    )


def msg_hold(buy_price, current_price, peak, profit_pct,
             tp1, tp2, tp3, sl, idr_rate, remaining_amount):
    emoji = "📈" if profit_pct >= 0 else "📉"
    return (
        f"{emoji} <b>Update Posisi BTC</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Buy    : ${buy_price:,.2f}\n"
        f"Now    : <b>${current_price:,.2f}</b> <i>≈ {idr_fmt(current_price, idr_rate)}</i>\n"
        f"Peak   : ${peak:,.2f} | Amount: {remaining_amount} BTC\n"
        f"PnL    : <b>{profit_pct:+.2f}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"TP2 : ${tp2:,.2f} (+10%) | TP3 : ${tp3:,.2f} (+20%)\n"
        f"SL  : ${sl:,.2f}"
    )


def send_daily_report(idr_rate):
    """[v2 #9] Daily PnL report jam 08:00 WIB."""
    try:
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        res = supabase.table("trade_history") \
            .select("profit, result, partial") \
            .gte("closed_at", f"{today_utc}T00:00:00+00:00") \
            .execute()

        trades     = res.data
        total_pnl  = sum(float(t["profit"] or 0) for t in trades)
        wins       = sum(1 for t in trades if float(t["profit"] or 0) > 0 and not t.get("partial"))
        losses     = sum(1 for t in trades if float(t["profit"] or 0) < 0)
        total_full = wins + losses
        winrate    = (wins / total_full * 100) if total_full > 0 else 0
        emoji      = "✅" if total_pnl >= 0 else "❌"

        tg(
            f"📊 <b>Daily Report BTC Bot v4</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Tanggal : {datetime.now(WIB).strftime('%d %b %Y')}\n"
            f"Total Trade : {total_full} | W:{wins} L:{losses}\n"
            f"Winrate : <b>{winrate:.1f}%</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Total PnL : {emoji} <b>{'+'if total_pnl>=0 else ''}{total_pnl:.4f} USDT</b>\n"
            f"<i>≈ {idr_fmt(abs(total_pnl), idr_rate)}</i>"
        )
    except Exception as e:
        log(f"⚠️ Daily report error: {e}")


# ════════════════════════════════════════════════════════
#  MAIN RUN
# ════════════════════════════════════════════════════════

def run():
    log("=" * 55)
    log("🚀 BTC TRADING BOT v5.2 START")
    log("=" * 55)

    client   = setup_client()
    idr_rate = get_usdt_idr_rate()
    log(f"💱 Kurs USD/IDR: Rp{idr_rate:,.0f}")

    balance = get_usdt_balance(client)
    log(f"💰 Balance USDT: ${balance:.2f}")

    now_wib = datetime.now(WIB)
    if now_wib.hour == 8 and now_wib.minute < 30:
        send_daily_report(idr_rate)

    # [v5.2] Auto-recover orphan position sebelum load
    # Jika ada BTC di wallet tapi tidak ada posisi di Supabase → recover otomatis
    auto_recover_orphan_position(client)

    position = load_position()

    # ══════════════════════════════════════════════
    #  HOLD MODE
    # ══════════════════════════════════════════════
    if position:
        buy_price = float(position.get("buy_price") or 0)
        amount    = float(position.get("amount") or 0)
        peak      = float(position.get("peak_price") or buy_price)
        tp1_hit   = bool(position.get("tp1_hit", False))
        tp2_hit   = bool(position.get("tp2_hit", False))
        sl_price  = float(position.get("sl_price") or buy_price * (1 - 0.025))
        pos_notes = position.get("notes", "")

        if amount <= 0 or buy_price <= 0:
            log("⚠️ Posisi invalid — clear")
            clear_position()
            return

        btc_bal = get_btc_balance(client)
        if btc_bal <= 0:
            log("⚠️ Saldo BTC kosong — clear")
            tg("⚠️ Posisi BTC di-clear karena saldo BTC kosong.")
            clear_position()
            return

        ticker = gate_retry(client.list_tickers, currency_pair=PAIR)
        if not ticker:
            log("⚠️ Gagal ambil harga — skip")
            return
        current_price = float(ticker[0].last or 0)
        if current_price <= 0:
            return

        peak       = max(peak, current_price)
        trailing   = peak * (1 - TRAILING_GAP)
        tp1_price  = buy_price * (1 + TP1_PCT)
        tp2_price  = buy_price * (1 + TP2_PCT)
        tp3_price  = buy_price * (1 + TP3_PCT)
        profit_pct = (current_price / buy_price - 1) * 100

        log(f"HOLD BTC | Price:${current_price:,.2f} | PnL:{profit_pct:+.2f}%")
        log(f"  SL:${sl_price:,.2f} Trail:${trailing:,.2f} TP1:${tp1_price:,.2f}")

        # ── STOP LOSS ─────────────────────────────────
        if current_price <= sl_price:
            sell_price = do_sell(client, amount, "STOP LOSS")
            profit     = round((sell_price - buy_price) * amount, 4)
            pct        = (sell_price / buy_price - 1) * 100
            save_trade(buy_price, sell_price, amount, "STOP LOSS", notes=pos_notes)
            clear_position()
            set_cooldown(COOLDOWN_SL)
            tg(msg_sell("🔴 STOP LOSS", buy_price, sell_price, amount, profit, pct, idr_rate))
            log(f"STOP LOSS | Profit: ${profit:.4f}")
            return

        # ── TRAILING STOP — aktif setelah TP1 ────────
        if current_price <= trailing and tp1_hit:
            sell_price = do_sell(client, amount, "TRAILING STOP")
            profit     = round((sell_price - buy_price) * amount, 4)
            pct        = (sell_price / buy_price - 1) * 100
            save_trade(buy_price, sell_price, amount, "TRAILING STOP", notes=pos_notes)
            clear_position()
            tg(msg_sell("🔴 TRAILING STOP", buy_price, sell_price, amount, profit, pct, idr_rate))
            log(f"TRAILING STOP | Profit: ${profit:.4f}")
            return

        # ── [v4 #5] MULTI-SCENARIO EXIT ───────────────
        # Diperiksa setelah SL/Trailing tapi sebelum TP
        # Hanya aktif jika dalam kondisi profit (threshold per scenario)
        exit_check = check_exit_scenarios(
            client, buy_price, current_price, profit_pct
        )
        if exit_check["should_exit"]:
            sell_price = do_sell(client, amount, exit_check["scenario"])
            profit     = round((sell_price - buy_price) * amount, 4)
            pct        = (sell_price / buy_price - 1) * 100
            save_trade(buy_price, sell_price, amount, exit_check["scenario"])
            clear_position()
            set_cooldown(COOLDOWN_SMART_EXIT)
            tg(msg_scenario_exit(
                exit_check["reason"], buy_price, sell_price, amount,
                profit, pct, idr_rate, exit_check["scenario"]
            ))
            log(f"SMART EXIT [{exit_check['scenario']}] | Profit: ${profit:.4f}")
            return

        # ── TP3 ──────────────────────────────────────
        if current_price >= tp3_price:
            still_bull, rsi_now = is_still_bullish(client)
            if still_bull:
                update_position({"peak_price": peak})
                log(f"TP3 zone +{profit_pct:.1f}% — masih bullish RSI:{rsi_now:.1f}, hold...")
                tg(f"⚡ BTC +{profit_pct:.1f}% — RSI:{rsi_now:.1f} kuat, hold TP3!")
            else:
                sell_price = do_sell(client, amount, "TP3")
                profit     = round((sell_price - buy_price) * amount, 4)
                pct        = (sell_price / buy_price - 1) * 100
                save_trade(buy_price, sell_price, amount, "TP3 EXIT")
                clear_position()
                tg(msg_sell("✅ TP3 EXIT", buy_price, sell_price, amount, profit, pct, idr_rate))
                log(f"TP3 EXIT | Profit: ${profit:.4f}")
            return

        # ── TP2 ──────────────────────────────────────
        if current_price >= tp2_price and not tp2_hit:
            still_bull, rsi_now = is_still_bullish(client)
            if still_bull:
                update_position({"peak_price": peak, "tp2_hit": True})
                log(f"TP2 zone +{profit_pct:.1f}% — masih bullish, hold ke TP3...")
                tg(f"🏆 BTC TP2 +{profit_pct:.1f}% — RSI:{rsi_now:.1f} kuat, hold ke TP3!")
            else:
                sell_price = do_sell(client, amount, "TP2")
                profit     = round((sell_price - buy_price) * amount, 4)
                pct        = (sell_price / buy_price - 1) * 100
                save_trade(buy_price, sell_price, amount, "TP2 EXIT")
                clear_position()
                tg(msg_sell("✅ TP2 EXIT", buy_price, sell_price, amount, profit, pct, idr_rate))
                log(f"TP2 EXIT | Profit: ${profit:.4f}")
            return

        # ── TP1 — Partial exit 50% ───────────────────
        if current_price >= tp1_price and not tp1_hit:
            still_bull, rsi_now = is_still_bullish(client)

            partial_amount = round(amount * TP1_SELL_RATIO, 6)
            sell_price     = do_sell(client, partial_amount, "TP1 PARTIAL")
            partial_profit = round((sell_price - buy_price) * partial_amount, 4)
            partial_pct    = (sell_price / buy_price - 1) * 100
            save_trade(buy_price, sell_price, partial_amount, "TP1 PARTIAL", partial=True)
            tg(msg_sell("✅ TP1 PARTIAL", buy_price, sell_price,
                        partial_amount, partial_profit, partial_pct, idr_rate, partial=True))

            remaining = round(amount - partial_amount, 6)

            if still_bull and remaining > 0:
                update_position({"peak_price": peak, "tp1_hit": True, "amount": remaining})
                log(f"TP1 +{profit_pct:.1f}% — jual 50%, hold {remaining} BTC ke TP2. RSI:{rsi_now:.1f}")
                tg(f"🥇 BTC TP1 +{profit_pct:.1f}% — 50% sold, sisa {remaining} BTC → TP2!")
            else:
                profit2 = 0.0
                if remaining > 0:
                    sell_price2 = do_sell(client, remaining, "TP1 FULL")
                    profit2     = round((sell_price2 - buy_price) * remaining, 4)
                    save_trade(buy_price, sell_price2, remaining, "TP1 FULL EXIT")
                clear_position()
                log(f"TP1 FULL EXIT | Total: ~${partial_profit + profit2:.4f}")
            return

        # ── HOLDING ───────────────────────────────────
        update_position({"peak_price": peak})
        log(f"Holding | Peak:${peak:,.2f} | PnL:{profit_pct:+.2f}% | {amount} BTC")

        if now_wib.minute < 30:
            tg(msg_hold(
                buy_price, current_price, peak, profit_pct,
                tp1_price, tp2_price, tp3_price, sl_price,
                idr_rate, amount
            ))
        return

    # ══════════════════════════════════════════════
    #  ENTRY MODE
    # ══════════════════════════════════════════════
    log("📊 Tidak ada posisi — cek entry BTC...")

    daily_pnl = get_daily_pnl()
    log(f"📉 Daily PnL: ${daily_pnl:.4f}")
    if daily_pnl <= -MAX_DAILY_LOSS:
        log(f"⛔ Max daily loss (${daily_pnl:.2f}) — stop hari ini")
        tg(
            f"⛔ <b>Max Daily Loss Tercapai</b>\n"
            f"Loss hari ini: ${abs(daily_pnl):.2f}\n"
            f"Bot berhenti trading sampai besok."
        )
        return

    cooldown = get_cooldown()
    if cooldown > 0:
        decrement_cooldown()
        log(f"⏳ Cooldown aktif ({cooldown} cycle) — skip entry")
        return

    if check_crash(client):
        tg(
            f"🛑 <b>BTC Crash Detected</b>\n"
            f"Drop > {abs(BTC_CRASH_1H)}% dalam 1 jam.\n"
            f"Entry diblokir sampai kondisi stabil."
        )
        return

    if balance < MIN_ORDER_USDT:
        log(f"⚠️ Balance ${balance:.2f} terlalu kecil")
        return

    analysis = analyze_btc(client, balance)

    if not analysis["valid"]:
        log(f"⛔ Entry skip: {analysis['reason']}")
        tg(
            f"🔍 <b>BTC Scan — No Entry</b>\n"
            f"Score: {analysis.get('score', 0)} | Regime: {analysis.get('regime', '—')}\n"
            f"Reason: {analysis['reason']}\n"
            f"{analysis.get('notes', '')}\n"
            f"<i>Scan berikutnya dalam 1 jam.</i>"
        )
        return

    log(f"✅ Entry! Score:{analysis['score']} RR:1:{analysis['rr']} "
        f"Risk:{analysis['active_risk_pct']}% Size:${analysis['order_usdt']}")

    try:
        buy_price, amount = do_buy_layered(client, analysis)
        if buy_price <= 0 or amount <= 0:
            log("⚠️ Order tidak valid")
            return

        sl_actual = round(buy_price * (1 - analysis["sl_pct"] / 100), 2)

        save_position({
            "pair":       PAIR,
            "buy_price":  buy_price,
            "amount":     amount,
            "peak_price": buy_price,
            "sl_price":   sl_actual,
            "status":     "open",
            "tp1_hit":    False,
            "tp2_hit":    False,
            "notes":      analysis.get("notes", ""),
        })

        analysis["tp1_price"]  = round(buy_price * (1 + TP1_PCT), 2)
        analysis["tp2_price"]  = round(buy_price * (1 + TP2_PCT), 2)
        analysis["tp3_price"]  = round(buy_price * (1 + TP3_PCT), 2)
        analysis["sl_price"]   = sl_actual
        analysis["order_usdt"] = round(amount * buy_price, 2)

        tg(msg_buy(buy_price, amount, analysis, idr_rate))
        log(f"🟢 BUY @ ${buy_price:,.2f} | {amount} BTC | "
            f"SL:${sl_actual:,.2f} TP1:${analysis['tp1_price']:,.2f}")

    except Exception as e:
        log(f"❌ Buy gagal: {e}")
        tg(f"❌ <b>BTC Buy Gagal</b>\n{e}")


if __name__ == "__main__":
    run()
