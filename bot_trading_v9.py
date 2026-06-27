"""
╔══════════════════════════════════════════════════════════════════╗
║     ALTCOIN TRADING BOT v9.0 — Compounding & Quality Edition    ║
║                                                                  ║
║  Scanner : Built-in scoring engine (EMA, MACD, ADX, RSI, ATR)  ║
║            Multi-timeframe 1h + 4h confirmation                 ║
║            Accumulation detection, organic move filter          ║
║            Slippage guard sebelum eksekusi order                ║
║  Entry   : Integrated scanner — zero latency                    ║
║  Pair    : Semua USDT pair di Gate.io (vol > 150K/hari)         ║
║  Order   : Market IOC (BUY only) + min_amount guard             ║
║  Exit    : TP1 partial 50% → TP2 full → SL                     ║
║            SL geser ke breakeven setelah TP1 hit               ║
║            TIME EXIT otomatis setelah MAX_HOLD_HOURS            ║
║            Dust exit: tutup tanpa sell jika nilai < $1          ║
║  Risk    : Dynamic risk % via Equity Curve Control (ECC)        ║
║            Re-entry logic setelah SL bounce                     ║
║  Safety  : Max daily loss, cooldown, BTC crash guard            ║
║            Block hours 23:00–06:00 WIB (low WR)                ║
║            Stale position alert setelah STALE_ALERT_HOURS       ║
║  Recover : Auto-recover orphan position (threshold $5)          ║
║  Control : Telegram commands /pause /resume /close /status      ║
║            INITIAL_EQUITY auto-update dari balance aktual       ║
║                                                                  ║
║  Flow per run (GitHub Actions setiap 10 menit):                 ║
║  1. Cek Telegram commands (pause/resume/close)                  ║
║  2. Load & recover orphan positions                             ║
║  3. Evaluasi posisi: SL / TP1 / TP2 / TIME EXIT / STALE ALERT  ║
║  4. Safety checks (daily loss, cooldown, block hours, BTC)      ║
║  5. Scan semua pair → score → slippage check → eksekusi entry   ║
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


# ════════════════════════════════════════════════════════════════
#  SECTION 1 — ENVIRONMENT & CLIENTS
# ════════════════════════════════════════════════════════════════

API_KEY      = os.environ["GATE_API_KEY"]
SECRET_KEY   = os.environ["GATE_SECRET_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TG_TOKEN     = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID   = os.environ["CHAT_ID"]

# Trading bot Supabase (positions, trade_history, bot_state)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Signal bot Supabase — opsional, untuk winrate lookup per pair
_sig_url = os.environ.get("SIGNAL_SUPABASE_URL", "")
_sig_key = os.environ.get("SIGNAL_SUPABASE_KEY", "")
supabase_signal = create_client(_sig_url, _sig_key) if (_sig_url and _sig_key) else None

BOT_VERSION = "9.0.0"
WIB         = timezone(timedelta(hours=7))


# ════════════════════════════════════════════════════════════════
#  SECTION 2 — CONFIG & CONSTANTS
# ════════════════════════════════════════════════════════════════

# ── Equity & compounding order sizing ───────────────────────────
#    Order size dihitung sebagai % equity → naik otomatis seiring
#    balance tumbuh (compounding). Tidak ada hard cap nominal.
INITIAL_EQUITY_USDT = float(os.environ.get("INITIAL_EQUITY_USDT") or "17")
RISK_PCT_DEFAULT    = 0.05   # risk default 5% equity per trade
RISK_PCT_FLOOR      = 0.02   # minimum risk saat loss streak (proteksi modal)
RISK_PCT_CAP        = 0.08   # maximum risk saat win streak
EQUITY_LOOKBACK     = 5      # jumlah trade terakhir untuk ECC
MIN_ORDER_USDT      = 6.0    # minimum order — disesuaikan untuk modal kecil
MAX_ORDER_PCT       = 0.70   # max 70% balance per trade (buffer 30% untuk fee+SL)
RESERVE_PCT         = 0.30   # selalu sisakan 30% balance sebagai buffer

# ── TP/SL structure ──────────────────────────────────────────────
TP1_SELL_RATIO  = 0.50   # jual 50% saat TP1
TP1_R           = 1.5    # TP1 = SL distance × 1.5
TP2_R           = 2.5    # TP2 = SL distance × 2.5
SL_ATR_MULT     = 2.0    # SL = entry − ATR × 2.0
ATR_SL_BUFFER   = 0.5    # SL = swing low − ATR × 0.5
MAX_SL_PCT      = 0.035  # max SL 3.5% dari entry
MIN_SL_PCT      = 0.005  # min SL 0.5% dari entry

# ── Position time management ─────────────────────────────────────
MAX_HOLD_HOURS    = 72   # force exit jika posisi > 72 jam (3 hari)
STALE_ALERT_HOURS = 24   # kirim alert Telegram jika posisi > 24 jam

# ── Safety guards ────────────────────────────────────────────────
MAX_DAILY_LOSS_PCT   = 0.10  # stop entry jika rugi > 10% equity hari ini (compounding)
MAX_OPEN_POSITIONS   = 1     # maksimal 1 posisi bersamaan
COOLDOWN_SL_CYCLES   = 3     # siklus cooldown setelah SL hit
COOLDOWN_SMART_CYCLES= 2     # siklus cooldown setelah kondisi tertentu
BTC_CRASH_THRESHOLD  = -5.0  # BTC drop > 5% → blok semua entry

# ── Dust & orphan filter ─────────────────────────────────────────
MIN_POSITION_VALUE_USDT = 5.0  # posisi < $5 dianggap dust, skip recover
DELISTED_TOKENS: set = {
    "TEDDY", "FLOKICEO", "URO", "SHIBAI", "REKT", "MONG", "BEL",
}

# ── Scanner thresholds — quality filter ──────────────────────────
MIN_VOLUME_USDT     = 200_000  # volume minimum pair (naik dari 150K → 200K)
MIN_SCORE           = 3.5      # score minimum (naik dari 3.0 → 3.5, lebih selektif)
MIN_RR              = 2.0      # RR minimum (naik dari 1.5 → 2.0)
MIN_WR_PCT          = 40.0     # skip pair dengan WR historis < 40%
MIN_WR_SAMPLE       = 5        # min 5 trade historis sebelum WR dipakai
MAX_ENTRY_DEV       = 0.015    # max deviasi harga dari entry signal (1.5%, turun dari 2%)
ADX_TREND           = 25       # ADX ≥ 25 → trending
ADX_CHOP            = 20       # ADX < 20 → choppy, skip
ADX_PERIOD          = 14

# ── Priority pairs — scan duluan sebelum pair lainnya ────────────
#    Dipilih berdasarkan liquidity tinggi, volatilitas terukur,
#    dan trend follow yang konsisten di pasar crypto.
PRIORITY_PAIRS = [
    "SOL_USDT",  "AVAX_USDT", "LINK_USDT", "DOT_USDT",
    "INJ_USDT",  "SUI_USDT",  "ARB_USDT",  "OP_USDT",
    "TIA_USDT",  "JUP_USDT",  "WIF_USDT",  "MATIC_USDT",
    "NEAR_USDT", "FTM_USDT",  "ATOM_USDT", "APT_USDT",
    "SEI_USDT",  "DYDX_USDT", "PYTH_USDT", "STRK_USDT",
]

# ── BTC regime filter ────────────────────────────────────────────
BTC_DROP_BLOCK        = -3.0   # BTC drop > 3% → blok BUY
BTC_CRASH_BLOCK       = -10.0  # BTC drop > 10% → halt semua
BTC_VOLATILE_1H       = 1.5    # BTC 1h range > 1.5% = volatile
BTC_RANGE_1H          = 2.5
BTC_TREND_LOOKBACK    = 4
BTC_TREND_MIN_BEARISH = 3

# ── Block jam WR rendah (23:00–06:00 WIB) ────────────────────────
_default_block = "23,0,1,2,3,4,5,6"
BLOCK_HOURS_WIB = set(
    int(h.strip())
    for h in os.getenv("BLOCK_HOURS_WIB", _default_block).split(",")
    if h.strip().isdigit()
)

# ── Slippage guard ───────────────────────────────────────────────
MAX_SLIPPAGE_PCT    = 0.015  # tolak entry jika slippage live > 1.5%

# ── Re-entry after SL bounce ─────────────────────────────────────
REENTRY_ENABLED     = True   # aktifkan re-entry setelah SL jika pair bounce
REENTRY_MIN_BOUNCE  = 0.03   # pair harus bounce min 3% dari SL
REENTRY_LOOKBACK_H  = 6      # window re-entry: max 6 jam setelah SL hit

# ── Telegram commands (dibaca dari bot_state) ────────────────────
TG_CMD_KEY     = "tg_command"
TG_CMD_PAUSE   = "pause"
TG_CMD_RESUME  = "resume"
TG_CMD_CLOSE   = "close"
TG_CMD_STATUS  = "status"

# ── API health tracking (internal) ───────────────────────────────
_api_failures         = 0
API_FAILURE_THRESHOLD = 5
_CANDLE_FORMAT_LOGGED = False


# ════════════════════════════════════════════════════════════════
#  SECTION 3 — UTILITIES (log, telegram, http, formatting)
# ════════════════════════════════════════════════════════════════

def log(msg: str, level: str = "info") -> None:
    ts  = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
    tag = {"info": "[INFO]", "warn": "[WARN]", "error": "[ERROR]"}.get(level, "[INFO]")
    print(f"{ts} {tag} {msg}")


def tg(msg: str) -> None:
    """Kirim pesan ke Telegram dengan retry 3x."""
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
                log(f"Telegram gagal setelah 3x: {e}", "warn")


def http_get(url: str, timeout: int = 6):
    """GET request sederhana, return dict atau None jika gagal."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log(f"HTTP GET error {url[:60]}: {e}", "warn")
    return None


