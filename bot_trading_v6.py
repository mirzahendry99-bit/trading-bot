"""
╔══════════════════════════════════════════════════════════════════╗
║         ALTCOIN TRADING BOT v6.1 — Integrated Scanner           ║
║                                                                  ║
║  Arsitektur v6.1 (Integrated):                                   ║
║  - Scanner : Built-in scoring engine dari Signal Bot Lite v1.4.2 ║
║              EMA, MACD, ADX, RSI, ATR, accumulation detection   ║
║              Multi-timeframe (1h + 4h confirmation)              ║
║  - Entry   : Langsung dari hasil scan — zero latency             ║
║  - Pair    : Semua pair USDT di Gate.io (vol > 150K USDT/hari)  ║
║  - Order   : Market order IOC (BUY)                             ║
║  - Exit    : TP1 (partial 50%) → TP2 (full) → SL               ║
║              SL geser ke breakeven setelah TP1 hit              ║
║  - Risk    : Dynamic risk % dari equity curve (ECC)             ║
║  - Safety  : Max daily loss, cooldown, BTC crash guard          ║
║              BLOCK_HOURS 23:00-06:00 WIB (low WR hours)        ║
║  - Recover : Auto-recover orphan position per pair              ║
║                                                                  ║
║  Flow per run (GitHub Actions setiap 10 menit):                 ║
║  1. Evaluasi open positions → SL/TP                             ║
║  2. Safety checks (BTC, daily loss, cooldown, block hours)      ║
║  3. Scan semua pair → score → langsung eksekusi entry           ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import json
import time
import math
import urllib.request
import urllib.parse
import numpy as np
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

# ── Signal Bot Supabase — opsional, hanya untuk winrate per pair ────────
# Kalau tidak di-set, winrate lookup di-skip (tidak error).
_sig_url = os.environ.get("SIGNAL_SUPABASE_URL", "")
_sig_key = os.environ.get("SIGNAL_SUPABASE_KEY", "")
supabase_signal = create_client(_sig_url, _sig_key) if (_sig_url and _sig_key) else None

BOT_VERSION = "6.1.0"
WIB         = timezone(timedelta(hours=7))

# ════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════

# ── Risk management ──────────────────────────────────────
INITIAL_EQUITY_USDT = float(os.environ.get("INITIAL_EQUITY_USDT") or "17")
RISK_PCT_DEFAULT        = 0.05
RISK_PCT_FLOOR          = 0.03
RISK_PCT_CAP            = 0.08
EQUITY_LOOKBACK         = 5
MIN_ORDER_USDT          = 10.0
MAX_ORDER_USDT          = 12.0    # buffer untuk fee & reserved balance Gate.io

# ── TP1 partial exit ─────────────────────────────────────
TP1_SELL_RATIO          = 0.50

# ── Safety guards ─────────────────────────────────────────
MAX_DAILY_LOSS          = 5.0
MAX_OPEN_POSITIONS      = 1
COOLDOWN_SL_CYCLES      = 3
COOLDOWN_SMART_CYCLES   = 2
BTC_CRASH_THRESHOLD     = -5.0

# ── Recover filter ────────────────────────────────────────
MIN_POSITION_VALUE_USDT = 1.0
DELISTED_TOKENS: set = {
    "TEDDY", "FLOKICEO", "URO", "SHIBAI", "REKT", "MONG",
}

# ── Scanner config (dari Signal Bot Lite v1.4.2) ──────────
MIN_VOLUME_USDT     = 150_000   # volume minimum pair per hari
MIN_SCORE           = 3.0       # score minimum untuk entry
MIN_RR              = 1.5       # risk/reward minimum
MAX_ENTRY_DEV       = 0.02      # max deviasi harga dari entry signal (2%)
MAX_SL_PCT          = 0.035     # max SL 3.5% dari entry
MIN_SL_PCT          = 0.005     # min SL 0.5% dari entry
TP1_R               = 1.5       # TP1 = SL dist × 1.5
TP2_R               = 2.5       # TP2 = SL dist × 2.5
SL_ATR_MULT         = 2.0       # SL = entry ± ATR × 2.0
ATR_SL_BUFFER       = 0.5       # SL = swing ± ATR × 0.5
ADX_TREND           = 25        # ADX ≥ 25 = trending
ADX_CHOP            = 20        # ADX < 20 = choppy
ADX_PERIOD          = 14
BTC_DROP_BLOCK      = -3.0      # BTC drop > 3% → blok BUY
BTC_CRASH_BLOCK     = -10.0     # BTC drop > 10% → halt semua
BTC_VOLATILE_1H     = 1.5       # BTC 1h range > 1.5% = volatile
BTC_RANGE_1H        = 2.5
BTC_TREND_LOOKBACK  = 4
BTC_TREND_MIN_BEARISH = 3

# Block jam dengan WR rendah (23:00–06:00 WIB)
_default_block = "23,0,1,2,3,4,5,6"
BLOCK_HOURS_WIB = set(
    int(h.strip())
    for h in os.getenv("BLOCK_HOURS_WIB", _default_block).split(",")
    if h.strip().isdigit()
)

# API health tracking
_api_failures = 0
API_FAILURE_THRESHOLD = 5
_CANDLE_FORMAT_LOGGED = False

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
#  SCANNER — TECHNICAL INDICATORS
#  Ported dari Signal Bot Lite v1.4.2
# ════════════════════════════════════════════════════════

def _track_api(success: bool) -> None:
    global _api_failures
    if success:
        _api_failures = max(0, _api_failures - 1)
    else:
        _api_failures += 1

def api_is_degraded() -> bool:
    return _api_failures >= API_FAILURE_THRESHOLD

def calc_ema(closes: list, period: int) -> float:
    if len(closes) < period:
        return closes[-1]
    k = 2.0 / (period + 1)
    ema = closes[0]
    for p in closes[1:]:
        ema = p * k + ema * (1.0 - k)
    return ema

def calc_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas   = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains    = [d if d > 0 else 0.0 for d in deltas[-period:]]
    losses   = [-d if d < 0 else 0.0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)

def calc_macd(closes: list):
    if len(closes) < 34:
        return 0.0, 0.0
    k12 = 2.0 / 13
    k26 = 2.0 / 27
    k9  = 2.0 / 10
    ema12 = closes[0]
    ema26 = closes[0]
    macd_series = []
    for price in closes:
        ema12 = price * k12 + ema12 * (1.0 - k12)
        ema26 = price * k26 + ema26 * (1.0 - k26)
        macd_series.append(ema12 - ema26)
    signal = macd_series[0]
    for v in macd_series[1:]:
        signal = v * k9 + signal * (1.0 - k9)
    return round(macd_series[-1], 8), round(signal, 8)

def calc_atr(closes: list, highs: list, lows: list, period: int = 14) -> float:
    if len(closes) < 2:
        return highs[-1] - lows[-1]
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i]  - lows[i],
            abs(highs[i]  - closes[i-1]),
            abs(lows[i]   - closes[i-1])
        )
        trs.append(tr)
    return sum(trs[-period:]) / min(period, len(trs))

def calc_adx(closes: list, highs: list, lows: list, period: int = 14) -> float:
    if len(closes) < period * 2:
        return 20.0
    plus_dm_list, minus_dm_list, tr_list = [], [], []
    for i in range(1, len(closes)):
        h_diff = highs[i]  - highs[i-1]
        l_diff = lows[i-1] - lows[i]
        plus_dm_list.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
        minus_dm_list.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
        tr_list.append(max(
            highs[i] - lows[i],
            abs(highs[i]  - closes[i-1]),
            abs(lows[i]   - closes[i-1])
        ))
    def smooth(lst):
        s = sum(lst[:period])
        result = [s]
        for v in lst[period:]:
            s = s - s / period + v
            result.append(s)
        return result
    sm_tr    = smooth(tr_list)
    sm_plus  = smooth(plus_dm_list)
    sm_minus = smooth(minus_dm_list)
    dx_list  = []
    for i in range(len(sm_tr)):
        if sm_tr[i] == 0:
            continue
        pdi  = 100 * sm_plus[i]  / sm_tr[i]
        mdi  = 100 * sm_minus[i] / sm_tr[i]
        dsum = pdi + mdi
        dx_list.append(100 * abs(pdi - mdi) / dsum if dsum > 0 else 0)
    if not dx_list:
        return 20.0
    return sum(dx_list[-period:]) / min(period, len(dx_list))

def detect_regime(closes: list, highs: list, lows: list) -> dict:
    adx = calc_adx(closes, highs, lows)
    if adx >= ADX_TREND:
        regime = "TRENDING"
    elif adx >= ADX_CHOP:
        regime = "RANGING"
    else:
        regime = "CHOPPY"
    return {"regime": regime, "adx": round(adx, 1)}

def detect_structure(closes: list, highs: list, lows: list, lookback: int = 60) -> dict:
    c = closes[-lookback:]
    h = highs[-lookback:]
    l = lows[-lookback:]
    n = len(c)
    last_sh, last_sl = None, None
    for i in range(n-2, 1, -1):
        if h[i] > h[i-1] and h[i] > h[i+1] and last_sh is None:
            last_sh = h[i]
        if l[i] < l[i-1] and l[i] < l[i+1] and last_sl is None:
            last_sl = l[i]
        if last_sh and last_sl:
            break
    return {
        "valid":   last_sh is not None and last_sl is not None,
        "last_sh": last_sh,
        "last_sl": last_sl,
    }

def detect_accumulation(closes: list, highs: list, lows: list,
                         volumes: list, lookback: int = 15) -> dict:
    if len(closes) < lookback + 5:
        return {"accumulating": False, "obv_slope": 0.0, "cmf": 0.0}
    c = closes[-lookback:]
    h = highs[-lookback:]
    l = lows[-lookback:]
    v = volumes[-lookback:]
    avg_price   = sum(c) / len(c)
    price_range = (max(h) - min(l)) / avg_price if avg_price > 0 else 1.0
    obv = [0.0]
    for i in range(1, len(c)):
        if c[i] > c[i-1]:   obv.append(obv[-1] + v[i])
        elif c[i] < c[i-1]: obv.append(obv[-1] - v[i])
        else:                obv.append(obv[-1])
    obv_early = sum(obv[:5]) / 5
    obv_late  = sum(obv[-5:]) / 5
    obv_slope = (obv_late - obv_early) / (abs(obv_early) + 1)
    mf_vol = total_vol = 0.0
    for i in range(-14, 0):
        hi, lo, cl, vl = h[i], l[i], c[i], v[i]
        hl_range = hi - lo
        mf_mult  = ((cl - lo) - (hi - cl)) / hl_range if hl_range > 0 else 0.0
        mf_vol    += mf_mult * vl
        total_vol += vl
    cmf = mf_vol / total_vol if total_vol > 0 else 0.0
    accumulating = price_range < 0.08 and obv_slope > 0.05 and cmf > 0.0
    return {
        "accumulating": accumulating,
        "obv_slope":    round(obv_slope, 3),
        "cmf":          round(cmf, 3),
    }

def is_organic_move(closes: list, volumes: list, lookback: int = 10) -> dict:
    if len(closes) < lookback + 2 or len(volumes) < lookback + 2:
        return {"organic": True, "reason": "data kurang"}
    c = closes[-(lookback+2):]
    v = volumes[-(lookback+2):]
    avg_vol      = sum(v[-lookback-1:-1]) / lookback
    last_vol     = v[-1]
    spike_ratio  = last_vol / (avg_vol + 1)
    velocity     = abs(c[-1] - c[-3]) / c[-3] if c[-3] > 0 else 0.0
    total_vol_5  = sum(v[-5:])
    concentration = last_vol / (total_vol_5 + 1)
    pnd = (c[-2] > c[-3] * 1.05) and (c[-1] < c[-2] * 0.98)
    if spike_ratio > 5.0:
        return {"organic": False, "reason": f"volume spike {spike_ratio:.1f}×"}
    if velocity > 0.10:
        return {"organic": False, "reason": f"velocity {velocity*100:.1f}%"}
    if concentration > 0.65:
        return {"organic": False, "reason": f"volume terkonsentrasi {concentration*100:.0f}%"}
    if pnd:
        return {"organic": False, "reason": "pump & dump pattern"}
    return {"organic": True, "reason": "ok"}

def score_signal(side: str, price: float, closes: list,
                 highs: list, lows: list, volumes: list,
                 structure: dict, rsi: float, macd: float, msig: float,
                 ema20: float, ema50: float, regime: str,
                 btc_4h: float = 0.0, fg: int = 50) -> float:
    score = 0.0
    if side == "BUY":
        if ema20 > ema50 and price > ema20:  score += 1.0
        elif ema20 > ema50:                   score += 0.5
    else:
        if ema20 < ema50 and price < ema20:  score += 1.0
        elif ema20 < ema50:                   score += 0.5
    if side == "BUY":
        if macd > msig and macd > 0:  score += 1.0
        elif macd > msig:              score += 0.5
    else:
        if macd < msig and macd < 0:  score += 1.0
        elif macd < msig:              score += 0.5
    avg_vol = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else 0
    if avg_vol > 0:
        if volumes[-1] > avg_vol * 1.5:   score += 1.0
        elif volumes[-1] > avg_vol * 1.2: score += 0.5
    raw_bonus = 0.0
    if side == "BUY" and 40 <= rsi <= 65:   raw_bonus += 0.3
    elif side == "SELL" and 35 <= rsi <= 60: raw_bonus += 0.3
    if side == "BUY" and btc_4h > 0:    raw_bonus += 0.3
    elif side == "SELL" and btc_4h < 0: raw_bonus += 0.3
    sh = structure.get("last_sh")
    sl_lvl = structure.get("last_sl")
    if sh and sl_lvl and (sh - sl_lvl) / sl_lvl > 0.02:
        raw_bonus += 0.2
    bonus   = min(raw_bonus, 0.5)
    penalty = -0.5 if (fg < 20 or fg > 80) else 0.0
    score  += bonus + penalty
    if regime == "RANGING":
        score *= 0.85
    return round(score, 2)

def calc_sl_tp_scan(entry: float, side: str, atr: float,
                    structure: dict):
    if side == "BUY":
        last_sl = structure.get("last_sl")
        if last_sl and last_sl < entry:
            sl = last_sl - atr * ATR_SL_BUFFER
        else:
            sl = entry - atr * SL_ATR_MULT
        sl = max(sl, entry * (1 - MAX_SL_PCT))
        sl = min(sl, entry * (1 - MIN_SL_PCT))
        sl_dist = entry - sl
        tp1 = entry + sl_dist * TP1_R
        tp2 = entry + sl_dist * TP2_R
    else:
        last_sh = structure.get("last_sh")
        if last_sh and last_sh > entry:
            sl = last_sh + atr * ATR_SL_BUFFER
        else:
            sl = entry + atr * SL_ATR_MULT
        sl = min(sl, entry * (1 + MAX_SL_PCT))
        sl = max(sl, entry * (1 + MIN_SL_PCT))
        sl_dist = sl - entry
        tp1 = entry - sl_dist * TP1_R
        tp2 = entry - sl_dist * TP2_R
    return round(sl, 8), round(tp1, 8), round(tp2, 8)

def get_candles_scan(client, pair: str, interval: str = "1h",
                     limit: int = 150):
    global _CANDLE_FORMAT_LOGGED
    try:
        candles = client.list_candlesticks(pair, interval=interval, limit=limit)
        if not candles or len(candles) < 10:
            _track_api(True)
            return None
        if not _CANDLE_FORMAT_LOGGED:
            log(f"[CANDLE FORMAT] {pair} len={len(candles[0])}")
            _CANDLE_FORMAT_LOGGED = True
        if len(candles[0]) < 6:
            _track_api(False)
            return None
        closes  = [float(c[2]) for c in candles]
        highs   = [float(c[3]) for c in candles]
        lows    = [float(c[4]) for c in candles]
        volumes = [float(c[1]) for c in candles]
        _track_api(True)
        return closes, highs, lows, volumes
    except Exception as e:
        log(f"   Candle error {pair}: {e}", "warn")
        _track_api(False)
        return None

def get_all_pairs_scan(client) -> list:
    try:
        tickers = client.list_tickers()
        EXCLUDED_SUFFIXES = [
            "3L_USDT", "3S_USDT", "5L_USDT", "5S_USDT",
            "2L_USDT", "2S_USDT", "UP_USDT", "DOWN_USDT", "ON_USDT",
        ]
        pairs_vol = []
        for t in tickers:
            try:
                pair = str(t.currency_pair)
                if not pair.endswith("_USDT"):
                    continue
                if any(pair.endswith(suf) for suf in EXCLUDED_SUFFIXES):
                    continue
                vol = float(t.quote_volume or 0)
                if vol >= MIN_VOLUME_USDT:
                    pairs_vol.append((pair, vol))
            except Exception:
                continue
        pairs_vol.sort(key=lambda x: x[1], reverse=True)
        _track_api(True)
        return [p for p, _ in pairs_vol]
    except Exception as e:
        log(f"get_all_pairs error: {e}", "error")
        _track_api(False)
        return []

def get_trending_pairs_scan(gate_pairs: list) -> list:
    try:
        req = urllib.request.Request(
            "https://api.coingecko.com/api/v3/search/trending",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data     = json.loads(r.read())
            coins    = data.get("coins", [])
            gate_set = set(gate_pairs)
            trending = []
            for item in coins:
                symbol = item.get("item", {}).get("symbol", "").upper()
                pair   = f"{symbol}_USDT"
                if pair in gate_set and pair not in trending:
                    trending.append(pair)
            if trending:
                log(f"🔥 Trending: {', '.join(trending)}")
            return trending
    except Exception as e:
        log(f"get_trending_pairs error: {e}", "warn")
        return []

def get_btc_regime_scan(client) -> dict:
    data_1h = get_candles_scan(client, "BTC_USDT", "1h", 10)
    data_4h = get_candles_scan(client, "BTC_USDT", "4h", BTC_TREND_LOOKBACK + 2)
    btc_1h = btc_4h = 0.0
    halt = block_buy = btc_bearish_trend = False
    btc_bearish_cycles = 0
    btc_volatile = False
    btc_1h_range = 0.0
    if data_1h:
        closes = data_1h[0]
        highs  = data_1h[1]
        lows   = data_1h[2]
        if len(closes) >= 2:
            btc_1h = (closes[-1] - closes[-2]) / closes[-2] * 100
        if highs and lows and closes:
            btc_1h_range = (highs[-1] - lows[-1]) / closes[-1] * 100
        if abs(btc_1h) >= BTC_VOLATILE_1H or btc_1h_range >= BTC_RANGE_1H:
            btc_volatile = True
        if btc_1h <= BTC_CRASH_BLOCK:
            halt = True
        elif btc_1h <= BTC_DROP_BLOCK:
            block_buy = True
    if data_4h:
        closes = data_4h[0]
        if len(closes) >= 2:
            btc_4h = (closes[-1] - closes[-2]) / closes[-2] * 100
        recent = closes[-BTC_TREND_LOOKBACK:]
        bearish_count = sum(
            1 for i in range(1, len(recent)) if recent[i] < recent[i-1]
        )
        btc_bearish_cycles = bearish_count
        if bearish_count >= BTC_TREND_MIN_BEARISH:
            btc_bearish_trend = True
    return {
        "btc_1h":             round(btc_1h, 2),
        "btc_4h":             round(btc_4h, 2),
        "btc_volatile":       btc_volatile,
        "halt":               halt,
        "block_buy":          block_buy,
        "btc_bearish_trend":  btc_bearish_trend,
        "btc_bearish_cycles": btc_bearish_cycles,
    }

def get_fear_greed_scan() -> int:
    try:
        req = urllib.request.Request(
            "https://api.alternative.me/fng/?limit=1",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
            return int(data["data"][0]["value"])
    except Exception as e:
        log(f"get_fear_greed error: {e} — fallback 50", "warn")
        return 50

def get_pair_winrate_scan(pair: str) -> dict:
    if supabase_signal is None:
        return {"win_rate": -1, "total": 0}
    try:
        rows = (
            supabase_signal.table("signals_v2")
            .select("result")
            .eq("pair", pair)
            .not_.is_("result", "null")
            .neq("result", "EXPIRED")
            .execute()
            .data
        ) or []
        WIN_RESULTS  = {"TP2", "TP1", "WIN", "PARTIAL_WIN", "SL_AFTER_TP1"}
        wins   = sum(1 for r in rows if r.get("result") in WIN_RESULTS)
        losses = sum(1 for r in rows if r.get("result") in {"SL", "LOSS", "EXPIRED_LOSS"})
        total  = wins + losses
        if total == 0:
            return {"win_rate": -1, "total": 0}
        return {"win_rate": round(wins / total * 100, 1), "total": total}
    except Exception as e:
        log(f"get_pair_winrate error ({pair}): {e}", "warn")
        return {"win_rate": -1, "total": 0}

def check_intraday_scan(client, pair: str, price: float,
                         btc: dict, fg: int = 50) -> dict | None:
    """Scan satu pair — return signal dict atau None."""
    if btc.get("halt"):
        return None
    if btc.get("block_buy"):
        return None
    if btc.get("btc_bearish_trend"):
        return None

    data = get_candles_scan(client, pair, "1h", 150)
    if data is None:
        return None
    closes, highs, lows, volumes = data

    atr     = calc_atr(closes, highs, lows)
    atr_pct = atr / price * 100
    if atr_pct < 0.2 or atr_pct > 8.0:
        return None

    mkt = detect_regime(closes, highs, lows)
    if mkt["regime"] == "CHOPPY":
        return None

    rsi        = calc_rsi(closes)
    macd, msig = calc_macd(closes)
    ema20      = calc_ema(closes, 20)
    ema50      = calc_ema(closes, 50)
    structure  = detect_structure(closes, highs, lows)

    if not structure["valid"]:
        return None
    if rsi > 70:
        return None

    score = score_signal(
        "BUY", price, closes, highs, lows, volumes,
        structure, rsi, macd, msig, ema20, ema50,
        mkt["regime"], btc.get("btc_4h", 0.0), fg
    )
    if score < MIN_SCORE:
        return None

    avg_vol = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else 0
    if avg_vol > 0 and volumes[-1] < avg_vol * 1.2:
        return None

    pump = is_organic_move(closes, volumes)
    if not pump["organic"]:
        log(f"      {pair} — pump filter: {pump['reason']}")
        return None

    accu = detect_accumulation(closes, highs, lows, volumes)
    if accu["accumulating"]:
        score = round(score + 0.3, 2)
        log(f"      {pair} — akumulasi: OBV={accu['obv_slope']:+.2f} → score +0.3")

    # 4h confirmation
    data_4h = get_candles_scan(client, pair, "4h", 60)
    if data_4h:
        c4, h4, l4, _ = data_4h
        ema20_4h = calc_ema(c4, 20)
        ema50_4h = calc_ema(c4, 50)
        macd_4h, msig_4h = calc_macd(c4)
        if not (ema20_4h > ema50_4h and macd_4h > msig_4h):
            return None

    # WR-based threshold
    wr_data = get_pair_winrate_scan(pair)
    wr_pct  = wr_data.get("win_rate", -1)
    wr_n    = wr_data.get("total", 0)
    wr_adj  = 0.0
    if wr_n >= 5:
        if wr_pct <= 30:   wr_adj = +0.3
        elif wr_pct >= 60: wr_adj = -0.2

    bearish_cycles = btc.get("btc_bearish_cycles", 0)
    adaptive_min   = MIN_SCORE + wr_adj + (0.5 if bearish_cycles >= 2 else 0.0)
    if score < adaptive_min:
        return None

    last_sh = structure.get("last_sh")
    entry   = round(last_sh * 1.002, 8) if (last_sh and price > last_sh) else price
    dev     = abs(price - entry) / entry
    if dev > MAX_ENTRY_DEV:
        return None

    sl, tp1, tp2 = calc_sl_tp_scan(entry, "BUY", atr, structure)

    if tp1 <= entry or sl >= entry:
        return None
    sl_dist = entry - sl
    if sl_dist <= 0:
        return None
    rr = abs(tp1 - entry) / sl_dist
    if rr < MIN_RR:
        return None

    tier = "A+" if score >= 3.8 else "A" if score >= 3.5 else "B"

    return {
        "pair":    pair,
        "side":    "BUY",
        "entry":   entry,
        "tp1":     tp1,
        "tp2":     tp2,
        "sl":      sl,
        "score":   score,
        "tier":    tier,
        "rr":      round(rr, 1),
        "rsi":     round(rsi, 1),
        "regime":  mkt["regime"],
        "adx":     mkt["adx"],
    }

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
    Eksekusi satu sinyal dari integrated scanner.
    Return True jika berhasil entry, False jika skip.
    """
    pair      = sig["pair"]
    entry_ref = float(sig["entry"] or 0)
    sl_ref    = float(sig["sl"] or 0)
    tp1_ref   = float(sig["tp1"] or 0)
    tp2_ref   = float(sig["tp2"] or 0)
    score     = float(sig.get("score") or 0)
    tier      = sig.get("tier") or "B"

    # Skip jika pair sudah punya posisi open
    if pair in open_pairs:
        log(f"   ⛔ {pair} — pair sudah ada di open positions")
        return False

    # Validasi data sinyal
    if entry_ref <= 0 or sl_ref <= 0 or tp1_ref <= 0 or tp2_ref <= 0:
        log(f"   ⛔ {pair} — data sinyal tidak lengkap (entry/sl/tp kosong)")
        return False

    # Ambil harga live
    live_price = get_ticker_price(client, pair)
    if live_price <= 0:
        log(f"   ⛔ {pair} — tidak bisa ambil harga live")
        return False

    # Cek harga masih valid (tidak terlalu jauh dari entry)
    price_drift = abs(live_price - entry_ref) / entry_ref
    if live_price > entry_ref * 1.02:
        log(f"   ⛔ {pair} — harga sudah naik {price_drift*100:.1f}% dari entry (skip)")
        return False
    if live_price < sl_ref:
        log(f"   ⛔ {pair} — harga ${live_price:.4f} sudah di bawah SL ${sl_ref:.4f}")
        return False

    # Hitung TP/SL dari harga live
    sl_pct  = abs(entry_ref - sl_ref) / entry_ref
    tp1_pct = abs(tp1_ref - entry_ref) / entry_ref
    tp2_pct = abs(tp2_ref - entry_ref) / entry_ref
    rr      = round(tp2_pct / sl_pct, 2) if sl_pct > 0 else 0.0

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
        "signal_id":  None,
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
        f"Score  : {score} | Regime: {sig.get('regime','?')} | ADX: {sig.get('adx','?')}\n"
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
    growth  = ((balance / INITIAL_EQUITY_USDT) - 1) * 100
    log(f"💰 Balance USDT: ${balance:.2f} | "
        f"Growth: {growth:+.1f}% dari modal awal ${INITIAL_EQUITY_USDT:.2f}")

    now_wib = datetime.now(WIB)

    # ── Heartbeat — jam 08:00 WIB ─────────────────────
    if now_wib.hour == 8 and now_wib.minute < 30:
        send_daily_report(idr_rate)
        daily_pnl_hb = get_daily_pnl()
        open_pos_hb  = load_open_positions()
        growth_icon  = "📈" if growth >= 0 else "📉"
        tg(
            f"💓 <b>Heartbeat — Altcoin Bot v{BOT_VERSION}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕗 {now_wib.strftime('%d %b %Y, %H:%M WIB')}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance  : <b>${balance:.2f}</b> ({idr_fmt(balance, idr_rate)})\n"
            f"{growth_icon} Growth    : <b>{growth:+.1f}%</b> dari modal ${INITIAL_EQUITY_USDT:.2f}\n"
            f"📂 Posisi   : <b>{len(open_pos_hb)}/{MAX_OPEN_POSITIONS}</b> open\n"
            f"📉 PnL hari ini: <b>${daily_pnl_hb:+.4f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>✅ Bot aktif dan berjalan normal.</i>"
        )

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

    # ── Run summary — setiap run ───────────────────────
    daily_pnl   = get_daily_pnl()
    pnl_emoji   = "✅" if daily_pnl >= 0 else "🔴"
    bal_emoji   = "📈" if balance >= INITIAL_EQUITY_USDT else "📉"
    pos_summary = ""
    if open_positions:
        lines = []
        for p in open_positions:
            try:
                price   = get_ticker_price(client, p["pair"])
                pnl_pct = (price / float(p["buy_price"]) - 1) * 100 if price > 0 else 0
                lines.append(
                    f"  • {p['pair']} | "
                    f"Entry:${float(p['buy_price']):.4f} | "
                    f"PnL:{pnl_pct:+.2f}%"
                )
            except Exception:
                lines.append(f"  • {p['pair']}")
        pos_summary = "\n" + "\n".join(lines)
    tg(
        f"📊 <b>Run Summary — Altcoin Bot</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {now_wib.strftime('%d %b %Y, %H:%M WIB')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{bal_emoji} Balance   : <b>${balance:.2f}</b> ({growth:+.1f}%)\n"
        f"{pnl_emoji} PnL hari ini: <b>${daily_pnl:+.4f} USDT</b>\n"
        f"📂 Posisi open: <b>{len(open_positions)}/{MAX_OPEN_POSITIONS}</b>"
        f"{pos_summary}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔄 Ditutup run ini: {closed_count} | Direcovery: {recovered}"
    )

    # ── Step 4: Safety checks sebelum entry ───────────
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        log(f"⛔ Max posisi ({MAX_OPEN_POSITIONS}) tercapai — skip entry")
        return

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

    # ── Step 5: Scan pair baru ────────────────────────────
    log(f"\n── Scan pair baru (integrated scanner) ──")
    now_wib_hour = datetime.now(WIB).hour
    if now_wib_hour in BLOCK_HOURS_WIB:
        log(f"⏸️  Jam {now_wib_hour:02d}:00 WIB masuk BLOCK_HOURS — scan dilewati")
        tg(
            f"⏸️ <b>Scan Dilewati</b>\n"
            f"Jam {now_wib_hour:02d}:00 WIB — low WR hours (23:00–06:00 WIB).\n"
            f"Bot aktif kembali pukul 07:00 WIB."
        )
        return

    # BTC regime check
    log("   Cek BTC regime...")
    btc = get_btc_regime_scan(client)
    log(f"   BTC 1h: {btc['btc_1h']:+.2f}% | 4h: {btc['btc_4h']:+.2f}% | "
        f"Volatile: {btc['btc_volatile']} | Bearish trend: {btc['btc_bearish_trend']}")

    if btc["halt"]:
        log("🛑 BTC crash — scan dibatalkan")
        tg(
            f"🛑 <b>BTC Crash Detected</b>\n"
            f"BTC drop {btc['btc_1h']:+.2f}% dalam 1h.\n"
            f"Scan dibatalkan sampai kondisi stabil."
        )
        return

    if btc["block_buy"]:
        log(f"⚠️ BTC drop {btc['btc_1h']:+.2f}% — BUY diblok")

    fg = get_fear_greed_scan()
    log(f"   Fear & Greed: {fg}")

    # Ambil semua pair
    all_pairs = get_all_pairs_scan(client)
    log(f"   {len(all_pairs)} pair tersedia")

    if not all_pairs:
        log("⚠️ Tidak ada pair — scan dibatalkan", "warn")
        return

    # Prioritaskan trending coins
    trending = get_trending_pairs_scan(all_pairs)
    if trending:
        non_trending = [p for p in all_pairs if p not in trending]
        all_pairs    = trending + non_trending

    if api_is_degraded():
        log("⚠️ API degraded — scan dibatalkan", "warn")
        return

    # Scan pair satu per satu
    scanned      = 0
    entries_done = 0
    max_entries  = MAX_OPEN_POSITIONS - len(open_positions)

    for pair in all_pairs:
        if entries_done >= max_entries:
            break
        if pair in open_pairs:
            continue
        if api_is_degraded():
            log("⚠️ API degraded mid-scan — stop", "warn")
            break

        price = get_ticker_price(client, pair)
        if price is None or price <= 0:
            continue

        scanned += 1

        sig = check_intraday_scan(client, pair, price, btc, fg)
        if sig is None:
            time.sleep(0.3)
            continue

        log(f"   ✅ SIGNAL: {pair} score={sig['score']} tier={sig['tier']} "
            f"rr={sig['rr']} entry=${sig['entry']:.4f}")

        # Refresh balance sebelum entry
        balance = get_usdt_balance(client)
        if balance < MIN_ORDER_USDT:
            log(f"⚠️ Balance ${balance:.2f} tidak cukup — stop")
            tg(
                f"⚠️ <b>Balance Tidak Cukup</b>\n"
                f"Balance: <b>${balance:.2f}</b> | Min order: <b>${MIN_ORDER_USDT:.2f}</b>\n"
                f"Top up diperlukan."
            )
            break

        ok = execute_signal(client, sig, balance, open_pairs, idr_rate)
        if ok:
            entries_done += 1
            open_pairs.add(pair)
            time.sleep(1)

        time.sleep(0.3)

    log(f"\n   Scan selesai — {scanned} pair diperiksa | {entries_done} entry baru")

    if scanned > 0 and entries_done == 0:
        tg(
            f"📭 <b>No Signal</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {now_wib.strftime('%H:%M WIB')}\n"
            f"Scan {scanned} pair — tidak ada yang memenuhi kriteria.\n"
            f"Balance siap: <b>${balance:.2f}</b> | Slot: <b>{max_entries}</b>\n"
            f"F&G: {fg} | BTC 1h: {btc['btc_1h']:+.2f}%"
        )

    log(f"\n{'='*55}")
    log(f"✅ Run selesai — {entries_done} entry baru | "
        f"{len(open_positions)} posisi tetap open")
    log(f"{'='*55}")


if __name__ == "__main__":
    run()
