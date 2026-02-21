#!/usr/bin/env python3
"""
Opportunity Scanner v5 — 4-stage funnel, 4-pillar scoring + BTC macro filter + hourly trend gate.
v4 adds: parallel candle fetches, BTC macro filter, cross-scan tracking,
volatility-adjusted leverage, nearest S/R, per-TF error recovery.
All new features are configurable via scanner-config.json.
Token cost: ~0 (all computation in Python).
"""

import json
import sys
import subprocess
import time
import math
import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── Load config (all optional with defaults) ───
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(SCRIPT_DIR)
CONFIG_FILE = os.path.join(WORKSPACE, "scanner-config.json")

config = {}
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE) as f:
        config = json.load(f)

TOP_N_DEEP = config.get("topNDeep", 15)
MIN_VOLUME_24H = config.get("minVolume24h", 500_000)
MAX_WORKERS = config.get("maxWorkers", 8)
DISABLE_MACRO = config.get("disableMacroFilter", False)
SCAN_HISTORY_SIZE = config.get("scanHistorySize", 12)
HOURLY_GATE = config.get("hourlyTrendGate", True)
COUNTER_TREND_HOURLY_PENALTY = config.get("counterTrendHourlyPenalty", -30)

MACRO_MODS = config.get("macroModifiers", {
    "strongDownLong": -40, "strongDownShort": 15,
    "downLong": -20, "downShort": 10,
    "upLong": 10, "upShort": -20,
    "strongUpLong": 15, "strongUpShort": -40
})

VOL_LEV = config.get("volatilityLeverage", {
    "highVolThreshold": 3.0, "highVolPenalty": 3,
    "medVolThreshold": 1.5, "medVolPenalty": 1
})

W_SMART_MONEY = 0.25
W_MARKET_STRUCTURE = 0.25
W_TECHNICALS = 0.25
W_FUNDING = 0.25

# ─── Helpers ───

def fetch_json(payload):
    r = subprocess.run(
        ["curl", "-s", "https://api.hyperliquid.xyz/info",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=30
    )
    return json.loads(r.stdout)

def fetch_mcporter(tool, args=""):
    import tempfile
    tmp = tempfile.mktemp(suffix=".json")
    cmd = f"mcporter call senpi.{tool} --output json {args} > {tmp} 2>/dev/null"
    subprocess.run(cmd, shell=True, timeout=60)
    with open(tmp) as f:
        data = json.load(f)
    os.unlink(tmp)
    return data

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i-1]
        gains.append(max(0, delta))
        losses.append(max(0, -delta))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def calc_ema(values, period):
    if not values:
        return []
    ema = [values[0]]
    k = 2 / (period + 1)
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema

