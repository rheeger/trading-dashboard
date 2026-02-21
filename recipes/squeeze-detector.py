#!/usr/bin/env python3
"""
Volatility Squeeze Breakout Detector
Scans Hyperliquid perps for Bollinger Band / Keltner Channel squeezes.
Outputs scored setups with ATR-based SL/TP. Zero LLM tokens.
"""

import json, sys, subprocess, os, math, time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(SCRIPT_DIR)
CONFIG_FILE = os.path.join(WORKSPACE, "squeeze-config.json")

cfg = {}
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)

BB_PERIOD = cfg.get("bbPeriod", 20)
BB_STD = cfg.get("bbStd", 2.0)
KC_PERIOD = cfg.get("kcPeriod", 20)
KC_ATR_MULT = cfg.get("kcAtrMultiplier", 1.5)
ATR_PERIOD = cfg.get("atrPeriod", 14)
MIN_SQUEEZE = cfg.get("minSqueezeDuration", 3)
ADX_THRESH = cfg.get("adxThreshold", 15)
VOL_SURGE_THRESH = cfg.get("volumeSurgeThreshold", 1.5)
SL_MULT = cfg.get("slMultiplier", 1.5)
TP_MULT = cfg.get("tpMultiplier", 3.0)
MIN_SCORE = cfg.get("minScore", 50)
MAX_WORKERS = cfg.get("maxWorkers", 8)
TOP_N = cfg.get("topNAssets", 30)
MIN_VOL = cfg.get("minVolume24h", 1_000_000)
USE_BTC = cfg.get("useBtcMacro", True)

