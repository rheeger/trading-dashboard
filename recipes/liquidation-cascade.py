#!/usr/bin/env python3
"""
Liquidation Cascade Detector
Scores cascade probability for top Hyperliquid perps by OI + funding + book depth.
Zero LLM tokens. All computation in Python.
"""

import json, sys, subprocess, os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(SCRIPT_DIR)
CONFIG_FILE = os.path.join(WORKSPACE, "cascade-config.json")

cfg = {}
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)

TOP_N = cfg.get("topNAssets", 20)
MIN_OI = cfg.get("minOI", 5_000_000)
MIN_FUNDING_ANN = cfg.get("minFundingAnnualized", 15)
CASCADE_DIST = cfg.get("cascadeDistanceThreshold", 0.05)
THIN_BOOK = cfg.get("thinBookThreshold", 500_000)
MIN_SCORE = cfg.get("minScore", 40)
CHECK_L2 = cfg.get("checkL2Book", True)
MAX_WORKERS = cfg.get("maxWorkers", 8)

def fetch_json(payload):
    r = subprocess.run(
        ["curl", "-s", "https://api.hyperliquid.xyz/info",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=30
    )
    return json.loads(r.stdout)

def estimate_cascade_zone(mark_price, funding_rate, direction):
    """Estimate where liquidation cascade would trigger based on funding magnitude."""
    ann_rate = abs(funding_rate * 24 * 365 * 100)
    if ann_rate > 100:
        avg_lev = 15
    elif ann_rate > 50:
        avg_lev = 10
    elif ann_rate > 25:
        avg_lev = 7
    else:
        avg_lev = 5

    maint_margin = 1 / avg_lev * 0.5

    if direction == "LONG":
        cascade_price = mark_price * (1 - maint_margin)
    else:
        cascade_price = mark_price * (1 + maint_margin)

    return round(cascade_price, 2), avg_lev

def fetch_book_depth(asset, price_level, side):
    """Fetch L2 book and compute depth at/near a price level."""
    try:
        book = fetch_json({"type": "l2Book", "coin": asset, "nSigFigs": 4})
        levels = book.get("levels", [[], []])
        bids = levels[0]
        asks = levels[1]

        depth = 0
        target_levels = bids if side == "LONG" else asks

        for level in target_levels:
            px = float(level["px"])
            sz = float(level["sz"])
            ntl = px * sz

            if side == "LONG" and px >= price_level:
                depth += ntl
            elif side == "SHORT" and px <= price_level:
                depth += ntl

        return round(depth, 2)
    except Exception:
        return None

# ─── Stage 1: Fetch market data ───
print("Stage 1: Fetching market structure...", file=sys.stderr)
meta_raw = fetch_json({"type": "metaAndAssetCtxs"})
meta_info = meta_raw[0]["universe"]
meta_ctx = meta_raw[1]

candidates = []
for info, ctx in zip(meta_info, meta_ctx):
    name = info["name"]
    try:
        funding = float(ctx.get("funding", 0))
        oi = float(ctx.get("openInterest", 0))
        mark = float(ctx.get("markPx", 0))
        vol24h = float(ctx.get("dayNtlVlm", 0))
    except (ValueError, TypeError):
        continue

    ann_rate = abs(funding * 24 * 365 * 100)
    if oi < MIN_OI or ann_rate < MIN_FUNDING_ANN:
        continue

    crowded_side = "LONG" if funding > 0 else "SHORT"
    trade_direction = "SHORT" if crowded_side == "LONG" else "LONG"

    cascade_price, est_leverage = estimate_cascade_zone(mark, funding, crowded_side)
    distance_pct = abs(mark - cascade_price) / mark * 100

    if distance_pct > CASCADE_DIST * 100:
        continue

    # Determine price trajectory from recent candles
    try:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        candles_1h = fetch_json({
            "type": "candleSnapshot",
            "req": {"coin": name, "interval": "1h",
                    "startTime": now_ms - (4 * 3600 * 1000), "endTime": now_ms}
        })
        if candles_1h:
            first_close = float(candles_1h[0]["c"])
            last_close = float(candles_1h[-1]["c"])
            if crowded_side == "LONG" and last_close < first_close:
                trajectory = "approaching"
            elif crowded_side == "SHORT" and last_close > first_close:
                trajectory = "approaching"
            else:
                trajectory = "retreating"
        else:
            trajectory = "unknown"
    except Exception:
        trajectory = "unknown"

    candidates.append({
        "asset": name, "mark": mark, "oi": oi, "funding": funding,
        "annRate": ann_rate, "crowdedSide": crowded_side,
        "tradeDirection": trade_direction, "cascadePrice": cascade_price,
        "distancePct": distance_pct, "vol24h": vol24h,
        "estLeverage": est_leverage, "trajectory": trajectory
    })

candidates.sort(key=lambda x: x["oi"], reverse=True)
candidates = candidates[:TOP_N]
print(f"Stage 1: {len(candidates)} candidates with extreme funding + high OI", file=sys.stderr)

# ─── Stage 2: Check book depth (optional, parallel) ───
book_depths = {}
if CHECK_L2 and candidates:
    print("Stage 2: Fetching L2 book depth...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_book_depth, c["asset"], c["cascadePrice"], c["crowdedSide"]): c["asset"]
            for c in candidates
        }
        for future in as_completed(futures):
            asset = futures[future]
            try:
                depth = future.result()
                book_depths[asset] = depth
            except Exception:
                pass

# ─── Stage 3: Score ───
alerts = []
for c in candidates:
    score = 0

    if c["oi"] > 50_000_000: score += 25
    elif c["oi"] > 10_000_000: score += 15
    elif c["oi"] > 5_000_000: score += 10

    if c["annRate"] > 50: score += 25
    elif c["annRate"] > 30: score += 15
    elif c["annRate"] > 15: score += 10

    if c["distancePct"] < 2: score += 20
    elif c["distancePct"] < 5: score += 10

    depth = book_depths.get(c["asset"])
    if depth is not None:
        if depth < THIN_BOOK: score += 15
        elif depth < THIN_BOOK * 4: score += 8

    if c["trajectory"] == "approaching": score += 10

    if c["vol24h"] > 0:
        oi_vol_ratio = c["oi"] / c["vol24h"]
        if oi_vol_ratio > 2: score += 5

    if score < MIN_SCORE:
        continue

    if score >= 70: magnitude = "high"
    elif score >= 50: magnitude = "medium"
    else: magnitude = "low"

    risks = []
    if c["distancePct"] > 3:
        risks.append(f"cascade zone distant ({c['distancePct']:.1f}%)")
    if c["trajectory"] == "retreating":
        risks.append("price moving away from cascade zone")
    if depth and depth > THIN_BOOK * 4:
        risks.append("thick book may absorb cascade")

    alerts.append({
        "asset": c["asset"],
        "score": min(100, score),
        "crowdedSide": c["crowdedSide"],
        "tradeDirection": c["tradeDirection"],
        "markPrice": c["mark"],
        "estimatedCascadeZone": c["cascadePrice"],
        "distanceToCascade": round(c["distancePct"], 2),
        "estimatedAvgLeverage": c["estLeverage"],
        "openInterest": round(c["oi"]),
        "fundingRate": round(c["funding"] * 100, 6),
        "annualizedFunding": round(c["annRate"], 1),
        "bookDepthAtCascade": depth,
        "priceTrajectory": c["trajectory"],
        "cascadeMagnitudeEstimate": magnitude,
        "risks": risks
    })

alerts.sort(key=lambda x: x["score"], reverse=True)

output = {
    "scanTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "assetsScanned": len(meta_info),
    "qualifyingAssets": len(candidates),
    "cascadeAlerts": alerts[:10]
}

print(json.dumps(output, indent=2))
print(f"\nDone. {len(alerts)} cascade alerts.", file=sys.stderr)