def get_usdt_idr_rate() -> float:
    """Ambil kurs USD/IDR dari dua sumber, fallback 16300."""
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
    """Format nilai USDT ke Rupiah yang mudah dibaca."""
    idr = usdt * rate
    if idr >= 1_000_000_000:
        return f"Rp{idr/1_000_000_000:.2f}M"
    if idr >= 1_000_000:
        return f"Rp{idr/1_000_000:.2f}jt"
    if idr >= 1_000:
        return f"Rp{idr:,.0f}"
    return f"Rp{idr:.2f}"


def position_age_hours(pos: dict) -> float:
    """Hitung umur posisi dalam jam dari created_at. Return 0 jika tidak ada."""
    created_at = pos.get("created_at")
    if not created_at:
        return 0.0
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds() / 3600
    except Exception:
        return 0.0


# ════════════════════════════════════════════════════════════════
#  SECTION 4 — TECHNICAL INDICATORS
# ════════════════════════════════════════════════════════════════

def _track_api(success: bool) -> None:
    global _api_failures
    _api_failures = max(0, _api_failures - 1) if success else _api_failures + 1


def api_is_degraded() -> bool:
    return _api_failures >= API_FAILURE_THRESHOLD


def calc_ema(closes: list, period: int) -> float:
    if len(closes) < period:
        return closes[-1]
    k   = 2.0 / (period + 1)
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
    """Return (macd_line, signal_line). Return (0, 0) jika data kurang."""
    if len(closes) < 34:
        return 0.0, 0.0
    k12, k26, k9 = 2.0/13, 2.0/27, 2.0/10
    ema12 = ema26 = closes[0]
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
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        for i in range(1, len(closes))
    ]
    return sum(trs[-period:]) / min(period, len(trs))


def calc_adx(closes: list, highs: list, lows: list, period: int = 14) -> float:
    if len(closes) < period * 2:
        return 20.0
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(closes)):
        h_diff = highs[i] - highs[i-1]
        l_diff = lows[i-1] - lows[i]
        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
        tr_list.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        ))
    def smooth(lst):
        s = sum(lst[:period])
        result = [s]
        for v in lst[period:]:
            s = s - s / period + v
            result.append(s)
        return result
    sm_tr    = smooth(tr_list)
    sm_plus  = smooth(plus_dm)
    sm_minus = smooth(minus_dm)
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
    """Deteksi swing high dan swing low terakhir."""
    c, h, l = closes[-lookback:], highs[-lookback:], lows[-lookback:]
    n = len(c)
    last_sh = last_sl = None
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
    """Deteksi pola akumulasi via OBV slope dan CMF."""
    if len(closes) < lookback + 5:
        return {"accumulating": False, "obv_slope": 0.0, "cmf": 0.0}
    c, h, l, v = closes[-lookback:], highs[-lookback:], lows[-lookback:], volumes[-lookback:]
    avg_price   = sum(c) / len(c)
    price_range = (max(h) - min(l)) / avg_price if avg_price > 0 else 1.0
    obv = [0.0]
    for i in range(1, len(c)):
        if c[i] > c[i-1]:   obv.append(obv[-1] + v[i])
        elif c[i] < c[i-1]: obv.append(obv[-1] - v[i])
        else:                obv.append(obv[-1])
    obv_slope = (sum(obv[-5:]) / 5 - sum(obv[:5]) / 5) / (abs(sum(obv[:5]) / 5) + 1)
    mf_vol = total_vol = 0.0
    for i in range(-14, 0):
        hl_range = h[i] - l[i]
        mf_mult  = ((c[i] - l[i]) - (h[i] - c[i])) / hl_range if hl_range > 0 else 0.0
        mf_vol    += mf_mult * v[i]
        total_vol += v[i]
    cmf = mf_vol / total_vol if total_vol > 0 else 0.0
    return {
        "accumulating": price_range < 0.08 and obv_slope > 0.05 and cmf > 0.0,
        "obv_slope":    round(obv_slope, 3),
        "cmf":          round(cmf, 3),
    }


def is_organic_move(closes: list, volumes: list, lookback: int = 10) -> dict:
    """Filter pump & dump dan volume spike tidak organik."""
    if len(closes) < lookback + 2 or len(volumes) < lookback + 2:
        return {"organic": True, "reason": "data kurang"}
    c, v    = closes[-(lookback+2):], volumes[-(lookback+2):]
    avg_vol = sum(v[-lookback-1:-1]) / lookback
    spike_ratio   = v[-1] / (avg_vol + 1)
    velocity      = abs(c[-1] - c[-3]) / c[-3] if c[-3] > 0 else 0.0
    concentration = v[-1] / (sum(v[-5:]) + 1)
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
    """Hitung score sinyal 0–3.5+. Min score untuk entry: MIN_SCORE."""
    score = 0.0
    # EMA trend alignment
    if side == "BUY":
        score += 1.0 if (ema20 > ema50 and price > ema20) else 0.5 if ema20 > ema50 else 0
    else:
        score += 1.0 if (ema20 < ema50 and price < ema20) else 0.5 if ema20 < ema50 else 0
    # MACD momentum
    if side == "BUY":
        score += 1.0 if (macd > msig and macd > 0) else 0.5 if macd > msig else 0
    else:
        score += 1.0 if (macd < msig and macd < 0) else 0.5 if macd < msig else 0
    # Volume surge
    avg_vol = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else 0
    if avg_vol > 0:
        score += 1.0 if volumes[-1] > avg_vol * 1.5 else 0.5 if volumes[-1] > avg_vol * 1.2 else 0
    # Bonus: RSI, BTC alignment, structure range — capped 0.5
    raw_bonus = 0.0
    if side == "BUY" and 40 <= rsi <= 65:   raw_bonus += 0.3
    elif side == "SELL" and 35 <= rsi <= 60: raw_bonus += 0.3
    if side == "BUY" and btc_4h > 0:        raw_bonus += 0.3
    elif side == "SELL" and btc_4h < 0:     raw_bonus += 0.3
    sh, sl_lvl = structure.get("last_sh"), structure.get("last_sl")
    if sh and sl_lvl and (sh - sl_lvl) / sl_lvl > 0.02:
        raw_bonus += 0.2
    score += min(raw_bonus, 0.5)
    # Penalty extreme fear/greed
    score += -0.5 if (fg < 20 or fg > 80) else 0.0
    # Ranging market → diskon 15%
    if regime == "RANGING":
        score *= 0.85
    return round(score, 2)


def calc_sl_tp(entry: float, side: str, atr: float, structure: dict):
    """
    Hitung SL, TP1, TP2 dari entry price.
    SL berbasis swing structure + ATR buffer, diklem MAX/MIN_SL_PCT.
    """
    if side == "BUY":
        last_sl = structure.get("last_sl")
        sl = (last_sl - atr * ATR_SL_BUFFER) if (last_sl and last_sl < entry) else (entry - atr * SL_ATR_MULT)
        sl = max(sl, entry * (1 - MAX_SL_PCT))
        sl = min(sl, entry * (1 - MIN_SL_PCT))
        sl_dist = entry - sl
        tp1 = entry + sl_dist * TP1_R
        tp2 = entry + sl_dist * TP2_R
    else:
        last_sh = structure.get("last_sh")
        sl = (last_sh + atr * ATR_SL_BUFFER) if (last_sh and last_sh > entry) else (entry + atr * SL_ATR_MULT)
        sl = min(sl, entry * (1 + MAX_SL_PCT))
        sl = max(sl, entry * (1 + MIN_SL_PCT))
        sl_dist = sl - entry
        tp1 = entry - sl_dist * TP1_R
        tp2 = entry - sl_dist * TP2_R
    return round(sl, 8), round(tp1, 8), round(tp2, 8)