def fetch_json(payload):
    r = subprocess.run(
        ["curl", "-s", "https://api.hyperliquid.xyz/info",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=30
    )
    return json.loads(r.stdout)

def calc_sma(values, period):
    if len(values) < period:
        return []
    return [sum(values[i:i+period]) / period for i in range(len(values) - period + 1)]

def calc_ema(values, period):
    if not values:
        return []
    ema = [values[0]]
    k = 2 / (period + 1)
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema

def calc_std(values, period):
    if len(values) < period:
        return []
    result = []
    for i in range(len(values) - period + 1):
        window = values[i:i+period]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        result.append(math.sqrt(variance))
    return result

def calc_atr_series(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]["h"])
        l = float(candles[i]["l"])
        pc = float(candles[i-1]["c"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return []
    atr = [sum(trs[:period]) / period]
    for i in range(period, len(trs)):
        atr.append((atr[-1] * (period - 1) + trs[i]) / period)
    return atr

def calc_adx(candles, period=14):
    if len(candles) < period * 2:
        return 0
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        h = float(candles[i]["h"])
        l = float(candles[i]["l"])
        ph = float(candles[i-1]["h"])
        pl = float(candles[i-1]["l"])
        pc = float(candles[i-1]["c"])
        up = h - ph
        down = pl - l
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    if len(trs) < period:
        return 0

    atr = sum(trs[:period])
    plus_di_sum = sum(plus_dm[:period])
    minus_di_sum = sum(minus_dm[:period])
    dx_values = []

    for i in range(period, len(trs)):
        atr = atr - atr / period + trs[i]
        plus_di_sum = plus_di_sum - plus_di_sum / period + plus_dm[i]
        minus_di_sum = minus_di_sum - minus_di_sum / period + minus_dm[i]
        if atr == 0:
            continue
        plus_di = (plus_di_sum / atr) * 100
        minus_di = (minus_di_sum / atr) * 100
        di_sum = plus_di + minus_di
        if di_sum == 0:
            continue
        dx_values.append(abs(plus_di - minus_di) / di_sum * 100)

    if len(dx_values) < period:
        return sum(dx_values) / len(dx_values) if dx_values else 0
    adx = sum(dx_values[:period]) / period
    for i in range(period, len(dx_values)):
        adx = (adx * (period - 1) + dx_values[i]) / period
    return round(adx, 1)

def detect_squeeze(candles):
    """Returns (squeeze_states, bb_upper, bb_lower, kc_upper, kc_lower) for the overlap period."""
    closes = [float(c["c"]) for c in candles]
    if len(closes) < max(BB_PERIOD, KC_PERIOD) + ATR_PERIOD:
        return [], [], [], [], []

    sma = calc_sma(closes, BB_PERIOD)
    std = calc_std(closes, BB_PERIOD)
    ema = calc_ema(closes, KC_PERIOD)
    atr = calc_atr_series(candles, ATR_PERIOD)

    min_len = min(len(sma), len(std), len(ema), len(atr))
    if min_len == 0:
        return [], [], [], [], []

    sma = sma[-min_len:]
    std = std[-min_len:]
    ema_k = ema[-min_len:]
    atr_k = atr[-min_len:]

    bb_upper = [sma[i] + BB_STD * std[i] for i in range(min_len)]
    bb_lower = [sma[i] - BB_STD * std[i] for i in range(min_len)]
    kc_upper = [ema_k[i] + KC_ATR_MULT * atr_k[i] for i in range(min_len)]
    kc_lower = [ema_k[i] - KC_ATR_MULT * atr_k[i] for i in range(min_len)]

    squeeze_states = []
    for i in range(min_len):
        is_squeeze = bb_upper[i] < kc_upper[i] and bb_lower[i] > kc_lower[i]
        squeeze_states.append(is_squeeze)

    return squeeze_states, bb_upper, bb_lower, kc_upper, kc_lower

def analyze_asset(name, candles, sm_data=None, btc_mod=None):
    if len(candles) < BB_PERIOD + ATR_PERIOD + 5:
        return None

    squeeze_states, bb_upper, bb_lower, kc_upper, kc_lower = detect_squeeze(candles)
    if not squeeze_states:
        return None

    closes = [float(c["c"]) for c in candles]
    current_price = closes[-1]
    atr_series = calc_atr_series(candles, ATR_PERIOD)
    current_atr = atr_series[-1] if atr_series else 0
    atr_pct = round(current_atr / current_price * 100, 3) if current_price else 0
    adx = calc_adx(candles, 14)

    squeeze_duration = 0
    for s in reversed(squeeze_states[:-1]):
        if s:
            squeeze_duration += 1
        else:
            break

    currently_squeezing = squeeze_states[-1]
    was_squeezing = squeeze_duration >= MIN_SQUEEZE

    if not was_squeezing and not currently_squeezing:
        return None

    if currently_squeezing:
        state = "squeezing"
        direction = None
        breakout_price = None
    else:
        prev_squeezing = squeeze_states[-2] if len(squeeze_states) >= 2 else False
        if prev_squeezing and not currently_squeezing:
            state = "breakout"
        else:
            state = "extended"

        if current_price > bb_upper[-1]:
            direction = "LONG"
        elif current_price < bb_lower[-1]:
            direction = "SHORT"
        else:
            direction = "LONG" if closes[-1] > closes[-2] else "SHORT"
        breakout_price = current_price

    vol_avg = sum(float(c["v"]) for c in candles[-BB_PERIOD:]) / BB_PERIOD
    vol_current = float(candles[-1]["v"])
    vol_surge = round(vol_current / vol_avg, 2) if vol_avg > 0 else 1.0

    score = 0
    if squeeze_duration >= 10: score += 50
    elif squeeze_duration >= 6: score += 35
    elif squeeze_duration >= 3: score += 20

    if vol_surge > 2.0: score += 20
    elif vol_surge > VOL_SURGE_THRESH: score += 10

    if adx > 25: score += 15
    elif adx > ADX_THRESH: score += 10

    sm_aligned = False
    if sm_data and direction:
        sm_dir = sm_data.get("dominantDirection", "")
        if sm_dir == direction:
            score += 15
            sm_aligned = True

    funding_favorable = False
    if btc_mod and direction:
        score += btc_mod.get(direction, 0)

    risks = []
    if adx < ADX_THRESH:
        risks.append("ADX below threshold")
    if state == "squeezing":
        risks.append("no breakout yet")
    if state == "extended":
        risks.append("breakout may be extended")
    if vol_surge < VOL_SURGE_THRESH:
        risks.append("low volume on breakout")

    entry = breakout_price
    sl, tp, rr = None, None, None
    lev = None
    if entry and direction and current_atr > 0:
        if direction == "LONG":
            sl = round(entry - current_atr * SL_MULT, 4)
            tp = round(entry + current_atr * TP_MULT, 4)
        else:
            sl = round(entry + current_atr * SL_MULT, 4)
            tp = round(entry - current_atr * TP_MULT, 4)
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = round(reward / risk, 1) if risk > 0 else 0

        if score > 75: lev = 7
        elif score > 60: lev = 5
        elif score > 40: lev = 4
        else: lev = 3
        if atr_pct > 3.0: lev = max(2, lev - 2)
        elif atr_pct > 1.5: lev = max(2, lev - 1)

    return {
        "asset": name,
        "timeframe": "4h",
        "direction": direction,
        "score": min(100, max(0, score)),
        "squeezeState": state,
        "squeezeDuration": squeeze_duration,
        "breakoutPrice": breakout_price,
        "currentPrice": current_price,
        "atr": round(current_atr, 4),
        "atrPct": atr_pct,
        "suggestedEntry": entry,
        "suggestedStop": sl,
        "suggestedTarget": tp,
        "riskReward": rr,
        "suggestedLeverage": lev,
        "adx": adx,
        "volumeSurge": vol_surge,
        "smAligned": sm_aligned,
        "fundingFavorable": funding_favorable,
        "risks": risks
    }

def fetch_candles_for_asset(name, now_ms):
    try:
        candles = fetch_json({
            "type": "candleSnapshot",
            "req": {"coin": name, "interval": "4h",
                    "startTime": now_ms - (14 * 24 * 3600 * 1000), "endTime": now_ms}
        })
        return name, candles
    except Exception:
        return name, []

# ─── Main ───

print("Stage 1: Fetching market structure...", file=sys.stderr)
now_ms = int(time.time() * 1000)
meta_raw = fetch_json({"type": "metaAndAssetCtxs"})
meta_info = meta_raw[0]["universe"]
meta_ctx = meta_raw[1]

asset_list = []
for info, ctx in zip(meta_info, meta_ctx):
    name = info["name"]
    try:
        vol = float(ctx.get("dayNtlVlm", 0))
    except (ValueError, TypeError):
        continue
    if vol >= MIN_VOL:
        asset_list.append((name, vol))

asset_list.sort(key=lambda x: x[1], reverse=True)
asset_list = asset_list[:TOP_N]
print(f"Stage 1: {len(asset_list)} assets by volume", file=sys.stderr)

btc_context = {"trend": "neutral", "chg1h": 0}
btc_mod = {"LONG": 0, "SHORT": 0}
if USE_BTC:
    try:
        btc_4h = fetch_json({
            "type": "candleSnapshot",
            "req": {"coin": "BTC", "interval": "4h",
                    "startTime": now_ms - (7 * 24 * 3600 * 1000), "endTime": now_ms}
        })
        closes = [float(c["c"]) for c in btc_4h]
        ema5 = calc_ema(closes, 5)
        ema13 = calc_ema(closes, 13)
        if ema5 and ema13:
            if ema5[-1] < ema13[-1] and closes[-1] < ema5[-1]:
                btc_context["trend"] = "strong_down"
                btc_mod = {"LONG": -10, "SHORT": 5}
            elif ema5[-1] < ema13[-1]:
                btc_context["trend"] = "down"
                btc_mod = {"LONG": -5, "SHORT": 3}
            elif ema5[-1] > ema13[-1] and closes[-1] > ema5[-1]:
                btc_context["trend"] = "strong_up"
                btc_mod = {"LONG": 5, "SHORT": -10}
            elif ema5[-1] > ema13[-1]:
                btc_context["trend"] = "up"
                btc_mod = {"LONG": 3, "SHORT": -5}
    except Exception:
        pass

import time as _time

print(f"Stage 2: Fetching candles (parallel, workers={MAX_WORKERS})...", file=sys.stderr)
candle_data = {}
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {executor.submit(fetch_candles_for_asset, name, now_ms): name for name, _ in asset_list}
    for future in as_completed(futures):
        name, candles = future.result()
        if candles:
            candle_data[name] = candles

print(f"Stage 3: Detecting squeezes across {len(candle_data)} assets...", file=sys.stderr)
setups = []
for name, candles in candle_data.items():
    result = analyze_asset(name, candles, btc_mod=btc_mod)
    if result and result["score"] >= MIN_SCORE:
        setups.append(result)

setups.sort(key=lambda x: x["score"], reverse=True)

squeezing_count = sum(1 for s in setups if s["squeezeState"] == "squeezing")
breakout_count = sum(1 for s in setups if s["squeezeState"] == "breakout")

output = {
    "scanTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "assetsScanned": len(candle_data),
    "squeezesDetected": squeezing_count + breakout_count,
    "breakoutsDetected": breakout_count,
    "btcContext": btc_context,
    "setups": setups[:10]
}

print(json.dumps(output, indent=2))
print(f"\nDone. {len(setups)} setups found ({breakout_count} breakouts, {squeezing_count} squeezing).", file=sys.stderr)