def calc_atr(candles, period=14):
    """Average True Range as % of current price."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]["h"])
        l = float(candles[i]["l"])
        pc = float(candles[i-1]["c"])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if not trs:
        return 0.0
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    current_price = float(candles[-1]["c"])
    if current_price == 0:
        return 0.0
    return round(atr / current_price * 100, 3)

def volume_ratio(candles, recent_n=4):
    if len(candles) < 6:
        return 1.0
    recent = candles[-recent_n:]
    prior = candles[:-recent_n]
    if not prior:
        return 1.0
    recent_avg = sum(float(c["v"]) for c in recent) / len(recent)
    prior_avg = sum(float(c["v"]) for c in prior) / len(prior)
    if prior_avg == 0:
        return 1.0
    return round(recent_avg / prior_avg, 2)

def price_changes(candles):
    if not candles:
        return {"chg1h": 0, "chg4h": 0, "chg24h": 0}
    current = float(candles[-1]["c"])
    def pct(idx):
        if abs(idx) > len(candles):
            idx = -len(candles)
        ref = float(candles[idx]["o"])
        return round((current - ref) / ref * 100, 2) if ref else 0
    return {
        "chg1h": pct(-1),
        "chg4h": pct(-4) if len(candles) >= 4 else pct(-len(candles)),
        "chg24h": pct(0)
    }

def find_swing_levels(candles, lookback=5, current_price=None):
    """v4: returns nearest S/R to current price when current_price is provided."""
    highs = [float(c["h"]) for c in candles]
    lows = [float(c["l"]) for c in candles]
    swing_highs = []
    swing_lows = []
    for i in range(lookback, len(candles) - lookback):
        if highs[i] == max(highs[i-lookback:i+lookback+1]):
            swing_highs.append(highs[i])
        if lows[i] == min(lows[i-lookback:i+lookback+1]):
            swing_lows.append(lows[i])

    if current_price and (swing_highs or swing_lows):
        resistance_levels = [h for h in swing_highs if h > current_price]
        support_levels = [l for l in swing_lows if l < current_price]
        nearest_r = min(resistance_levels) if resistance_levels else (max(swing_highs) if swing_highs else None)
        nearest_s = max(support_levels) if support_levels else (min(swing_lows) if swing_lows else None)
        return [nearest_r] if nearest_r else [], [nearest_s] if nearest_s else []

    return swing_highs[-3:] if swing_highs else [], swing_lows[-3:] if swing_lows else []

def detect_patterns(candles):
    if len(candles) < 3:
        return []
    patterns = []
    c1 = candles[-3]
    c2 = candles[-2]
    c3 = candles[-1]

    def body(c):
        return abs(float(c["c"]) - float(c["o"]))
    def full_range(c):
        return float(c["h"]) - float(c["l"])
    def is_bullish(c):
        return float(c["c"]) > float(c["o"])
    def upper_wick(c):
        return float(c["h"]) - max(float(c["c"]), float(c["o"]))
    def lower_wick(c):
        return min(float(c["c"]), float(c["o"])) - float(c["l"])

    fr3 = full_range(c3)
    b3 = body(c3)

    if fr3 > 0:
        if lower_wick(c3) > b3 * 2 and upper_wick(c3) < b3 * 0.5:
            patterns.append("hammer" if is_bullish(c3) else "inverted_hammer")
        if upper_wick(c3) > b3 * 2 and lower_wick(c3) < b3 * 0.5:
            patterns.append("shooting_star")
        if b3 < fr3 * 0.1:
            patterns.append("doji")

    if not is_bullish(c2) and is_bullish(c3):
        if float(c3["c"]) > float(c2["o"]) and float(c3["o"]) < float(c2["c"]):
            patterns.append("bullish_engulfing")
    if is_bullish(c2) and not is_bullish(c3):
        if float(c3["c"]) < float(c2["o"]) and float(c3["o"]) > float(c2["c"]):
            patterns.append("bearish_engulfing")

    if is_bullish(c1) and is_bullish(c2) and is_bullish(c3):
        if float(c3["c"]) > float(c2["c"]) > float(c1["c"]):
            patterns.append("three_soldiers")
    if not is_bullish(c1) and not is_bullish(c2) and not is_bullish(c3):
        if float(c3["c"]) < float(c2["c"]) < float(c1["c"]):
            patterns.append("three_crows")

    return patterns

def analyze_trend(candles_4h):
    if len(candles_4h) < 5:
        return "neutral", 0
    closes = [float(c["c"]) for c in candles_4h]
    ema_fast = calc_ema(closes, 5)
    ema_slow = calc_ema(closes, 13)
    if not ema_fast or not ema_slow:
        return "neutral", 0

    fast_now = ema_fast[-1]
    slow_now = ema_slow[-1]
    price_now = closes[-1]

    if fast_now > slow_now and price_now > fast_now:
        trend = "strong_up"
        strength = min(100, int((fast_now - slow_now) / slow_now * 1000))
    elif fast_now > slow_now:
        trend = "up"
        strength = min(70, int((fast_now - slow_now) / slow_now * 500))
    elif fast_now < slow_now and price_now < fast_now:
        trend = "strong_down"
        strength = min(100, int((slow_now - fast_now) / slow_now * 1000))
    elif fast_now < slow_now:
        trend = "down"
        strength = min(70, int((slow_now - fast_now) / slow_now * 500))
    else:
        trend = "neutral"
        strength = 0

    if len(ema_fast) >= 3 and len(ema_slow) >= 3:
        gap_now = abs(ema_fast[-1] - ema_slow[-1])
        gap_prev = abs(ema_fast[-3] - ema_slow[-3])
        if gap_now > gap_prev:
            strength = min(100, strength + 15)

    return trend, strength

def multi_tf_analysis(candles_1h, candles_15m, candles_4h, current_price=None):
    result = {}
    trend_4h, trend_strength = analyze_trend(candles_4h)
    result["trend4h"] = trend_4h
    result["trendStrength"] = trend_strength

    closes_1h = [float(c["c"]) for c in candles_1h]
    result["rsi1h"] = calc_rsi(closes_1h)
    result["volRatio1h"] = volume_ratio(candles_1h, 4)
    result.update(price_changes(candles_1h))

    # v4: ATR as % of price for volatility-adjusted leverage
    result["atrPct"] = calc_atr(candles_1h)

    swing_highs, swing_lows = find_swing_levels(candles_1h, 3, current_price)
    result["resistance"] = round(swing_highs[0], 4) if swing_highs else None
    result["support"] = round(swing_lows[0], 4) if swing_lows else None

    if candles_15m:
        closes_15m = [float(c["c"]) for c in candles_15m]
        result["rsi15m"] = calc_rsi(closes_15m)
        result["volRatio15m"] = volume_ratio(candles_15m, 4)
        result["patterns15m"] = detect_patterns(candles_15m)

        if len(candles_15m) >= 4:
            recent_close = float(candles_15m[-1]["c"])
            hour_ago_open = float(candles_15m[-4]["o"])
            result["momentum15m"] = round((recent_close - hour_ago_open) / hour_ago_open * 100, 3)
        else:
            result["momentum15m"] = 0

        if len(candles_15m) >= 8:
            recent_vol = sum(float(c["v"]) for c in candles_15m[-4:])
            prior_vol = sum(float(c["v"]) for c in candles_15m[-8:-4])
            recent_chg = float(candles_15m[-1]["c"]) - float(candles_15m[-4]["c"])
            if prior_vol > 0:
                vol_surge = recent_vol / prior_vol
                if vol_surge > 1.5 and recent_chg < 0:
                    result["divergence"] = "bullish"
                elif vol_surge > 1.5 and recent_chg > 0:
                    result["divergence"] = "bearish"
                else:
                    result["divergence"] = None
            else:
                result["divergence"] = None
        else:
            result["divergence"] = None
    else:
        result["rsi15m"] = 50
        result["volRatio15m"] = 1.0
        result["patterns15m"] = []
        result["momentum15m"] = 0
        result["divergence"] = None

    result["patterns1h"] = detect_patterns(candles_1h)
    return result

# ─── Scoring functions (each returns 0-100) ───

def score_smart_money(asset_data):
    if not asset_data:
        return 0, "LONG", {}
    pnl_pct = abs(asset_data.get("pnlContributionPct", 0))
    traders = asset_data.get("traderCount", 0)
    accel = asset_data.get("contributionChange4h", 0)
    direction = asset_data.get("dominantDirection", "LONG")

    score = 0
    if pnl_pct > 15: score += 50
    elif pnl_pct > 5: score += 35
    elif pnl_pct > 1: score += 20
    elif pnl_pct > 0.3: score += 10

    if traders > 400: score += 30   # v5: higher weight for 400+
    elif traders > 300: score += 25
    elif traders > 100: score += 18
    elif traders > 30: score += 10
    elif traders > 10: score += 5

    if abs(accel) > 10: score += 20
    elif abs(accel) > 3: score += 12
    elif abs(accel) > 1: score += 6

    avg_at_peak = asset_data.get("avgAtPeak", 50)
    near_peak_pct = asset_data.get("nearPeakPct", 0)

    if avg_at_peak > 85: score += 15
    elif avg_at_peak > 70: score += 8
    elif avg_at_peak < 50: score -= 10

    if near_peak_pct > 50: score += 10

    details = {
        "pnlPct": round(pnl_pct, 1), "traders": traders,
        "accel": round(accel, 1), "direction": direction,
        "avgAtPeak": avg_at_peak, "nearPeakPct": near_peak_pct
    }
    return min(100, round(score)), direction, details

def score_market_structure(meta):
    vol24h = meta.get("volume24h", 0)
    oi = meta.get("openInterest", 0)
    prev_day_vol = meta.get("prevDayVolume", vol24h)

    score = 0
    if vol24h > 50_000_000: score += 30
    elif vol24h > 10_000_000: score += 20
    elif vol24h > 1_000_000: score += 10

    if prev_day_vol > 0:
        vol_change = vol24h / prev_day_vol
        if vol_change > 2.0: score += 30
        elif vol_change > 1.3: score += 20
        elif vol_change > 1.0: score += 10

    if oi > 10_000_000: score += 20
    elif oi > 1_000_000: score += 10

    if vol24h > 0:
        oi_vol = oi / vol24h
        if 0.3 < oi_vol < 3.0: score += 20

    details = {
        "vol24h": round(vol24h), "oi": round(oi),
        "volTrend": round(vol24h / prev_day_vol, 2) if prev_day_vol > 0 else 1.0
    }
    return min(100, round(score)), details

def score_technicals(tf_data, direction, hourly_trend="NEUTRAL"):
    score = 0
    rsi1h = tf_data.get("rsi1h", 50)
    rsi15m = tf_data.get("rsi15m", 50)
    vol1h = tf_data.get("volRatio1h", 1.0)
    vol15m = tf_data.get("volRatio15m", 1.0)
    trend = tf_data.get("trend4h", "neutral")
    patterns15m = tf_data.get("patterns15m", [])
    patterns1h = tf_data.get("patterns1h", [])
    momentum15m = tf_data.get("momentum15m", 0)
    divergence = tf_data.get("divergence")
    chg4h = tf_data.get("chg4h", 0)

    if direction == "LONG":
        if trend in ("strong_up", "up"): score += 20
        elif trend == "neutral": score += 5
        elif trend in ("strong_down", "down"): score -= 5
    else:
        if trend in ("strong_down", "down"): score += 20
        elif trend == "neutral": score += 5
        elif trend in ("strong_up", "up"): score -= 5

    if direction == "LONG":
        if rsi1h < 30: score += 20
        elif rsi1h < 40: score += 15
        elif rsi1h < 55: score += 8
        elif rsi1h > 70: score -= 10
    else:
        if rsi1h > 70: score += 20
        elif rsi1h > 60: score += 15
        elif rsi1h > 45: score += 8
        elif rsi1h < 30: score -= 10

    if direction == "LONG":
        if rsi15m < 35 and rsi1h < 45: score += 10
        elif rsi15m < 40: score += 5
    else:
        if rsi15m > 65 and rsi1h > 55: score += 10
        elif rsi15m > 60: score += 5

    best_vol = max(vol1h, vol15m)
    if best_vol > 2.0: score += 15
    elif best_vol > 1.5: score += 10
    elif best_vol > 1.2: score += 5
    elif best_vol < 0.5: score -= 5

    bullish_patterns = {"hammer", "bullish_engulfing", "three_soldiers", "doji"}
    bearish_patterns = {"shooting_star", "bearish_engulfing", "three_crows", "doji"}
    relevant_patterns = bullish_patterns if direction == "LONG" else bearish_patterns
    found = set(patterns15m) & relevant_patterns
    if found: score += min(15, len(found) * 8)
    found_1h = set(patterns1h) & relevant_patterns
    if found_1h: score += min(5, len(found_1h) * 3)

    if direction == "LONG" and momentum15m > 0.1: score += 10
    elif direction == "LONG" and momentum15m < -0.3: score += 5
    elif direction == "SHORT" and momentum15m < -0.1: score += 10
    elif direction == "SHORT" and momentum15m > 0.3: score += 5

    if direction == "LONG" and chg4h > 1: score += 10
    elif direction == "LONG" and chg4h < -2: score += 7
    elif direction == "SHORT" and chg4h < -1: score += 10
    elif direction == "SHORT" and chg4h > 2: score += 7

    if divergence == "bullish" and direction == "LONG": score += 10
    elif divergence == "bearish" and direction == "SHORT": score += 10
    elif divergence == "bullish" and direction == "SHORT": score -= 5
    elif divergence == "bearish" and direction == "LONG": score -= 5

    # v5: Hourly trend alignment (passed in, not from tf_data)
    if direction == "LONG" and hourly_trend == "UP": score += 20
    elif direction == "SHORT" and hourly_trend == "DOWN": score += 20
    elif direction == "LONG" and hourly_trend == "DOWN": score -= 30
    elif direction == "SHORT" and hourly_trend == "UP": score -= 30

    details = {
        "rsi1h": rsi1h, "rsi15m": rsi15m,
        "volRatio1h": vol1h, "volRatio15m": vol15m,
        "trend4h": trend, "trendStrength": tf_data.get("trendStrength", 0),
        "patterns15m": patterns15m, "patterns1h": patterns1h,
        "momentum15m": momentum15m, "divergence": divergence,
        "chg1h": tf_data.get("chg1h", 0), "chg4h": chg4h,
        "chg24h": tf_data.get("chg24h", 0),
        "support": tf_data.get("support"), "resistance": tf_data.get("resistance"),
        "atrPct": tf_data.get("atrPct", 0),
        "hourlyTrend": hourly_trend
    }
    return max(0, min(100, round(score))), details

def score_funding(funding_rate, direction):
    score = 0
    ann_rate = funding_rate * 24 * 365 * 100
    favorable = (direction == "LONG" and funding_rate <= 0) or \
                (direction == "SHORT" and funding_rate >= 0)

    if abs(ann_rate) < 5: score += 40
    elif abs(ann_rate) < 15: score += 25 if favorable else 15

    if favorable:
        if abs(ann_rate) > 50: score += 35
        elif abs(ann_rate) > 15: score += 25
        elif abs(ann_rate) > 5: score += 15
    else:
        if abs(ann_rate) > 50: score -= 20
        elif abs(ann_rate) > 15: score -= 10

    details = {
        "rate": round(funding_rate * 100, 4),
        "annualized": round(ann_rate, 1),
        "favorable": favorable
    }
    return max(0, min(100, round(score))), details

def suggest_leverage(final_score, direction, rsi, funding_favorable, atr_pct=0):
    """v4: factors in asset volatility via ATR. Configurable thresholds."""
    base = 3
    if final_score > 350: base = 10
    elif final_score > 280: base = 7
    elif final_score > 200: base = 5

    # v4: scale down for volatile assets
    high_thresh = VOL_LEV.get("highVolThreshold", 3.0)
    high_pen = VOL_LEV.get("highVolPenalty", 3)
    med_thresh = VOL_LEV.get("medVolThreshold", 1.5)
    med_pen = VOL_LEV.get("medVolPenalty", 1)

    if atr_pct > high_thresh:
        base = max(2, base - high_pen)
    elif atr_pct > med_thresh:
        base = max(2, base - med_pen)

    if direction == "LONG" and rsi > 65:
        base = max(2, base - 2)
    elif direction == "SHORT" and rsi < 35:
        base = max(2, base - 2)

    if funding_favorable and base < 10:
        base += 1

    return min(10, base)

# ─── v4: Parallel candle fetcher ───

def classify_hourly_trend(candles_1h):
    """v5: Analyze hourly candles for HH/HL or LH/LL structure."""
    if len(candles_1h) < 8:
        return "NEUTRAL"
    highs = [float(c["h"]) for c in candles_1h]
    lows = [float(c["l"]) for c in candles_1h]
    swing_highs, swing_lows = [], []
    for i in range(3, len(candles_1h) - 3):
        if highs[i] == max(highs[i-3:i+4]):
            swing_highs.append((i, highs[i]))
        if lows[i] == min(lows[i-3:i+4]):
            swing_lows.append((i, lows[i]))
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "NEUTRAL"
    recent_highs = [h for _, h in swing_highs[-3:]]
    recent_lows = [l for _, l in swing_lows[-3:]]
    hh = all(recent_highs[i] > recent_highs[i-1] for i in range(1, len(recent_highs)))
    hl = all(recent_lows[i] > recent_lows[i-1] for i in range(1, len(recent_lows)))
    lh = all(recent_highs[i] < recent_highs[i-1] for i in range(1, len(recent_highs)))
    ll = all(recent_lows[i] < recent_lows[i-1] for i in range(1, len(recent_lows)))
    if hh and hl: return "UP"
    elif lh and ll: return "DOWN"
    elif hh or hl: return "UP"
    elif lh or ll: return "DOWN"
    return "NEUTRAL"

def fetch_asset_candles(name, now_ms):
    """Fetch 3 timeframes for one asset. Returns (name, 4h, 1h, 15m) with per-TF error recovery."""
    candles_4h, candles_1h, candles_15m = [], [], None

    try:
        candles_4h = fetch_json({
            "type": "candleSnapshot",
            "req": {"coin": name, "interval": "4h",
                    "startTime": now_ms - (7 * 24 * 3600 * 1000), "endTime": now_ms}
        })
    except Exception:
        pass

    try:
        candles_1h = fetch_json({
            "type": "candleSnapshot",
            "req": {"coin": name, "interval": "1h",
                    "startTime": now_ms - (3 * 24 * 3600 * 1000), "endTime": now_ms}
        })
    except Exception:
        pass

    try:
        candles_15m = fetch_json({
            "type": "candleSnapshot",
            "req": {"coin": name, "interval": "15m",
                    "startTime": now_ms - (6 * 3600 * 1000), "endTime": now_ms}
        })
    except Exception:
        candles_15m = None

    return name, candles_4h, candles_1h, candles_15m

# ═══════════════════════════════════════════
# STAGE 0: BTC Macro Context (v4)
# ═══════════════════════════════════════════

btc_context = {"trend": "neutral", "strength": 0, "chg1h": 0, "macroModifier": {"LONG": 0, "SHORT": 0}}

if not DISABLE_MACRO:
    print("Stage 0: Fetching BTC macro context...", file=sys.stderr)
    try:
        now_ms = int(time.time() * 1000)
        btc_4h = fetch_json({
            "type": "candleSnapshot",
            "req": {"coin": "BTC", "interval": "4h",
                    "startTime": now_ms - (7 * 24 * 3600 * 1000), "endTime": now_ms}
        })
        btc_1h = fetch_json({
            "type": "candleSnapshot",
            "req": {"coin": "BTC", "interval": "1h",
                    "startTime": now_ms - (24 * 3600 * 1000), "endTime": now_ms}
        })
        btc_trend, btc_strength = analyze_trend(btc_4h)
        btc_chg = price_changes(btc_1h)
        btc_1h_chg = btc_chg.get("chg1h", 0)

        long_mod = 0
        short_mod = 0
        if btc_trend == "strong_down" and btc_1h_chg < -1:
            long_mod = MACRO_MODS.get("strongDownLong", -40)
            short_mod = MACRO_MODS.get("strongDownShort", 15)
        elif btc_trend == "down":
            long_mod = MACRO_MODS.get("downLong", -20)
            short_mod = MACRO_MODS.get("downShort", 10)
        elif btc_trend == "strong_up" and btc_1h_chg > 1:
            long_mod = MACRO_MODS.get("strongUpLong", 15)
            short_mod = MACRO_MODS.get("strongUpShort", -40)
        elif btc_trend == "up":
            long_mod = MACRO_MODS.get("upLong", 10)
            short_mod = MACRO_MODS.get("upShort", -20)

        btc_context = {
            "trend": btc_trend, "strength": btc_strength,
            "chg1h": btc_1h_chg,
            "macroModifier": {"LONG": long_mod, "SHORT": short_mod}
        }
        print(f"Stage 0: BTC {btc_trend} (str={btc_strength}), 1h chg={btc_1h_chg}%, LONG mod={long_mod}, SHORT mod={short_mod}", file=sys.stderr)
    except Exception as e:
        print(f"Stage 0: BTC macro fetch failed ({e}), continuing without", file=sys.stderr)

# ═══════════════════════════════════════════
# STAGE 1: Bulk screen (all assets)
# ═══════════════════════════════════════════

print("Stage 1: Fetching market structure for all assets...", file=sys.stderr)
now_ms = int(time.time() * 1000)
meta_raw = fetch_json({"type": "metaAndAssetCtxs"})
meta_info = meta_raw[0]["universe"]
meta_ctx = meta_raw[1]

assets = {}
for i, (info, ctx) in enumerate(zip(meta_info, meta_ctx)):
    name = info["name"]
    try:
        funding = float(ctx.get("funding", 0))
        vol24h = float(ctx.get("dayNtlVlm", 0))
        oi = float(ctx.get("openInterest", 0))
        mark = float(ctx.get("markPx", 0))
    except (ValueError, TypeError):
        continue
    if vol24h < MIN_VOLUME_24H:
        continue
    assets[name] = {
        "funding": funding, "volume24h": vol24h,
        "openInterest": oi, "markPrice": mark,
        "prevDayVolume": vol24h,
    }

print(f"Stage 1: {len(assets)} assets pass volume filter (of {len(meta_info)} total)", file=sys.stderr)

# ═══════════════════════════════════════════
# STAGE 2: Smart money overlay
# ═══════════════════════════════════════════

print("Stage 2: Fetching smart money data...", file=sys.stderr)
sm_by_asset = {}
try:
    momentum_raw = fetch_mcporter("leaderboard_get_markets")
    markets_list = momentum_raw.get("data", {}).get("markets", {}).get("markets", [])

    for item in markets_list:
        name = item.get("token", "")
        if name not in assets:
            continue
        pnl = float(item.get("pct_of_top_traders_gain", 0)) * 100
        accel = item.get("contribution_pct_change_4h")
        entry = {
            "pnlContributionPct": pnl,
            "traderCount": int(item.get("trader_count", 0)),
            "contributionChange4h": float(accel) if accel is not None else 0.0,
            "dominantDirection": item.get("direction", "long").upper()
        }
        if name not in sm_by_asset or pnl > sm_by_asset[name]["pnlContributionPct"]:
            sm_by_asset[name] = entry

    print("Stage 2b: Fetching top trader peak data...", file=sys.stderr)
    try:
        top_traders_raw = fetch_mcporter("leaderboard_get_top", "-p limit=100")
        top_traders = top_traders_raw.get("data", {}).get("leaderboard", {}).get("data", [])

        from collections import defaultdict
        market_peaks = defaultdict(list)
        for t in top_traders:
            upnl = t.get("unrealized_pnl", 0)
            ath = t.get("ath_delta", 0)
            ratio = upnl / ath if ath > 0 else 0
            for m in t.get("top_markets", []):
                market_peaks[m].append(ratio)

        for name in sm_by_asset:
            if name in market_peaks:
                ratios = market_peaks[name]
                avg_at_peak = sum(ratios) / len(ratios)
                near_peak_pct = sum(1 for r in ratios if r > 0.85) / len(ratios)
                sm_by_asset[name]["avgAtPeak"] = round(avg_at_peak * 100, 1)
                sm_by_asset[name]["nearPeakPct"] = round(near_peak_pct * 100, 1)
    except Exception as e:
        print(f"Stage 2b: Peak data fetch failed ({e})", file=sys.stderr)

    print(f"Stage 2: Smart money data for {len(sm_by_asset)} assets", file=sys.stderr)
except Exception as e:
    print(f"Stage 2: Smart money fetch failed ({e})", file=sys.stderr)

# Quick score and pick top N
quick_scores = []
for name, meta in assets.items():
    sm = sm_by_asset.get(name, {})
    sm_score, direction, _ = score_smart_money(sm)
    if not sm:
        direction = "LONG" if meta["funding"] < 0 else "SHORT"
    ms_score, _ = score_market_structure(meta)
    fund_score, _ = score_funding(meta["funding"], direction)
    quick = (sm_score * W_SMART_MONEY + ms_score * W_MARKET_STRUCTURE + fund_score * W_FUNDING) / (1 - W_TECHNICALS)
    quick_scores.append((name, quick, direction))

sm_top = sorted(sm_by_asset.items(), key=lambda x: x[1]["pnlContributionPct"], reverse=True)
forced_assets = set(name for name, _ in sm_top[:8] if name in assets)

quick_scores.sort(key=lambda x: x[1], reverse=True)
top_names = set()
top_assets = []
for item in quick_scores:
    if len(top_assets) >= TOP_N_DEEP and item[0] not in forced_assets:
        continue
    if item[0] not in top_names:
        top_assets.append(item)
        top_names.add(item[0])
    if len(top_names) >= TOP_N_DEEP + len(forced_assets):
        break

print(f"Stage 2: Top {len(top_assets)} for deep analysis: {[a[0] for a in top_assets]}", file=sys.stderr)

# ═══════════════════════════════════════════
# STAGE 3: Deep dive (v4: PARALLEL candle fetches)
# ═══════════════════════════════════════════

print(f"Stage 3: Fetching multi-TF candles (parallel, workers={MAX_WORKERS})...", file=sys.stderr)
now_ms = int(time.time() * 1000)

candle_data = {}
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {
        executor.submit(fetch_asset_candles, name, now_ms): (name, quick, direction)
        for name, quick, direction in top_assets
    }
    for future in as_completed(futures):
        try:
            name, c4h, c1h, c15m = future.result()
            candle_data[name] = (c4h, c1h, c15m)
            print(f"  {name}: done (4h={len(c4h)} 1h={len(c1h)} 15m={len(c15m) if c15m else 0})", file=sys.stderr)
        except Exception as e:
            orig = futures[future]
            print(f"  {orig[0]}: failed ({e})", file=sys.stderr)

results = []
for name, quick, direction in top_assets:
    if name not in candle_data:
        continue
    candles_4h, candles_1h, candles_15m = candle_data[name]
    if not candles_1h:
        continue

    current_price = assets[name]["markPrice"]
    tf_data = multi_tf_analysis(candles_1h, candles_15m, candles_4h, current_price)
    # v5: Hourly trend classification
    hourly_trend = classify_hourly_trend(candles_1h)
    tf_data["hourlyTrend"] = hourly_trend

    sm = sm_by_asset.get(name, {})
    sm_score, _, sm_details = score_smart_money(sm)
    ms_score, ms_details = score_market_structure(assets[name])
    tech_score, tech_details = score_technicals(tf_data, direction, hourly_trend)
    fund_score, fund_details = score_funding(assets[name]["funding"], direction)

    final = round(
        sm_score * W_SMART_MONEY * 4 +
        ms_score * W_MARKET_STRUCTURE * 4 +
        tech_score * W_TECHNICALS * 4 +
        fund_score * W_FUNDING * 4
    )

    # v4: apply BTC macro modifier
    macro_mod = btc_context["macroModifier"].get(direction, 0)
    final = max(0, final + macro_mod)

    atr_pct = tech_details.get("atrPct", 0)
    rsi1h = tech_details.get("rsi1h", 50)
    lev = suggest_leverage(final, direction, rsi1h, fund_details["favorable"], atr_pct)

    risks = []
    if direction == "LONG" and rsi1h > 65:
        risks.append("overbought RSI")
    elif direction == "SHORT" and rsi1h < 35:
        risks.append("oversold RSI")
    best_vol = max(tech_details.get("volRatio1h", 1), tech_details.get("volRatio15m", 1))
    if best_vol < 0.5: risks.append("volume dying")
    elif best_vol < 0.7: risks.append("volume declining")
    if not fund_details["favorable"]:
        risks.append(f"funding against you ({fund_details['annualized']:+.1f}% ann)")
    if abs(tech_details.get("chg24h", 0)) > 5:
        risks.append("extended move, may revert")
    if sm_score < 10: risks.append("weak smart money signal")
    if ms_score < 20: risks.append("low volume/OI")
    trend = tech_details.get("trend4h", "neutral")
    if direction == "LONG" and trend in ("strong_down", "down"):
        risks.append("counter-trend (4h downtrend)")
    elif direction == "SHORT" and trend in ("strong_up", "up"):
        risks.append("counter-trend (4h uptrend)")
    div = tech_details.get("divergence")
    if div == "bullish" and direction == "SHORT":
        risks.append("bullish vol divergence (15m)")
    elif div == "bearish" and direction == "LONG":
        risks.append("bearish vol divergence (15m)")
    if macro_mod < -15:
        risks.append(f"BTC macro headwind ({macro_mod:+d} pts)")

    results.append({
        "asset": name, "direction": direction, "leverage": lev,
        "finalScore": final,
        "pillarScores": {
            "smartMoney": sm_score, "marketStructure": ms_score,
            "technicals": tech_score, "funding": fund_score
        },
        "smartMoney": sm_details, "marketStructure": ms_details,
        "technicals": tech_details, "funding": fund_details,
        "markPrice": assets[name]["markPrice"],
        "risks": risks
    })

results.sort(key=lambda x: x["finalScore"], reverse=True)

# ═══════════════════════════════════════════
# v5: Hard disqualifier check
# ═══════════════════════════════════════════
disqualified = []
qualified_results = []
for r in results:
    hourly = r["technicals"].get("hourlyTrend", "NEUTRAL")
    direction = r["direction"]
    rsi1h = r["technicals"].get("rsi1h", 50)
    best_vol = max(r["technicals"].get("volRatio1h", 1), r["technicals"].get("volRatio15m", 1))
    fund_ann = abs(r["funding"].get("annualized", 0))
    fund_fav = r["funding"].get("favorable", True)
    macro_mod = btc_context["macroModifier"].get(direction, 0)

    dq_reason = None
    if HOURLY_GATE:
        if hourly == "DOWN" and direction == "LONG":
            dq_reason = f"counter-trend on hourly (hourlyTrend=DOWN)"
        elif hourly == "UP" and direction == "SHORT":
            dq_reason = f"counter-trend on hourly (hourlyTrend=UP)"
    if not dq_reason and direction == "SHORT" and rsi1h < 20:
        dq_reason = f"extreme oversold RSI ({rsi1h})"
    if not dq_reason and direction == "LONG" and rsi1h > 80:
        dq_reason = f"extreme overbought RSI ({rsi1h})"
    if not dq_reason and best_vol < 0.5:
        dq_reason = "volume dying (both TFs < 0.5)"
    if not dq_reason and not fund_fav and fund_ann > 50:
        dq_reason = f"funding heavily against ({fund_ann:.0f}% ann)"
    if not dq_reason and macro_mod < -30:
        dq_reason = f"BTC macro headwind ({macro_mod:+d} pts)"

    if dq_reason:
        disqualified.append({"asset": r["asset"], "direction": direction, "reason": dq_reason, "wouldHaveScored": r["finalScore"]})
    else:
        r["hourlyTrend"] = hourly
        r["trendAligned"] = True
        qualified_results.append(r)

results = qualified_results

# ═══════════════════════════════════════════
# STAGE 4: Cross-scan momentum tracking (v4)
# ═══════════════════════════════════════════

history_file = os.path.join(WORKSPACE, "scan-history.json")
history = []
if os.path.exists(history_file):
    try:
        with open(history_file) as f:
            history = json.load(f)
    except Exception:
        history = []

current_scores = {
    r["asset"]: {"score": r["finalScore"], "dir": r["direction"]}
    for r in results[:15]
}

if len(history) >= 1:
    prev = history[-1].get("scores", {})
    for r in results:
        prev_entry = prev.get(r["asset"])
        if prev_entry and prev_entry.get("dir") == r["direction"]:
            r["scoreDelta"] = r["finalScore"] - prev_entry["score"]
        else:
            r["scoreDelta"] = None

        streak = 0
        for scan in reversed(history):
            if r["asset"] in scan.get("scores", {}):
                streak += 1
            else:
                break
        r["scanStreak"] = streak + 1  # +1 for current scan
else:
    for r in results:
        r["scoreDelta"] = None
        r["scanStreak"] = 1

scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
history.append({"time": scan_time, "scores": current_scores})
history = history[-SCAN_HISTORY_SIZE:]

try:
    with open(history_file, "w") as f:
        json.dump(history, f, indent=2)
except Exception:
    pass

# ═══════════════════════════════════════════
# OUTPUT
# ═══════════════════════════════════════════

output = {
    "scanTime": scan_time,
    "assetsScanned": len(meta_info),
    "passedStage1": len(assets),
    "passedStage2": len(top_assets),
    "deepDived": len(results) + len(disqualified),
    "disqualified": len(disqualified),
    "disqualifiedAssets": disqualified[:5],
    "btcContext": btc_context,
    "pillarWeights": {
        "smartMoney": W_SMART_MONEY, "marketStructure": W_MARKET_STRUCTURE,
        "technicals": W_TECHNICALS, "funding": W_FUNDING
    },
    "opportunities": results[:15]
}

print(json.dumps(output, indent=2))
print(f"\nDone. {len(results)} scored opportunities.", file=sys.stderr)