# ════════════════════════════════════════════════════════════════
#  SECTION 5 — MARKET DATA (candles, pairs, BTC regime, F&G)
# ════════════════════════════════════════════════════════════════

def get_candles(client, pair: str, interval: str = "1h", limit: int = 150):
    """Ambil OHLCV dari Gate.io. Return (closes, highs, lows, volumes) atau None."""
    global _CANDLE_FORMAT_LOGGED
    try:
        candles = client.list_candlesticks(pair, interval=interval, limit=limit)
        if not candles or len(candles) < 10 or len(candles[0]) < 6:
            _track_api(len(candles or []) >= 10)
            return None
        if not _CANDLE_FORMAT_LOGGED:
            log(f"[CANDLE FORMAT] {pair} cols={len(candles[0])}")
            _CANDLE_FORMAT_LOGGED = True
        _track_api(True)
        return (
            [float(c[2]) for c in candles],  # close
            [float(c[3]) for c in candles],  # high
            [float(c[4]) for c in candles],  # low
            [float(c[1]) for c in candles],  # volume
        )
    except Exception as e:
        log(f"Candle error {pair}: {e}", "warn")
        _track_api(False)
        return None


def get_all_pairs(client) -> list:
    """Ambil semua USDT pair di Gate.io dengan volume >= MIN_VOLUME_USDT."""
    EXCLUDED_SUFFIXES = [
        "3L_USDT", "3S_USDT", "5L_USDT", "5S_USDT",
        "2L_USDT", "2S_USDT", "UP_USDT", "DOWN_USDT", "ON_USDT",
    ]
    try:
        tickers    = client.list_tickers()
        pairs_vol  = []
        for t in tickers:
            try:
                pair = str(t.currency_pair)
                if not pair.endswith("_USDT"):
                    continue
                if any(pair.endswith(s) for s in EXCLUDED_SUFFIXES):
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


def get_trending_pairs(gate_pairs: list) -> list:
    """Ambil trending coins dari CoinGecko yang ada di Gate.io."""
    try:
        req = urllib.request.Request(
            "https://api.coingecko.com/api/v3/search/trending",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data     = json.loads(r.read())
            gate_set = set(gate_pairs)
            trending = []
            for item in data.get("coins", []):
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


def get_btc_regime(client) -> dict:
    """Analisa kondisi BTC 1h dan 4h untuk filter entry."""
    data_1h = get_candles(client, "BTC_USDT", "1h", 10)
    data_4h = get_candles(client, "BTC_USDT", "4h", BTC_TREND_LOOKBACK + 2)
    btc_1h = btc_4h = 0.0
    halt = block_buy = btc_bearish_trend = btc_volatile = False
    btc_bearish_cycles = 0
    if data_1h:
        closes, highs, lows, _ = data_1h
        if len(closes) >= 2:
            btc_1h = (closes[-1] - closes[-2]) / closes[-2] * 100
        btc_1h_range = (highs[-1] - lows[-1]) / closes[-1] * 100 if closes else 0
        btc_volatile = abs(btc_1h) >= BTC_VOLATILE_1H or btc_1h_range >= BTC_RANGE_1H
        halt      = btc_1h <= BTC_CRASH_BLOCK
        block_buy = not halt and btc_1h <= BTC_DROP_BLOCK
    if data_4h:
        closes = data_4h[0]
        if len(closes) >= 2:
            btc_4h = (closes[-1] - closes[-2]) / closes[-2] * 100
        recent            = closes[-BTC_TREND_LOOKBACK:]
        btc_bearish_cycles = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i-1])
        btc_bearish_trend = btc_bearish_cycles >= BTC_TREND_MIN_BEARISH
    return {
        "btc_1h":             round(btc_1h, 2),
        "btc_4h":             round(btc_4h, 2),
        "btc_volatile":       btc_volatile,
        "halt":               halt,
        "block_buy":          block_buy,
        "btc_bearish_trend":  btc_bearish_trend,
        "btc_bearish_cycles": btc_bearish_cycles,
    }


