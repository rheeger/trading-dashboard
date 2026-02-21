#!/usr/bin/env python3
"""
Backtest Engine for Opportunity Scanner v4 configurations.

Replays scanner v4 scoring logic against historical candle data,
simulates position entry/exit with DSL-like stops, and compares
configs head-to-head.

Usage: python3 scripts/backtest-v2.py [--days 7] [--assets 20]
"""

import json, subprocess, sys, os, math, time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(SCRIPT_DIR)

# ─── CLI args ───
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--days", type=int, default=7, help="Lookback period in days")
parser.add_argument("--assets", type=int, default=20, help="Top N assets by volume to test")
parser.add_argument("--output", default="json", choices=["json", "summary"])
parser.add_argument("--fee-pct", type=float, default=0.07, help="One-way fee percentage")
args = parser.parse_args()

LOOKBACK_DAYS = args.days
TOP_N_ASSETS = args.assets
FEE_PCT = args.fee_pct / 100  # 0.07% one-way default on HL
ROUND_TRIP_FEE = FEE_PCT * 2

# ─── Configs to compare ───
CONFIGS = {
    "v1_current": {
        "label": "v1 (current): threshold 175, 5 positions, SM flips",
        "minScore": 175,
        "maxPositions": 5,
        "minHoldBars": 1,       # 1 bar = 4h
        "assetCooldownBars": 0,
        "requireTrendAlign": False,
        "requireMultiScanner": False,
        "disqualifyVolumeDeclining": False,
        "disqualifyRSIExtreme": False,
        "flipOnSMReverse": True,
        "smFlipThreshold": 0,   # any reversal
        "positionSizePct": 0.15, # ~$185 of $1230
        "stopLossPct": 0.03,
        "leverage": 5,
    },
    "v2_proposed": {
        "label": "v2 (proposed): threshold 220, 2 positions, no flips",
        "minScore": 220,
        "maxPositions": 2,
        "minHoldBars": 3,
        "assetCooldownBars": 6,
        "requireTrendAlign": True,
        "requireHourlyTrend": False,
        "requireMultiScanner": False,
        "disqualifyVolumeDeclining": True,
        "disqualifyRSIExtreme": True,
        "flipOnSMReverse": False,
        "positionSizePct": 0.35,
        "stopLossPct": 0.03,
        "leverage": 5,
    },
    "v5_moderate": {
        "label": "v5-moderate: threshold 200, 3 pos, hourly gate, no flips",
        "minScore": 200,
        "maxPositions": 3,
        "minHoldBars": 2,
        "assetCooldownBars": 4,
        "requireTrendAlign": True,
        "requireHourlyTrend": True,
        "requireMultiScanner": False,
        "disqualifyVolumeDeclining": True,
        "disqualifyRSIExtreme": True,
        "flipOnSMReverse": False,
        "positionSizePct": 0.25,
        "stopLossPct": 0.03,
        "leverage": 5,
    },
    "v5_moderate_175": {
        "label": "v5-moderate-175: threshold 175, 3 pos, hourly gate",
        "minScore": 175,
        "maxPositions": 3,
        "minHoldBars": 2,
        "assetCooldownBars": 4,
        "requireTrendAlign": True,
        "requireHourlyTrend": True,
        "requireMultiScanner": False,
        "disqualifyVolumeDeclining": True,
        "disqualifyRSIExtreme": True,
        "flipOnSMReverse": False,
        "positionSizePct": 0.25,
        "stopLossPct": 0.03,
        "leverage": 5,
    },
    "v2_moderate": {
        "label": "v2-moderate: threshold 200, 3 positions (no hourly gate)",
        "minScore": 200,
        "maxPositions": 3,
        "minHoldBars": 2,
        "assetCooldownBars": 4,
        "requireTrendAlign": True,
        "requireHourlyTrend": False,
        "requireMultiScanner": False,
        "disqualifyVolumeDeclining": True,
        "disqualifyRSIExtreme": False,
        "flipOnSMReverse": False,
        "positionSizePct": 0.25,
        "stopLossPct": 0.03,
        "leverage": 5,
    },
}

