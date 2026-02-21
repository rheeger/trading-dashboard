#!/usr/bin/env python3
"""
Funding Rate Harvester — scans for extreme funding rates
and outputs ranked delta-neutral harvest opportunities.
Zero LLM tokens. All computation in Python.
"""

import json, sys, subprocess, os
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(SCRIPT_DIR)
CONFIG_FILE = os.path.join(WORKSPACE, "funding-config.json")
STATE_FILE = os.path.join(WORKSPACE, "funding-state.json")

cfg = {}
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)

MIN_RATE = cfg.get("minAnnualizedRate", 25)
EXIT_RATE = cfg.get("exitAnnualizedRate", 5)
MIN_VOL = cfg.get("minVolume24h", 5_000_000)
SPOT_ASSETS = set(cfg.get("spotAssets", ["BTC", "ETH", "SOL", "HYPE"]))

def fetch_json(payload):
    r = subprocess.run(
        ["curl", "-s", "https://api.hyperliquid.xyz/info",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=30
    )
    return json.loads(r.stdout)

# ─── Fetch market data ───
print("Fetching market data...", file=sys.stderr)
meta_raw = fetch_json({"type": "metaAndAssetCtxs"})
meta_info = meta_raw[0]["universe"]
meta_ctx = meta_raw[1]

# ─── Fetch spot mids for spread calculation ───
try:
    all_mids = fetch_json({"type": "allMids"})
except Exception:
    all_mids = {}

# ─── Load state ───
state = {"activeHedges": [], "totalYield": 0, "totalHedgesOpened": 0, "totalHedgesClosed": 0}
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        pass

# ─── Scan for opportunities ───
opportunities = []
for info, ctx in zip(meta_info, meta_ctx):
    name = info["name"]
    try:
        funding = float(ctx.get("funding", 0))
        vol24h = float(ctx.get("dayNtlVlm", 0))
        mark = float(ctx.get("markPx", 0))
    except (ValueError, TypeError):
        continue

    if vol24h < MIN_VOL:
        continue

    ann_rate = abs(funding * 24 * 365 * 100)
    if ann_rate < MIN_RATE:
        continue

    direction = "short_perp" if funding > 0 else "long_perp"
    has_spot = name in SPOT_ASSETS

    spread = None
    if has_spot and name in all_mids:
        spot_mid = float(all_mids.get(name, mark))
        spread = round(abs(mark - spot_mid) / mark * 100, 4)

    score = 0
    if ann_rate > 100: score += 40
    elif ann_rate > 50: score += 30
    elif ann_rate > 25: score += 20

    if vol24h > 50_000_000: score += 15
    elif vol24h > 10_000_000: score += 10

    if has_spot: score += 10
    if spread is not None and spread < 0.05: score += 15
    elif spread is not None and spread < 0.1: score += 10

    estimated_daily = round(ann_rate / 365, 2)

    risks = []
    if not has_spot:
        risks.append("no spot market — requires cross-exchange hedge")
    if spread and spread > 0.1:
        risks.append(f"wide spread ({spread}%)")

    opportunities.append({
        "asset": name,
        "fundingRate": round(funding * 100, 6),
        "annualized": round(ann_rate, 1),
        "direction": direction,
        "score": min(100, score),
        "hasSpotMarket": has_spot,
        "volume24h": round(vol24h),
        "spread": spread,
        "rateStableHours": None,
        "estimatedDailyYield": estimated_daily,
        "risks": risks
    })

opportunities.sort(key=lambda x: x["score"], reverse=True)

# ─── Check active hedges ───
for hedge in state.get("activeHedges", []):
    asset = hedge["asset"]
    for info, ctx in zip(meta_info, meta_ctx):
        if info["name"] == asset:
            current_rate = float(ctx.get("funding", 0))
            current_ann = abs(current_rate * 24 * 365 * 100)
            hedge["currentRate"] = round(current_rate * 100, 6)
            hedge["currentAnnualized"] = round(current_ann, 1)
            hedge["shouldClose"] = current_ann < EXIT_RATE
            break

output = {
    "scanTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "assetsScanned": len(meta_info),
    "opportunities": opportunities[:10],
    "activeHedges": state.get("activeHedges", []),
    "totalYield": state.get("totalYield", 0)
}

print(json.dumps(output, indent=2))
print(f"\nDone. {len(opportunities)} opportunities found.", file=sys.stderr)