def get_fear_greed() -> int:
    """Ambil Fear & Greed Index. Fallback 50 (neutral)."""
    try:
        req = urllib.request.Request(
            "https://api.alternative.me/fng/?limit=1",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return int(json.loads(r.read())["data"][0]["value"])
    except Exception as e:
        log(f"Fear & Greed error: {e} — fallback 50", "warn")
        return 50


def get_pair_winrate(pair: str) -> dict:
    """Ambil win rate historis pair dari Signal Bot Supabase."""
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
        WIN_RESULTS = {"TP2", "TP1", "WIN", "PARTIAL_WIN", "SL_AFTER_TP1"}
        wins   = sum(1 for r in rows if r.get("result") in WIN_RESULTS)
        losses = sum(1 for r in rows if r.get("result") in {"SL", "LOSS", "EXPIRED_LOSS"})
        total  = wins + losses
        if total == 0:
            return {"win_rate": -1, "total": 0}
        return {"win_rate": round(wins / total * 100, 1), "total": total}
    except Exception as e:
        log(f"get_pair_winrate error ({pair}): {e}", "warn")
        return {"win_rate": -1, "total": 0}


# ════════════════════════════════════════════════════════════════
#  SECTION 6 — SCANNER (scan satu pair, return signal atau None)
# ════════════════════════════════════════════════════════════════

def scan_pair(client, pair: str, price: float, btc: dict, fg: int = 50) -> dict | None:
    """
    Scan satu pair. Return signal dict jika memenuhi semua kriteria,
    atau None jika skip.
    """
    # BTC guard
    if btc.get("halt") or btc.get("block_buy") or btc.get("btc_bearish_trend"):
        return None

    data = get_candles(client, pair, "1h", 150)
    if data is None:
        return None
    closes, highs, lows, volumes = data

    # ATR filter — pair terlalu flat atau terlalu volatile
    atr     = calc_atr(closes, highs, lows)
    atr_pct = atr / price * 100
    if atr_pct < 0.2 or atr_pct > 8.0:
        return None

    # Regime filter
    mkt = detect_regime(closes, highs, lows)
    if mkt["regime"] == "CHOPPY":
        return None

    # Indicators
    rsi        = calc_rsi(closes)
    macd, msig = calc_macd(closes)
    ema20      = calc_ema(closes, 20)
    ema50      = calc_ema(closes, 50)
    structure  = detect_structure(closes, highs, lows)

    if not structure["valid"] or rsi > 70:
        return None

    score = score_signal(
        "BUY", price, closes, highs, lows, volumes,
        structure, rsi, macd, msig, ema20, ema50,
        mkt["regime"], btc.get("btc_4h", 0.0), fg
    )
    if score < MIN_SCORE:
        return None

    # Volume confirmation
    avg_vol = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else 0
    if avg_vol > 0 and volumes[-1] < avg_vol * 1.2:
        return None

    # Organic move filter
    pump = is_organic_move(closes, volumes)
    if not pump["organic"]:
        log(f"      {pair} — pump filter: {pump['reason']}")
        return None

    # Accumulation bonus
    accu = detect_accumulation(closes, highs, lows, volumes)
    if accu["accumulating"]:
        score = round(score + 0.3, 2)
        log(f"      {pair} — akumulasi OBV={accu['obv_slope']:+.2f} → score +0.3")

    # 4h confirmation — EMA dan MACD harus searah
    data_4h = get_candles(client, pair, "4h", 60)
    if data_4h:
        c4, h4, l4, _ = data_4h
        if not (calc_ema(c4, 20) > calc_ema(c4, 50) and calc_macd(c4)[0] > calc_macd(c4)[1]):
            return None

    # Adaptive score threshold berbasis WR historis dan BTC bearish cycles
    # Win rate filter — skip pair dengan WR historis buruk
    wr_data  = get_pair_winrate(pair)
    wr_pct   = wr_data.get("win_rate", -1)
    wr_n     = wr_data.get("total", 0)
    if wr_n >= MIN_WR_SAMPLE and wr_pct < MIN_WR_PCT:
        log(f"      {pair} — WR {wr_pct:.1f}% < {MIN_WR_PCT:.0f}% (n={wr_n}) — skip")
        return None

    wr_adj   = 0.0
    if wr_n >= MIN_WR_SAMPLE:
        wr_adj = +0.3 if wr_pct <= 30 else (-0.2 if wr_pct >= 60 else 0.0)
    bearish_cycles = btc.get("btc_bearish_cycles", 0)
    adaptive_min   = MIN_SCORE + wr_adj + (0.5 if bearish_cycles >= 2 else 0.0)
    if score < adaptive_min:
        return None

    # Entry price — gunakan breakout di atas swing high jika sudah terlewati
    last_sh = structure.get("last_sh")
    entry   = round(last_sh * 1.002, 8) if (last_sh and price > last_sh) else price
    if abs(price - entry) / entry > MAX_ENTRY_DEV:
        return None

    # SL/TP calculation
    sl, tp1, tp2 = calc_sl_tp(entry, "BUY", atr, structure)
    if tp1 <= entry or sl >= entry:
        return None
    sl_dist = entry - sl
    if sl_dist <= 0:
        return None
    rr = abs(tp1 - entry) / sl_dist
    if rr < MIN_RR:
        return None

    tier = "A+" if score >= 3.8 else "A" if score >= 3.5 else "B"

    # Re-entry check — cek apakah pair ini pernah SL dan sekarang bounce
    reentry_bonus = 0.0
    if REENTRY_ENABLED:
        sl_evt = get_sl_event(pair)
        if sl_evt:
            try:
                hit_at    = datetime.fromisoformat(sl_evt["hit_at"].replace("Z", "+00:00"))
                hours_ago = (datetime.now(timezone.utc) - hit_at).total_seconds() / 3600
                sl_p      = float(sl_evt["sl_price"])
                if hours_ago <= REENTRY_LOOKBACK_H and sl_p > 0:
                    bounce_pct = (price - sl_p) / sl_p
                    if bounce_pct >= REENTRY_MIN_BOUNCE:
                        reentry_bonus = 0.4
                        log(f"      {pair} — re-entry bounce {bounce_pct*100:.1f}% dari SL → score +0.4")
                    else:
                        log(f"      {pair} — SL event ada tapi bounce belum cukup ({bounce_pct*100:.1f}%)")
                else:
                    clear_sl_event(pair)  # expired
            except Exception:
                pass

    score = round(score + reentry_bonus, 2)

    return {
        "pair":   pair,
        "side":   "BUY",
        "entry":  entry,
        "tp1":    tp1,
        "tp2":    tp2,
        "sl":     sl,
        "score":  score,
        "tier":   tier,
        "rr":     round(rr, 1),
        "rsi":    round(rsi, 1),
        "regime": mkt["regime"],
        "adx":    mkt["adx"],
    }


# ════════════════════════════════════════════════════════════════
#  SECTION 7 — GATE.IO CLIENT & ORDER EXECUTION
# ════════════════════════════════════════════════════════════════

def setup_client():
    cfg = gate_api.Configuration(
        host="https://api.gateio.ws/api/v4",
        key=API_KEY, secret=SECRET_KEY
    )
    return gate_api.SpotApi(gate_api.ApiClient(cfg))


def gate_retry(fn, *args, retries: int = 3, **kwargs):
    """Panggil Gate.io API dengan retry otomatis + rate limit handling."""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err   = str(e).lower()
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
    """Return (min_amount, amount_precision) untuk pair."""
    try:
        pairs = gate_retry(client.list_currency_pairs)
        if pairs:
            for p in pairs:
                if p.id == pair:
                    return float(p.min_base_amount or 0.001), int(p.amount_precision or 4)
    except Exception as e:
        log(f"Precision {pair} error: {e}", "warn")
    return 0.001, 4


def do_buy(client, pair: str, order_usdt: float) -> tuple:
    """
    Market BUY. Return (buy_price, filled_amount).
    Raise Exception jika gagal.
    """
    price = get_ticker_price(client, pair)
    if price <= 0:
        raise Exception(f"Harga {pair} tidak valid")
    min_amount, precision = get_pair_precision(client, pair)
    amount = round(order_usdt / price, precision)
    if min_amount > 0 and amount < min_amount:
        raise Exception(f"Amount {amount} < min {min_amount} untuk {pair}")
    log(f"MARKET BUY {pair} | {amount} @ ${price:,.4f} | ${order_usdt:.2f} USDT")
    order  = gate_api.Order(
        currency_pair=pair, type="market", side="buy",
        amount=str(amount), time_in_force="ioc"
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
    Market SELL. Return sell_price.
    Raise Exception jika gagal.
    """
    currency            = pair.split("_")[0]
    coin_bal            = get_coin_balance(client, currency)
    _, precision        = get_pair_precision(client, pair)
    if coin_bal <= 0:
        raise Exception(f"Saldo {currency} kosong")
    sell_amount = round(min(amount, coin_bal), precision)
    price       = get_ticker_price(client, pair)
    log(f"{label} {pair} | {sell_amount} {currency} @ ${price:,.4f}")
    order  = gate_api.Order(
        currency_pair=pair, type="market", side="sell",
        amount=str(sell_amount), time_in_force="ioc"
    )
    result = gate_retry(client.create_order, order)
    if result is None:
        raise Exception(f"Sell order {pair} gagal")
    sell_price = float(result.avg_deal_price or price)
    log(f"✅ SELL filled: {sell_amount} {currency} @ ${sell_price:,.4f}")
    return sell_price


# ════════════════════════════════════════════════════════════════
#  SECTION 8 — SUPABASE (positions, trade history, bot state)
# ════════════════════════════════════════════════════════════════

def load_open_positions() -> list:
    try:
        res = supabase.table("positions").select("*").eq("status", "open").execute()
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
               signal_id: str | None = None, notes: str = "") -> float:
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
    """Write-back hasil ke signals_v2 untuk tracking WR Signal Bot."""
    if supabase_signal is None:
        return
    try:
        supabase_signal.table("signals_v2").update({
            "result":    result,
            "pnl_usdt":  round(pnl_usdt, 4),
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", signal_id).execute()
        log(f"📊 signals_v2 updated: {signal_id} → {result} (${pnl_usdt:.4f})")
    except Exception as e:
        log(f"Update signal result error: {e}", "warn")


def get_bot_paused() -> bool:
    """Cek apakah bot dalam kondisi paused via Telegram command."""
    try:
        res = supabase.table("bot_state").select("value") \
            .eq("key", "bot_paused").execute()
        return res.data[0]["value"] == "true" if res.data else False
    except Exception:
        return False


def set_bot_paused(paused: bool):
    try:
        val = "true" if paused else "false"
        res = supabase.table("bot_state").select("key").eq("key", "bot_paused").execute()
        if res.data:
            supabase.table("bot_state").update({"value": val}).eq("key", "bot_paused").execute()
        else:
            supabase.table("bot_state").insert({"key": "bot_paused", "value": val}).execute()
    except Exception as e:
        log(f"set_bot_paused error: {e}", "warn")


def get_pending_command() -> str:
    """Ambil pending command dari bot_state. Return '' jika tidak ada."""
    try:
        res = supabase.table("bot_state").select("value") \
            .eq("key", TG_CMD_KEY).execute()
        return res.data[0]["value"] if res.data else ""
    except Exception:
        return ""


def clear_command():
    """Hapus command setelah dieksekusi."""
    try:
        supabase.table("bot_state").update({"value": ""}) \
            .eq("key", TG_CMD_KEY).execute()
    except Exception as e:
        log(f"clear_command error: {e}", "warn")


def process_telegram_command(client, cmd: str, idr_rate: float) -> bool:
    """
    Proses command dari Telegram. Return True jika run harus berhenti.
    Commands: pause | resume | close:PAIR_USDT | status
    """
    if not cmd:
        return False
    clear_command()
    cmd = cmd.strip().lower()
    log(f"📨 Command diterima: '{cmd}'")

    # /pause — stop semua entry baru
    if cmd == TG_CMD_PAUSE:
        set_bot_paused(True)
        tg(
            f"⏸️ <b>Bot Dijeda</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Semua entry baru dihentikan.\n"
            f"Posisi open tetap dipantau & dievaluasi.\n"
            f"Kirim <code>/resume</code> untuk aktifkan kembali."
        )
        log("⏸️ Bot paused via Telegram")
        return False  # masih evaluasi posisi, hanya skip scan

    # /resume — aktifkan kembali
    elif cmd == TG_CMD_RESUME:
        set_bot_paused(False)
        tg(
            f"▶️ <b>Bot Diaktifkan</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Bot kembali aktif dan akan scan pair baru."
        )
        log("▶️ Bot resumed via Telegram")
        return False

    # /status — kirim kondisi bot sekarang
    elif cmd == TG_CMD_STATUS:
        balance    = get_usdt_balance(client)
        daily_pnl  = get_daily_pnl()
        open_pos   = load_open_positions()
        paused     = get_bot_paused()
        cooldown   = get_cooldown()
        growth     = ((balance / INITIAL_EQUITY_USDT) - 1) * 100
        pos_lines  = []
        for p in open_pos:
            price   = get_ticker_price(client, p["pair"])
            pnl_pct = (price / float(p["buy_price"]) - 1) * 100 if price > 0 else 0
            age_h   = position_age_hours(p)
            pos_lines.append(
                f"  • {p['pair']} | Entry:${float(p['buy_price']):.4f} | "
                f"PnL:{pnl_pct:+.2f}% | {age_h:.1f}h"
            )
        pos_text = "\n".join(pos_lines) if pos_lines else "  (tidak ada)"
        tg(
            f"📋 <b>Status Bot v{BOT_VERSION}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ Status  : <b>{'⏸️ PAUSED' if paused else '▶️ AKTIF'}</b>\n"
            f"⏳ Cooldown: <b>{cooldown} siklus</b>\n"
            f"💰 Balance : <b>${balance:.2f}</b> ({growth:+.1f}%)\n"
            f"📉 PnL hari ini: <b>${daily_pnl:+.4f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📂 Posisi open ({len(open_pos)}/{MAX_OPEN_POSITIONS}):\n{pos_text}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>Commands: /pause /resume /close PAIR /status</i>"
        )
        return False

    # /close PAIR_USDT — force close posisi tertentu
    elif cmd.startswith("close:") or cmd.startswith("close "):
        sep   = ":" if ":" in cmd else " "
        parts = cmd.split(sep, 1)
        if len(parts) < 2:
            tg("❌ Format salah. Gunakan: <code>close:BTC_USDT</code>")
            return False
        target_pair = parts[1].strip().upper()
        open_pos    = load_open_positions()
        matched     = [p for p in open_pos if p["pair"] == target_pair]
        if not matched:
            tg(f"❌ Posisi <b>{target_pair}</b> tidak ditemukan.")
            return False
        pos     = matched[0]
        amount  = float(pos["amount"])
        buy_p   = float(pos["buy_price"])
        try:
            sell_price = do_sell(client, target_pair, amount, "MANUAL CLOSE")
            profit     = round((sell_price - buy_p) * amount, 4)
            pct        = (sell_price / buy_p - 1) * 100
            save_trade(target_pair, buy_p, sell_price, amount, "MANUAL",
                       notes="Closed via Telegram /close command")
            close_position(pos["id"])
            tg(
                f"✅ <b>Manual Close — {target_pair}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Buy  : ${buy_p:,.4f}\n"
                f"Sell : <b>${sell_price:,.4f}</b>\n"
                f"PnL  : <b>{profit:+.4f} USDT ({pct:+.2f}%)</b>\n"
                f"≈ {idr_fmt(abs(profit), idr_rate)}"
            )
            log(f"✅ Manual close {target_pair} via Telegram | PnL: ${profit:.4f}")
        except Exception as e:
            tg(f"❌ Gagal close <b>{target_pair}</b>: {e}")
            log(f"Manual close {target_pair} gagal: {e}", "error")
        return False

    else:
        tg(
            f"❓ Command tidak dikenal: <code>{cmd}</code>\n"
            f"Commands tersedia:\n"
            f"/pause — hentikan entry baru\n"
            f"/resume — aktifkan kembali\n"
            f"/close PAIR_USDT — force close posisi\n"
            f"/status — lihat kondisi bot"
        )
        return False


def save_sl_event(pair: str, sl_price: float, entry_price: float):
    """Simpan event SL hit untuk tracking re-entry."""
    try:
        supabase.table("bot_state").insert({
            "key":   f"sl_event:{pair}",
            "value": json.dumps({
                "sl_price":    sl_price,
                "entry_price": entry_price,
                "hit_at":      datetime.now(timezone.utc).isoformat(),
            })
        }).execute()
    except Exception:
        pass


def get_sl_event(pair: str) -> dict | None:
    """Ambil event SL terakhir untuk pair."""
    try:
        res = supabase.table("bot_state").select("value") \
            .eq("key", f"sl_event:{pair}").execute()
        if res.data:
            return json.loads(res.data[0]["value"])
    except Exception:
        pass
    return None


def clear_sl_event(pair: str):
    try:
        supabase.table("bot_state").delete().eq("key", f"sl_event:{pair}").execute()
    except Exception:
        pass


def update_initial_equity(new_equity: float):
    """Auto-update INITIAL_EQUITY di bot_state saat top up terdeteksi."""
    global INITIAL_EQUITY_USDT
    try:
        res = supabase.table("bot_state").select("key") \
            .eq("key", "initial_equity").execute()
        if res.data:
            supabase.table("bot_state").update({"value": str(round(new_equity, 2))}) \
                .eq("key", "initial_equity").execute()
        else:
            supabase.table("bot_state").insert({
                "key": "initial_equity", "value": str(round(new_equity, 2))
            }).execute()
        INITIAL_EQUITY_USDT = new_equity
        log(f"💰 INITIAL_EQUITY updated → ${new_equity:.2f}")
    except Exception as e:
        log(f"update_initial_equity error: {e}", "warn")


def load_initial_equity() -> float:
    """Load INITIAL_EQUITY dari Supabase (override env var)."""
    try:
        res = supabase.table("bot_state").select("value") \
            .eq("key", "initial_equity").execute()
        if res.data:
            return float(res.data[0]["value"])
    except Exception:
        pass
    return INITIAL_EQUITY_USDT


def get_daily_pnl() -> float:
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        res   = supabase.table("trade_history") \
            .select("profit") \
            .gte("closed_at", f"{today}T00:00:00+00:00").execute()
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
        log(f"Cooldown get error: {e}", "warn")
        return 0


def set_cooldown(cycles: int):
    try:
        res = supabase.table("bot_state").select("key").eq("key", "altcoin_bot").execute()
        if res.data:
            supabase.table("bot_state").update({"cooldown_remaining": cycles}) \
                .eq("key", "altcoin_bot").execute()
        else:
            supabase.table("bot_state").insert({"key": "altcoin_bot", "cooldown_remaining": cycles}).execute()
    except Exception as e:
        log(f"Set cooldown error: {e}", "warn")


def decrement_cooldown():
    current = get_cooldown()
    if current > 0:
        set_cooldown(current - 1)
        log(f"⏳ Cooldown: {current} → {current-1} siklus tersisa")


# ════════════════════════════════════════════════════════════════
#  SECTION 9 — EQUITY CURVE CONTROL (ECC)
# ════════════════════════════════════════════════════════════════

def get_dynamic_risk_pct() -> float:
    """
    Dynamic risk % berdasarkan N trade terakhir.
    - Loss streak ≥ 3 → floor
    - Loss streak 1–2 → reduced
    - Win streak ≥ 3 → boost (capped CAP)
    - Default → RISK_PCT_DEFAULT
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

    losses_streak = next((i for i, p in enumerate(trades) if p >= 0), len(trades))
    wins_streak   = next((i for i, p in enumerate(trades) if p <= 0), len(trades))

    if losses_streak >= 2:
        risk = RISK_PCT_FLOOR
        log(f"⚠️ ECC: {losses_streak} loss streak → floor {risk*100:.1f}%")
    elif losses_streak == 1:
        risk = max(RISK_PCT_DEFAULT * 0.6, RISK_PCT_FLOOR)
        log(f"⚠️ ECC: 1 loss streak → reduced {risk*100:.1f}%")
    elif wins_streak >= 3:
        boost = min(wins_streak - 2, 3) * 0.005
        risk  = min(RISK_PCT_DEFAULT + boost, RISK_PCT_CAP)
        log(f"📈 ECC: {wins_streak} win streak → boost {risk*100:.1f}%")
    else:
        risk = RISK_PCT_DEFAULT

    return risk


def calc_order_size(equity: float, entry: float, sl: float, risk_pct: float) -> float:
    """
    Compounding position sizing — order size naik otomatis seiring balance tumbuh.

    Formula:
      risk_amount = equity × risk_pct
      order_usdt  = risk_amount ÷ sl_pct   (Kelly-inspired sizing)

    Constraints:
      - Floor : MIN_ORDER_USDT ($6) agar valid di Gate.io
      - Cap   : equity × MAX_ORDER_PCT (70%) — sisakan 30% buffer
      - Reserve: equity × RESERVE_PCT selalu tersimpan

    Contoh growth path (win rate 55%, RR 2.5):
      $17  → order ~$8   | $50  → order ~$22
      $100 → order ~$44  | $200 → order ~$88
    """
    sl_pct = abs(entry - sl) / entry
    if sl_pct <= 0:
        return MIN_ORDER_USDT

    # Equity yang bisa dipakai (setelah reserve)
    usable     = equity * (1.0 - RESERVE_PCT)
    risk_amount = usable * risk_pct
    order_usdt  = risk_amount / sl_pct

    # Floor dan cap
    order_usdt = max(order_usdt, MIN_ORDER_USDT)
    order_usdt = min(order_usdt, equity * MAX_ORDER_PCT)
    order_usdt = min(order_usdt, usable)

    return round(order_usdt, 2)


# ════════════════════════════════════════════════════════════════
#  SECTION 10 — AUTO-RECOVER ORPHAN POSITIONS
# ════════════════════════════════════════════════════════════════

def auto_recover_orphan(client, open_positions: list):
    """
    Deteksi coin di wallet tanpa entry di Supabase.
    Buat posisi recover dengan SL/TP generic.
    Skip: USDT, delisted, dust < MIN_POSITION_VALUE_USDT.
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
        if currency in DELISTED_TOKENS:
            log(f"  [RECOVER] {currency} di blacklist delisted — skip")
            continue

        pair = f"{currency}_USDT"
        bal  = float(acc.available or 0)
        if bal <= 0 or pair in open_pairs:
            continue

        log(f"🔍 [RECOVER] {bal} {currency} ditemukan tanpa posisi — cek...")

        # Cari harga beli dari order history
        buy_price = 0.0
        try:
            orders = gate_retry(
                client.list_orders,
                currency_pair=pair, status="finished", side="buy", limit=5
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
                log(f"  [RECOVER] {currency} delisted — blacklist", "warn")
                DELISTED_TOKENS.add(currency)
                continue
            log(f"  Order history {pair} error: {e}", "warn")

        # Fallback ke harga live jika tidak ada order history
        if buy_price <= 0:
            try:
                price = get_ticker_price(client, pair)
                if price > 0:
                    buy_price = price
                    log(f"  [RECOVER] Pakai harga live ${buy_price:.4f}")
            except Exception as e:
                err_str = str(e)
                if "INVALID_CURRENCY" in err_str or "delisted" in err_str.lower():
                    DELISTED_TOKENS.add(currency)
                    continue

        if buy_price <= 0:
            log(f"  [RECOVER] Tidak bisa tentukan harga {pair} — skip", "warn")
            continue

        # Filter dust — skip jika nilai < MIN_POSITION_VALUE_USDT ($5)
        position_value = bal * buy_price
        if position_value < MIN_POSITION_VALUE_USDT:
            log(f"  [RECOVER] {currency} dust (${position_value:.4f} < ${MIN_POSITION_VALUE_USDT}) — skip")
            continue

        # SL dinamis berbasis volatilitas harga
        sl_pct    = 0.03 if buy_price >= 1.0 else (0.04 if buy_price >= 0.01 else 0.05)
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
            f"🔄 <b>Auto-Recover</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Pair   : <b>{pair}</b>\n"
            f"Amount : <b>{bal} {currency}</b> | Nilai: <b>${order_val:.2f}</b>\n"
            f"Buy    : <b>${buy_price:,.4f}</b>\n"
            f"SL     : <b>${sl_price:,.4f}</b> (-{sl_pct*100:.0f}%)\n"
            f"TP1/TP2: <b>${tp1_price:,.4f}</b> / <b>${tp2_price:,.4f}</b>\n"
            f"<i>⚠️ Verifikasi harga beli di Gate.io history.</i>"
        )
        log(f"✅ [RECOVER] {pair}: {bal} @ ${buy_price:.4f} (${order_val:.2f})")


# ════════════════════════════════════════════════════════════════
#  SECTION 11 — POSITION EVALUATION (SL / TP1 / TP2 / TIME EXIT)
# ════════════════════════════════════════════════════════════════

def evaluate_position(client, pos: dict, idr_rate: float) -> str:
    """
    Evaluasi satu posisi open. Urutan pengecekan:
    1. Zombie cleanup (nilai < $1)
    2. TIME EXIT (umur > MAX_HOLD_HOURS)
    3. STALE ALERT (umur > STALE_ALERT_HOURS, kirim notif sekali)
    4. STOP LOSS
    5. TP2 full exit
    6. TP1 partial exit + geser SL ke breakeven
    Return: 'sl' | 'tp1' | 'tp2' | 'time_exit' | 'hold'
    """
    pair      = pos["pair"]
    buy_price = float(pos["buy_price"])
    amount    = float(pos["amount"])
    sl_price  = float(pos["sl_price"])
    tp1_price = float(pos["tp1_price"])
    tp2_price = float(pos["tp2_price"])
    tp1_hit   = bool(pos.get("tp1_hit", False))
    signal_id = pos.get("signal_id")
    pos_id    = pos["id"]
    peak      = float(pos.get("peak_price") or buy_price)
    currency  = pair.split("_")[0]

    price = get_ticker_price(client, pair)
    if price <= 0:
        log(f"  ⚠️ Ticker {pair} gagal — skip evaluasi", "warn")
        return "hold"

    # ── 1. Zombie cleanup ─────────────────────────────────────────
    position_value = amount * price
    if position_value < 1.0:
        log(f"  🧹 {pair} zombie (nilai: ${position_value:.6f}) — tutup tanpa sell")
        close_position(pos_id)
        return "sl"

    # Update peak price
    peak = max(peak, price)
    update_position(pos_id, {"peak_price": peak})

    profit_pct = (price / buy_price - 1) * 100
    age_hours  = position_age_hours(pos)
    log(f"  {pair} | Price:${price:,.4f} | PnL:{profit_pct:+.2f}% | "
        f"Age:{age_hours:.1f}h | SL:${sl_price:.4f} TP1:${tp1_price:.4f} TP2:${tp2_price:.4f}")

    # ── 2. TIME EXIT — force close jika terlalu lama ──────────────
    if age_hours >= MAX_HOLD_HOURS:
        log(f"  ⏰ {pair} umur {age_hours:.1f}h ≥ {MAX_HOLD_HOURS}h — TIME EXIT", "warn")
        # Jika dust, tutup tanpa sell
        if position_value < MIN_POSITION_VALUE_USDT:
            log(f"  🧹 {pair} TIME EXIT dust (${position_value:.4f}) — tutup tanpa sell")
            close_position(pos_id)
            if signal_id:
                update_signal_result(signal_id, "TIME_EXIT_DUST", 0)
            tg(
                f"⏰ <b>TIME EXIT (Dust) — {pair}</b>\n"
                f"Posisi dust ${position_value:.4f} ditutup setelah {age_hours:.1f}h.\n"
                f"Tidak bisa dijual (di bawah minimum order)."
            )
            return "time_exit"
        try:
            sell_price = do_sell(client, pair, amount, "TIME EXIT")
            profit     = round((sell_price - buy_price) * amount, 4)
            pct        = (sell_price / buy_price - 1) * 100
            save_trade(pair, buy_price, sell_price, amount, "TIME_EXIT",
                       signal_id=signal_id,
                       notes=f"Force exit setelah {age_hours:.1f} jam")
            if signal_id:
                update_signal_result(signal_id, "TIME_EXIT", profit)
            close_position(pos_id)
            tg(
                f"⏰ <b>TIME EXIT — {pair}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Posisi ditutup paksa setelah <b>{age_hours:.1f} jam</b>\n"
                f"Buy  : ${buy_price:,.4f}\n"
                f"Sell : <b>${sell_price:,.4f}</b>\n"
                f"PnL  : <b>{profit:+.4f} USDT ({pct:+.2f}%)</b>\n"
                f"≈ {idr_fmt(abs(profit), idr_rate)}"
            )
            log(f"⏰ TIME EXIT {pair} | PnL: ${profit:.4f} setelah {age_hours:.1f}h")
            return "time_exit"
        except Exception as e:
            log(f"  TIME EXIT sell {pair} gagal: {e}", "error")
            return "hold"

    # ── 3. STALE ALERT — notif sekali jika sudah lama tanpa exit ──
    stale_alerted = bool(pos.get("stale_alerted", False))
    if age_hours >= STALE_ALERT_HOURS and not stale_alerted:
        update_position(pos_id, {"stale_alerted": True})
        tg(
            f"⚠️ <b>Stale Position Alert</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Pair  : <b>{pair}</b>\n"
            f"Umur  : <b>{age_hours:.1f} jam</b> (alert threshold: {STALE_ALERT_HOURS}h)\n"
            f"PnL   : <b>{profit_pct:+.2f}%</b> | Price: ${price:,.4f}\n"
            f"SL    : ${sl_price:.4f} | TP2: ${tp2_price:.4f}\n"
            f"<i>Force exit otomatis pada {MAX_HOLD_HOURS}h.</i>"
        )
        log(f"⚠️ STALE ALERT {pair} — {age_hours:.1f}h open")

    # ── 4. STOP LOSS ──────────────────────────────────────────────
    if price <= sl_price:
        try:
            sell_price = do_sell(client, pair, amount, "STOP LOSS")
            profit     = round((sell_price - buy_price) * amount, 4)
            pct        = (sell_price / buy_price - 1) * 100
            save_trade(pair, buy_price, sell_price, amount, "SL", signal_id=signal_id)
            if signal_id:
                update_signal_result(signal_id, "SL", profit)
            close_position(pos_id)
            save_sl_event(pair, sl_price, buy_price)
            set_cooldown(COOLDOWN_SL_CYCLES)
            tg(
                f"🔴 <b>STOP LOSS — {pair}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Buy  : ${buy_price:,.4f}\n"
                f"Sell : <b>${sell_price:,.4f}</b>\n"
                f"PnL  : <b>{profit:+.4f} USDT ({pct:+.2f}%)</b>\n"
                f"≈ {idr_fmt(abs(profit), idr_rate)}"
            )
            log(f"🔴 SL {pair} | PnL: ${profit:.4f}")
            return "sl"
        except Exception as e:
            log(f"  SL sell {pair} gagal: {e}", "error")
            return "hold"

    # ── 5. TP2 — full exit ────────────────────────────────────────
    if price >= tp2_price:
        try:
            sell_price = do_sell(client, pair, amount, "TP2")
            profit     = round((sell_price - buy_price) * amount, 4)
            pct        = (sell_price / buy_price - 1) * 100
            save_trade(pair, buy_price, sell_price, amount, "TP2", signal_id=signal_id)
            if signal_id:
                update_signal_result(signal_id, "TP2", profit)
            close_position(pos_id)
            tg(
                f"✅ <b>TP2 EXIT — {pair}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Buy  : ${buy_price:,.4f}\n"
                f"Sell : <b>${sell_price:,.4f}</b>\n"
                f"PnL  : <b>+{profit:.4f} USDT ({pct:+.2f}%)</b>\n"
                f"≈ {idr_fmt(profit, idr_rate)}"
            )
            log(f"✅ TP2 {pair} | PnL: ${profit:.4f}")
            return "tp2"
        except Exception as e:
            log(f"  TP2 sell {pair} gagal: {e}", "error")
            return "hold"

    # ── 6. TP1 — partial exit 50% + geser SL ke breakeven ─────────
    if price >= tp1_price and not tp1_hit:
        try:
            partial_amount = round(amount * TP1_SELL_RATIO, 8)
            sell_price     = do_sell(client, pair, partial_amount, "TP1 PARTIAL")
            partial_profit = round((sell_price - buy_price) * partial_amount, 4)
            pct            = (sell_price / buy_price - 1) * 100
            remaining      = round(amount - partial_amount, 8)
            new_sl         = round(buy_price * 1.002, 8)  # SL → breakeven +0.2%
            save_trade(pair, buy_price, sell_price, partial_amount,
                       "TP1", partial=True, signal_id=signal_id)
            update_position(pos_id, {
                "tp1_hit":  True,
                "amount":   remaining,
                "sl_price": new_sl,
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


# ════════════════════════════════════════════════════════════════
#  SECTION 12 — ENTRY EXECUTION
# ════════════════════════════════════════════════════════════════

def execute_entry(client, sig: dict, equity: float,
                  open_pairs: set, idr_rate: float) -> bool:
    """
    Eksekusi entry dari hasil scanner.
    Return True jika berhasil, False jika skip.
    """
    pair      = sig["pair"]
    entry_ref = float(sig["entry"] or 0)
    sl_ref    = float(sig["sl"] or 0)
    tp1_ref   = float(sig["tp1"] or 0)
    tp2_ref   = float(sig["tp2"] or 0)
    score     = float(sig.get("score") or 0)
    tier      = sig.get("tier") or "B"

    if pair in open_pairs:
        log(f"   ⛔ {pair} — sudah ada di open positions")
        return False
    if entry_ref <= 0 or sl_ref <= 0 or tp1_ref <= 0 or tp2_ref <= 0:
        log(f"   ⛔ {pair} — data sinyal tidak lengkap")
        return False

    live_price = get_ticker_price(client, pair)
    if live_price <= 0:
        log(f"   ⛔ {pair} — tidak bisa ambil harga live")
        return False
    # Slippage guard — tolak jika harga sudah terlalu jauh dari signal
    slippage = abs(live_price - entry_ref) / entry_ref
    if slippage > MAX_SLIPPAGE_PCT:
        log(f"   ⛔ {pair} — slippage {slippage*100:.2f}% > {MAX_SLIPPAGE_PCT*100:.1f}% — skip")
        return False
    if live_price > entry_ref * 1.02:
        log(f"   ⛔ {pair} — harga sudah naik dari entry (skip)")
        return False
    if live_price < sl_ref:
        log(f"   ⛔ {pair} — harga ${live_price:.4f} sudah di bawah SL ${sl_ref:.4f}")
        return False

    # Kalkulasi SL/TP dari harga live
    sl_pct  = abs(entry_ref - sl_ref)  / entry_ref
    tp1_pct = abs(tp1_ref  - entry_ref) / entry_ref
    tp2_pct = abs(tp2_ref  - entry_ref) / entry_ref
    rr      = round(tp2_pct / sl_pct, 2) if sl_pct > 0 else 0.0
    sl_live  = round(live_price * (1 - sl_pct), 8)
    tp1_live = round(live_price * (1 + tp1_pct), 8)
    tp2_live = round(live_price * (1 + tp2_pct), 8)

    risk_pct   = get_dynamic_risk_pct()
    order_usdt = calc_order_size(equity, live_price, sl_live, risk_pct)
    log(f"   ✅ {pair} | Score:{score} RR:{rr} Tier:{tier} | "
        f"Entry:${live_price:.4f} SL:${sl_live:.4f} "
        f"TP1:${tp1_live:.4f} TP2:${tp2_live:.4f} | ${order_usdt:.2f}")

    try:
        buy_price, filled = do_buy(client, pair, order_usdt)
    except Exception as e:
        log(f"   ❌ Buy {pair} gagal: {e}", "error")
        tg(f"❌ <b>Buy Gagal — {pair}</b>\n{e}")
        return False

    if buy_price <= 0 or filled <= 0:
        log(f"   ❌ {pair} — filled tidak valid", "error")
        return False

    # Recalc dari fill aktual
    sl_final  = round(buy_price * (1 - sl_pct), 8)
    tp1_final = round(buy_price * (1 + tp1_pct), 8)
    tp2_final = round(buy_price * (1 + tp2_pct), 8)
    order_val = round(filled * buy_price, 2)
    currency  = pair.split("_")[0]

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
        f"<i>⚠️ Bot jual otomatis di TP/SL. Time exit: {MAX_HOLD_HOURS}h.</i>"
    )
    log(f"🟢 BUY {pair} @ ${buy_price:.4f} | {filled} {currency} | "
        f"SL:${sl_final:.4f} TP1:${tp1_final:.4f} TP2:${tp2_final:.4f}")
    return True


# ════════════════════════════════════════════════════════════════
#  SECTION 13 — DAILY REPORT
# ════════════════════════════════════════════════════════════════

def send_daily_report(idr_rate: float):
    try:
        today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        res    = supabase.table("trade_history") \
            .select("profit, result, partial") \
            .gte("closed_at", f"{today}T00:00:00+00:00").execute()
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


# ════════════════════════════════════════════════════════════════
#  SECTION 14 — MAIN RUN
# ════════════════════════════════════════════════════════════════

def run():
    log("=" * 60)
    log(f"🚀 ALTCOIN BOT v{BOT_VERSION} — "
        f"{datetime.now(WIB).strftime('%Y-%m-%d %H:%M WIB')}")
    log("=" * 60)

    client   = setup_client()
    idr_rate = get_usdt_idr_rate()
    log(f"💱 Kurs USD/IDR: Rp{idr_rate:,.0f}")

    # ── Load INITIAL_EQUITY dari Supabase (override env) ─────────
    global INITIAL_EQUITY_USDT
    INITIAL_EQUITY_USDT = load_initial_equity()

    balance = get_usdt_balance(client)
    growth  = ((balance / INITIAL_EQUITY_USDT) - 1) * 100

    # Auto-detect top up: jika balance > initial + 20%, update baseline
    if balance > INITIAL_EQUITY_USDT * 1.20:
        open_pos_val = sum(
            float(p["amount"]) * get_ticker_price(client, p["pair"])
            for p in load_open_positions()
        )
        total_equity = balance + open_pos_val
        if total_equity > INITIAL_EQUITY_USDT * 1.20:
            log(f"💰 Top up terdeteksi: ${total_equity:.2f} > ${INITIAL_EQUITY_USDT:.2f} × 1.2")
            update_initial_equity(round(total_equity, 2))
            tg(
                f"💰 <b>Top Up Terdeteksi</b>\n"
                f"Modal awal diperbarui → <b>${total_equity:.2f}</b>"
            )
            growth = 0.0

    log(f"💰 Balance: ${balance:.2f} | Growth: {growth:+.1f}% dari modal ${INITIAL_EQUITY_USDT:.2f}")

    now_wib = datetime.now(WIB)

    # ── Heartbeat harian jam 08:00 WIB ────────────────────────────
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

    # ── Step 0: Proses Telegram command ──────────────────────────
    cmd = get_pending_command()
    if cmd:
        stop = process_telegram_command(client, cmd, idr_rate)
        if stop:
            return

    # ── Step 1: Load open positions ───────────────────────────────
    open_positions = load_open_positions()
    log(f"📂 Open positions: {len(open_positions)}/{MAX_OPEN_POSITIONS}")

    # ── Step 2: Auto-recover orphan positions ─────────────────────
    log("\n── Auto-recover orphan positions ──")
    log(f"   Blacklist: {len(DELISTED_TOKENS)} token ({', '.join(sorted(DELISTED_TOKENS))})")
    pre_recover_count = len(open_positions)
    auto_recover_orphan(client, open_positions)
    open_positions = load_open_positions()
    recovered      = len(open_positions) - pre_recover_count
    log(f"   Recover: +{recovered} posisi baru | Total: {len(open_positions)}")

    # ── Step 3: Evaluasi semua posisi open ───────────────────────
    log(f"\n── Evaluasi {len(open_positions)} posisi open ──")
    closed_count = 0
    for pos in open_positions:
        result = evaluate_position(client, pos, idr_rate)
        if result in ("sl", "tp2", "time_exit"):
            closed_count += 1

    open_positions = load_open_positions()
    open_pairs     = {p["pair"] for p in open_positions}
    log(f"   Selesai: {closed_count} ditutup | {len(open_positions)} masih open")

    # ── Run Summary ───────────────────────────────────────────────
    daily_pnl = get_daily_pnl()
    pnl_emoji = "✅" if daily_pnl >= 0 else "🔴"
    bal_emoji = "📈" if balance >= INITIAL_EQUITY_USDT else "📉"

    # Compounding progress
    compound_x  = balance / INITIAL_EQUITY_USDT
    next_target = INITIAL_EQUITY_USDT * (round(compound_x) + 1)
    progress_to_next = (balance - INITIAL_EQUITY_USDT * round(compound_x)) / \
                       (INITIAL_EQUITY_USDT) * 100 if compound_x >= 1 else growth

    pos_lines = []
    for p in open_positions:
        try:
            price   = get_ticker_price(client, p["pair"])
            pnl_pct = (price / float(p["buy_price"]) - 1) * 100 if price > 0 else 0
            age_h   = position_age_hours(p)
            pos_lines.append(
                f"  • {p['pair']} | Entry:${float(p['buy_price']):.4f} | "
                f"PnL:{pnl_pct:+.2f}% | {age_h:.1f}h"
            )
        except Exception:
            pos_lines.append(f"  • {p['pair']}")
    pos_summary = ("\n" + "\n".join(pos_lines)) if pos_lines else ""
    tg(
        f"📊 <b>Run Summary — Altcoin Bot v{BOT_VERSION}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {now_wib.strftime('%d %b %Y, %H:%M WIB')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{bal_emoji} Balance   : <b>${balance:.2f}</b> ({growth:+.1f}%)\n"
        f"{pnl_emoji} PnL hari ini: <b>${daily_pnl:+.4f} USDT</b>\n"
        f"📈 Compound : <b>{compound_x:.2f}×</b> dari modal awal ${INITIAL_EQUITY_USDT:.2f}\n"
        f"📂 Posisi open: <b>{len(open_positions)}/{MAX_OPEN_POSITIONS}</b>"
        f"{pos_summary}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔄 Ditutup: {closed_count} | Recovery: {recovered}"
    )

    # ── Step 4: Safety checks sebelum scan ───────────────────────
    if get_bot_paused():
        log("⏸️ Bot paused via Telegram — skip scan")
        return

    if len(open_positions) >= MAX_OPEN_POSITIONS:
        log(f"⛔ Max posisi ({MAX_OPEN_POSITIONS}) tercapai — skip scan")
        return

    if daily_pnl <= -(INITIAL_EQUITY_USDT * MAX_DAILY_LOSS_PCT):
        max_loss_usdt = INITIAL_EQUITY_USDT * MAX_DAILY_LOSS_PCT
        log(f"⛔ Max daily loss ${daily_pnl:.2f} (limit: -${max_loss_usdt:.2f}) — stop hari ini")
        tg(
            f"⛔ <b>Max Daily Loss</b>\n"
            f"Loss hari ini: <b>${abs(daily_pnl):.2f}</b> "
            f"(limit {MAX_DAILY_LOSS_PCT*100:.0f}% equity = ${max_loss_usdt:.2f})\n"
            f"Bot berhenti entry sampai besok."
        )
        return

    cooldown = get_cooldown()
    if cooldown > 0:
        decrement_cooldown()
        log(f"⏳ Cooldown aktif ({cooldown} siklus) — skip scan")
        return

    # ── Step 5: Block hours check ─────────────────────────────────
    log("\n── Scan pair baru ──")
    if now_wib.hour in BLOCK_HOURS_WIB:
        log(f"⏸️  Jam {now_wib.hour:02d}:00 WIB — BLOCK_HOURS, scan dilewati")
        tg(
            f"⏸️ <b>Scan Dilewati</b>\n"
            f"Jam {now_wib.hour:02d}:00 WIB — low WR hours (23:00–06:00).\n"
            f"Bot aktif kembali pukul 07:00 WIB."
        )
        return

    # ── Step 6: BTC regime check ──────────────────────────────────
    log("   Cek BTC regime...")
    btc = get_btc_regime(client)
    log(f"   BTC 1h: {btc['btc_1h']:+.2f}% | 4h: {btc['btc_4h']:+.2f}% | "
        f"Volatile: {btc['btc_volatile']} | Bearish: {btc['btc_bearish_trend']}")

    if btc["halt"]:
        log("🛑 BTC crash — scan dibatalkan")
        tg(
            f"🛑 <b>BTC Crash Detected</b>\n"
            f"BTC drop {btc['btc_1h']:+.2f}% dalam 1h.\n"
            f"Scan dibatalkan."
        )
        return

    fg = get_fear_greed()
    log(f"   Fear & Greed: {fg}")

    # ── Step 7: Ambil pair & prioritaskan trending + priority list ──
    all_pairs = get_all_pairs(client)
    log(f"   {len(all_pairs)} pair tersedia")
    if not all_pairs:
        log("⚠️ Tidak ada pair — scan dibatalkan", "warn")
        return

    # Susun urutan: priority → trending → sisanya
    gate_set      = set(all_pairs)
    priority_valid = [p for p in PRIORITY_PAIRS if p in gate_set]
    trending       = get_trending_pairs(all_pairs)
    trending_new   = [p for p in trending if p not in priority_valid]
    rest           = [p for p in all_pairs if p not in priority_valid and p not in trending_new]
    all_pairs      = priority_valid + trending_new + rest
    log(f"   Urutan scan: {len(priority_valid)} priority | "
        f"{len(trending_new)} trending | {len(rest)} lainnya")

    if api_is_degraded():
        log("⚠️ API degraded — scan dibatalkan", "warn")
        return

    # ── Step 8: Scan pair satu per satu ──────────────────────────
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
        if not price or price <= 0:
            continue

        scanned += 1
        sig = scan_pair(client, pair, price, btc, fg)
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
                f"Balance: <b>${balance:.2f}</b> | Min: <b>${MIN_ORDER_USDT:.2f}</b>\n"
                f"Top up diperlukan."
            )
            break

        ok = execute_entry(client, sig, balance, open_pairs, idr_rate)
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
            f"Balance: <b>${balance:.2f}</b> | Slot: <b>{max_entries}</b>\n"
            f"F&G: {fg} | BTC 1h: {btc['btc_1h']:+.2f}%"
        )

    log("=" * 60)
    log(f"✅ Run selesai — {entries_done} entry | {len(open_positions)} open")
    log("=" * 60)


if __name__ == "__main__":
    run()