# ─── Data fetching ───
def fetch_json(payload):
    r = subprocess.run(
        ["curl", "-s", "https://api.hyperliquid.xyz/info",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=30
    )
    return json.loads(r.stdout)

def fetch_candles(asset, interval, start_ms, end_ms):
    return fetch_json({
        "type": "candleSnapshot",
        "req": {"coin": asset, "interval": interval,
                "startTime": start_ms, "endTime": end_ms}
    })

# ─── Technical indicators (same as scanner v4) ───
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

def calc_atr_pct(candles, period=14):
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = float(candles[i]["h"]), float(candles[i]["l"]), float(candles[i-1]["c"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    price = float(candles[-1]["c"])
    return round(atr / price * 100, 3) if price else 0

def analyze_trend(closes):
    if len(closes) < 13:
        return "neutral", 0
    ema5 = calc_ema(closes, 5)
    ema13 = calc_ema(closes, 13)
    fast, slow, price = ema5[-1], ema13[-1], closes[-1]
    if fast > slow and price > fast:
        return "strong_up", min(100, int((fast - slow) / slow * 1000))
    elif fast > slow:
        return "up", min(70, int((fast - slow) / slow * 500))
    elif fast < slow and price < fast:
        return "strong_down", min(100, int((slow - fast) / slow * 1000))
    elif fast < slow:
        return "down", min(70, int((slow - fast) / slow * 500))
    return "neutral", 0

def volume_ratio(candles, recent_n=4):
    if len(candles) < 6:
        return 1.0
    recent = candles[-recent_n:]
    prior = candles[:-recent_n]
    if not prior:
        return 1.0
    r_avg = sum(float(c["v"]) for c in recent) / len(recent)
    p_avg = sum(float(c["v"]) for c in prior) / len(prior)
    return round(r_avg / p_avg, 2) if p_avg else 1.0

def detect_patterns(candles):
    if len(candles) < 3:
        return []
    patterns = []
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    def body(c): return abs(float(c["c"]) - float(c["o"]))
    def full_range(c): return float(c["h"]) - float(c["l"])
    def is_bull(c): return float(c["c"]) > float(c["o"])
    def lower_wick(c): return min(float(c["c"]), float(c["o"])) - float(c["l"])
    def upper_wick(c): return float(c["h"]) - max(float(c["c"]), float(c["o"]))

    fr3, b3 = full_range(c3), body(c3)
    if fr3 > 0:
        if lower_wick(c3) > b3 * 2 and upper_wick(c3) < b3 * 0.5:
            patterns.append("hammer")
        if upper_wick(c3) > b3 * 2 and lower_wick(c3) < b3 * 0.5:
            patterns.append("shooting_star")
    if not is_bull(c2) and is_bull(c3) and float(c3["c"]) > float(c2["o"]) and float(c3["o"]) < float(c2["c"]):
        patterns.append("bullish_engulfing")
    if is_bull(c2) and not is_bull(c3) and float(c3["c"]) < float(c2["o"]) and float(c3["o"]) > float(c2["c"]):
        patterns.append("bearish_engulfing")
    if is_bull(c1) and is_bull(c2) and is_bull(c3) and float(c3["c"]) > float(c2["c"]) > float(c1["c"]):
        patterns.append("three_soldiers")
    if not is_bull(c1) and not is_bull(c2) and not is_bull(c3) and float(c3["c"]) < float(c2["c"]) < float(c1["c"]):
        patterns.append("three_crows")
    return patterns

# ─── Scoring (simplified v4 — no SM since we can't backtest it) ───
def classify_hourly_trend_bt(candles_1h):
    """v5: HH/HL or LH/LL structure on hourly candles."""
    if len(candles_1h) < 8:
        return "NEUTRAL"
    highs = [float(c["h"]) for c in candles_1h]
    lows = [float(c["l"]) for c in candles_1h]
    swing_highs, swing_lows = [], []
    for i in range(3, len(candles_1h) - 3):
        if highs[i] == max(highs[i-3:i+4]):
            swing_highs.append(highs[i])
        if lows[i] == min(lows[i-3:i+4]):
            swing_lows.append(lows[i])
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "NEUTRAL"
    rh = swing_highs[-3:]
    rl = swing_lows[-3:]
    hh = all(rh[i] > rh[i-1] for i in range(1, len(rh)))
    hl = all(rl[i] > rl[i-1] for i in range(1, len(rl)))
    lh = all(rh[i] < rh[i-1] for i in range(1, len(rh)))
    ll = all(rl[i] < rl[i-1] for i in range(1, len(rl)))
    if hh and hl: return "UP"
    elif lh and ll: return "DOWN"
    elif hh or hl: return "UP"
    elif lh or ll: return "DOWN"
    return "NEUTRAL"

def score_at_bar(candles_4h, candles_1h, funding_rate, vol24h, oi):
    """Score an asset at a point in time. Returns (score, direction, details)."""
    if len(candles_4h) < 15 or len(candles_1h) < 15:
        return 0, None, {}

    closes_4h = [float(c["c"]) for c in candles_4h]
    closes_1h = [float(c["c"]) for c in candles_1h]
    current_price = closes_1h[-1]

    trend, trend_str = analyze_trend(closes_4h)
    rsi_1h = calc_rsi(closes_1h)
    rsi_4h = calc_rsi(closes_4h)
    vol_ratio = volume_ratio(candles_1h, 4)
    atr_pct = calc_atr_pct(candles_1h)
    patterns = detect_patterns(candles_1h)

    # Determine direction from trend
    if trend in ("strong_up", "up"):
        direction = "LONG"
    elif trend in ("strong_down", "down"):
        direction = "SHORT"
    else:
        # Neutral — use RSI
        direction = "LONG" if rsi_1h < 45 else "SHORT" if rsi_1h > 55 else None
        if direction is None:
            return 0, None, {}

    # --- Pillar 1: Market Structure (no SM available) ---
    ms_score = 0
    if vol24h > 50_000_000: ms_score += 30
    elif vol24h > 10_000_000: ms_score += 20
    elif vol24h > 1_000_000: ms_score += 10
    if vol_ratio > 2.0: ms_score += 30
    elif vol_ratio > 1.3: ms_score += 20
    elif vol_ratio > 1.0: ms_score += 10
    if oi > 10_000_000: ms_score += 20
    elif oi > 1_000_000: ms_score += 10
    ms_score = min(100, ms_score)

    # --- Pillar 2: Technicals ---
    tech_score = 0
    if direction == "LONG":
        if trend in ("strong_up",): tech_score += 20
        elif trend == "up": tech_score += 15
        if rsi_1h < 30: tech_score += 20
        elif rsi_1h < 40: tech_score += 15
        elif rsi_1h < 55: tech_score += 8
    else:
        if trend in ("strong_down",): tech_score += 20
        elif trend == "down": tech_score += 15
        if rsi_1h > 70: tech_score += 20
        elif rsi_1h > 60: tech_score += 15
        elif rsi_1h > 45: tech_score += 8

    if vol_ratio > 2.0: tech_score += 15
    elif vol_ratio > 1.5: tech_score += 10
    elif vol_ratio > 1.2: tech_score += 5

    bullish_p = {"hammer", "bullish_engulfing", "three_soldiers"}
    bearish_p = {"shooting_star", "bearish_engulfing", "three_crows"}
    relevant = bullish_p if direction == "LONG" else bearish_p
    found = set(patterns) & relevant
    if found: tech_score += min(15, len(found) * 8)

    chg_4h = (closes_4h[-1] - closes_4h[-4]) / closes_4h[-4] * 100 if len(closes_4h) >= 4 else 0
    if direction == "LONG" and chg_4h > 1: tech_score += 10
    elif direction == "SHORT" and chg_4h < -1: tech_score += 10

    tech_score = max(0, min(100, tech_score))

    # --- Pillar 3: Funding ---
    ann_rate = funding_rate * 24 * 365 * 100
    favorable = (direction == "LONG" and funding_rate <= 0) or \
                (direction == "SHORT" and funding_rate >= 0)
    fund_score = 0
    if abs(ann_rate) < 5: fund_score += 40
    elif abs(ann_rate) < 15: fund_score += 25 if favorable else 15
    if favorable:
        if abs(ann_rate) > 50: fund_score += 35
        elif abs(ann_rate) > 15: fund_score += 25
    else:
        if abs(ann_rate) > 50: fund_score -= 20
    fund_score = max(0, min(100, fund_score))

    # --- Pillar 4: Pseudo-SM (use momentum as proxy) ---
    # Since we can't get historical SM, use price momentum + volume as proxy
    sm_proxy = 0
    chg_24h = (closes_1h[-1] - closes_1h[0]) / closes_1h[0] * 100 if closes_1h[0] else 0
    if direction == "LONG" and chg_24h > 3: sm_proxy += 40
    elif direction == "LONG" and chg_24h > 1: sm_proxy += 25
    elif direction == "SHORT" and chg_24h < -3: sm_proxy += 40
    elif direction == "SHORT" and chg_24h < -1: sm_proxy += 25
    if vol_ratio > 1.5: sm_proxy += 30
    elif vol_ratio > 1.2: sm_proxy += 15
    sm_proxy = min(100, sm_proxy)

    # Final score (0-400 scale)
    final = round(sm_proxy + ms_score + tech_score + fund_score)

    # v5: hourly trend scoring
    hourly_trend = classify_hourly_trend_bt(candles_1h)
    if direction == "LONG" and hourly_trend == "UP": tech_score += 20
    elif direction == "SHORT" and hourly_trend == "DOWN": tech_score += 20
    elif direction == "LONG" and hourly_trend == "DOWN": tech_score -= 30
    elif direction == "SHORT" and hourly_trend == "UP": tech_score -= 30
    tech_score = max(0, min(100, tech_score))

    # Recalculate final with updated tech
    final = round(sm_proxy + ms_score + tech_score + fund_score)

    details = {
        "trend": trend, "trendStr": trend_str,
        "rsi1h": rsi_1h, "rsi4h": rsi_4h,
        "volRatio": vol_ratio, "atrPct": atr_pct,
        "patterns": patterns, "chg4h": round(chg_4h, 2),
        "funding": round(ann_rate, 1),
        "hourlyTrend": hourly_trend,
        "pillars": {"sm_proxy": sm_proxy, "mktStructure": ms_score,
                    "technicals": tech_score, "funding": fund_score}
    }
    return final, direction, details


# ─── Backtest simulation ───
class Position:
    def __init__(self, asset, direction, entry_price, size_usd, leverage, bar_idx):
        self.asset = asset
        self.direction = direction
        self.entry = entry_price
        self.size_usd = size_usd
        self.leverage = leverage
        self.notional = size_usd * leverage
        self.opened_bar = bar_idx
        self.hw = entry_price
        self.stop = entry_price * (1 - 0.03) if direction == "LONG" else entry_price * (1 + 0.03)
        self.breach_count = 0

    def update(self, price):
        if self.direction == "LONG":
            self.hw = max(self.hw, price)
            # trailing stop
            trailing = self.hw * (1 - 0.03)
            self.stop = max(self.stop, trailing)
            hit_stop = price <= self.stop
        else:
            self.hw = min(self.hw, price)
            trailing = self.hw * (1 + 0.03)
            self.stop = min(self.stop, trailing)
            hit_stop = price >= self.stop

        if hit_stop:
            self.breach_count += 1
        else:
            self.breach_count = 0
        return self.breach_count >= 2

    def pnl(self, exit_price):
        if self.direction == "LONG":
            pct = (exit_price - self.entry) / self.entry
        else:
            pct = (self.entry - exit_price) / self.entry
        gross = pct * self.notional
        fees = self.notional * ROUND_TRIP_FEE
        return gross - fees

    def pnl_pct(self, exit_price):
        return self.pnl(exit_price) / self.size_usd * 100


def run_backtest(config, asset_data, label):
    """Run one config against all asset data. Returns results dict."""
    cfg = config
    budget = 1230.0
    balance = budget
    positions = []  # active
    closed_trades = []
    asset_cooldowns = {}  # asset -> bar when cooldown expires

    # Flatten all scoring events across assets into timeline
    # asset_data[asset] = {"candles_4h": [...], "candles_1h": [...], "meta": {...}}

    # We'll step through time in 4h bars
    # Find common time range
    all_assets = list(asset_data.keys())
    if not all_assets:
        return {"error": "no assets"}

    ref = asset_data[all_assets[0]]
    n_bars = len(ref["candles_4h"])

    # Need enough history for indicators
    start_bar = 20  # skip first 20 bars for indicator warmup

    for bar in range(start_bar, n_bars):
        current_time = ref["candles_4h"][bar]["t"] if "t" in ref["candles_4h"][bar] else bar

        # 1. Update existing positions
        closed_this_bar = []
        for pos in positions[:]:
            asset = pos.asset
            if asset not in asset_data:
                continue
            candles = asset_data[asset]["candles_4h"]
            if bar >= len(candles):
                continue
            price = float(candles[bar]["c"])
            should_close = pos.update(price)

            # Check min hold time
            held_bars = bar - pos.opened_bar
            if held_bars < cfg["minHoldBars"]:
                continue

            if should_close:
                pnl = pos.pnl(price)
                balance += pos.size_usd + pnl
                closed_trades.append({
                    "asset": asset, "direction": pos.direction,
                    "entry": pos.entry, "exit": price,
                    "pnl": round(pnl, 2), "pnl_pct": round(pos.pnl_pct(price), 1),
                    "held_bars": held_bars, "reason": "DSL_stop",
                    "bar": bar
                })
                positions.remove(pos)
                asset_cooldowns[asset] = bar + cfg["assetCooldownBars"]
                closed_this_bar.append(asset)

        # 2. Score all assets for new entries
        if len(positions) >= cfg["maxPositions"]:
            continue

        candidates = []
        for asset in all_assets:
            if asset in [p.asset for p in positions]:
                continue
            if asset in asset_cooldowns and bar < asset_cooldowns[asset]:
                continue

            ad = asset_data[asset]
            if bar >= len(ad["candles_4h"]) or bar >= len(ad["candles_1h"]):
                continue

            c4h = ad["candles_4h"][:bar+1]
            c1h_end = min(bar * 6 + 6, len(ad["candles_1h"]))  # approximate 1h mapping
            c1h = ad["candles_1h"][:c1h_end]

            funding = ad["meta"].get("funding", 0)
            vol = ad["meta"].get("vol24h", 0)
            oi = ad["meta"].get("oi", 0)

            score, direction, details = score_at_bar(c4h, c1h, funding, vol, oi)
            if score < cfg["minScore"] or direction is None:
                continue

            # Apply filters
            if cfg["requireTrendAlign"]:
                trend = details.get("trend", "neutral")
                if direction == "LONG" and trend not in ("strong_up", "up"):
                    continue
                if direction == "SHORT" and trend not in ("strong_down", "down"):
                    continue

            # v5: Hourly trend hard gate
            if cfg.get("requireHourlyTrend", False):
                ht = details.get("hourlyTrend", "NEUTRAL")
                if direction == "LONG" and ht == "DOWN":
                    continue
                if direction == "SHORT" and ht == "UP":
                    continue

            if cfg["disqualifyRSIExtreme"]:
                rsi = details.get("rsi1h", 50)
                if direction == "LONG" and rsi > 70:
                    continue
                if direction == "SHORT" and rsi < 30:
                    continue

            if cfg["disqualifyVolumeDeclining"]:
                vr = details.get("volRatio", 1.0)
                if vr < 0.7:
                    continue

            candidates.append((asset, score, direction, details, float(ad["candles_4h"][bar]["c"])))

        # Sort by score, take best
        candidates.sort(key=lambda x: x[1], reverse=True)
        slots = cfg["maxPositions"] - len(positions)

        for asset, score, direction, details, price in candidates[:slots]:
            size = balance * cfg["positionSizePct"]
            if size < 50:
                continue
            balance -= size
            pos = Position(asset, direction, price, size, cfg["leverage"], bar)
            positions.append(pos)

    # Close remaining positions at final price
    for pos in positions:
        candles = asset_data[pos.asset]["candles_4h"]
        price = float(candles[-1]["c"])
        pnl = pos.pnl(price)
        balance += pos.size_usd + pnl
        closed_trades.append({
            "asset": pos.asset, "direction": pos.direction,
            "entry": pos.entry, "exit": price,
            "pnl": round(pnl, 2), "pnl_pct": round(pos.pnl_pct(price), 1),
            "held_bars": n_bars - pos.opened_bar, "reason": "end_of_test",
            "bar": n_bars
        })

    # Stats
    wins = [t for t in closed_trades if t["pnl"] > 0]
    losses = [t for t in closed_trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in closed_trades)
    avg_hold = sum(t["held_bars"] for t in closed_trades) / len(closed_trades) if closed_trades else 0
    max_dd = 0
    peak_bal = budget
    running = budget
    for t in closed_trades:
        running += t["pnl"]
        peak_bal = max(peak_bal, running)
        dd = (peak_bal - running) / peak_bal * 100
        max_dd = max(max_dd, dd)

    fee_total = sum(
        Position(t["asset"], t["direction"], t["entry"], abs(t.get("pnl", 0)) + 100, cfg["leverage"], 0).notional * ROUND_TRIP_FEE
        for t in closed_trades
    ) if False else len(closed_trades) * budget * cfg["positionSizePct"] * cfg["leverage"] * ROUND_TRIP_FEE

    return {
        "config": label,
        "totalTrades": len(closed_trades),
        "wins": len(wins),
        "losses": len(losses),
        "winRate": round(len(wins) / len(closed_trades) * 100, 1) if closed_trades else 0,
        "totalPnL": round(total_pnl, 2),
        "totalPnLPct": round(total_pnl / budget * 100, 1),
        "avgPnLPerTrade": round(total_pnl / len(closed_trades), 2) if closed_trades else 0,
        "avgWin": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
        "avgLoss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
        "avgHoldBars": round(avg_hold, 1),
        "avgHoldHours": round(avg_hold * 4, 1),
        "maxDrawdownPct": round(max_dd, 1),
        "finalBalance": round(balance, 2),
        "estFeeDrag": round(fee_total, 2),
        "profitFactor": round(sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)), 2) if losses and sum(t["pnl"] for t in losses) != 0 else float('inf'),
        "trades": closed_trades
    }


# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════
print(f"Backtest: {LOOKBACK_DAYS} days, top {TOP_N_ASSETS} assets, fee={args.fee_pct}%", file=sys.stderr)

# Stage 1: Get top assets by volume
print("Stage 1: Fetching market structure...", file=sys.stderr)
meta_raw = fetch_json({"type": "metaAndAssetCtxs"})
meta_info = meta_raw[0]["universe"]
meta_ctx = meta_raw[1]

asset_meta = {}
for info, ctx in zip(meta_info, meta_ctx):
    name = info["name"]
    try:
        vol = float(ctx.get("dayNtlVlm", 0))
        funding = float(ctx.get("funding", 0))
        oi = float(ctx.get("openInterest", 0))
    except (ValueError, TypeError):
        continue
    asset_meta[name] = {"vol24h": vol, "funding": funding, "oi": oi}

top_assets = sorted(asset_meta.items(), key=lambda x: x[1]["vol24h"], reverse=True)[:TOP_N_ASSETS]
top_names = [a[0] for a in top_assets]
print(f"Stage 1: {len(top_names)} assets: {top_names}", file=sys.stderr)

# Stage 2: Fetch historical candles (parallel)
print(f"Stage 2: Fetching {LOOKBACK_DAYS}d candles for {len(top_names)} assets...", file=sys.stderr)
now_ms = int(time.time() * 1000)
start_ms = now_ms - (LOOKBACK_DAYS * 24 * 3600 * 1000)

asset_data = {}

def fetch_asset(name):
    c4h = fetch_candles(name, "4h", start_ms, now_ms)
    c1h = fetch_candles(name, "1h", start_ms, now_ms)
    return name, c4h, c1h

with ThreadPoolExecutor(max_workers=8) as executor:
    futures = {executor.submit(fetch_asset, name): name for name in top_names}
    for future in as_completed(futures):
        name = futures[future]
        try:
            n, c4h, c1h = future.result()
            if c4h and c1h:
                asset_data[n] = {
                    "candles_4h": c4h,
                    "candles_1h": c1h,
                    "meta": asset_meta[n]
                }
                print(f"  {n}: 4h={len(c4h)} 1h={len(c1h)}", file=sys.stderr)
        except Exception as e:
            print(f"  {name}: failed ({e})", file=sys.stderr)

print(f"Stage 2: {len(asset_data)} assets with data", file=sys.stderr)

# Stage 3: Run backtests
print("Stage 3: Running backtests...", file=sys.stderr)
results = {}
for config_name, config in CONFIGS.items():
    print(f"  Running {config_name}...", file=sys.stderr)
    r = run_backtest(config, asset_data, config["label"])
    results[config_name] = r
    print(f"    {r['totalTrades']} trades, PnL=${r['totalPnL']}, WR={r['winRate']}%, DD={r['maxDrawdownPct']}%", file=sys.stderr)

# Output
if args.output == "json":
    # Remove individual trades for cleaner output
    clean = {}
    for k, v in results.items():
        clean[k] = {kk: vv for kk, vv in v.items() if kk != "trades"}
        clean[k]["sampleTrades"] = v["trades"][:5] if v.get("trades") else []
    print(json.dumps(clean, indent=2))
else:
    for name, r in results.items():
        print(f"\n{'='*60}")
        print(f"  {r['config']}")
        print(f"{'='*60}")
        print(f"  Trades: {r['totalTrades']} | Wins: {r['wins']} | Losses: {r['losses']}")
        print(f"  Win Rate: {r['winRate']}%")
        print(f"  Total PnL: ${r['totalPnL']} ({r['totalPnLPct']}%)")
        print(f"  Avg PnL/trade: ${r['avgPnLPerTrade']}")
        print(f"  Avg Win: ${r['avgWin']} | Avg Loss: ${r['avgLoss']}")
        print(f"  Profit Factor: {r['profitFactor']}")
        print(f"  Avg Hold: {r['avgHoldHours']}h")
        print(f"  Max Drawdown: {r['maxDrawdownPct']}%")
        print(f"  Est Fee Drag: ${r['estFeeDrag']}")
        print(f"  Final Balance: ${r['finalBalance']}")

print(f"\nDone.", file=sys.stderr)
