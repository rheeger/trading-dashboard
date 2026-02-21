#!/usr/bin/env python3
"""
Dynamic Grid Trading — Setup
Creates initial grid state file with calculated levels.
Agent then places the limit orders via Senpi API.
"""

import json, sys, subprocess, os
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(SCRIPT_DIR)
CONFIG_FILE = os.path.join(WORKSPACE, "grid-config.json")
STATE_FILE = os.environ.get("GRID_STATE_FILE", os.path.join(WORKSPACE, "grid-state.json"))

with open(CONFIG_FILE) as f:
    cfg = json.load(f)

asset = cfg["asset"]
grid_count = cfg.get("gridCount", 10)
range_pct = cfg.get("rangePct", 4.0)
leverage = cfg.get("maxLeverage", 3)
allocation = cfg.get("allocationPct", 30)
budget = cfg.get("budget", 1000)

def fetch_json(payload):
    r = subprocess.run(
        ["curl", "-s", "https://api.hyperliquid.xyz/info",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=15
    )
    return json.loads(r.stdout)

mids = fetch_json({"type": "allMids"})
center = float(mids[asset])

half_range = center * range_pct / 100 / 2
range_high = round(center + half_range, 2)
range_low = round(center - half_range, 2)
grid_spacing = round((range_high - range_low) / grid_count, 2)

total_allocation = budget * allocation / 100
order_size_usd = round(total_allocation / grid_count, 2)

levels = []
for i in range(grid_count):
    level_price = round(range_low + grid_spacing * (i + 0.5), 2)
    side = "BUY" if level_price < center else "SELL"
    size = round(order_size_usd * leverage / level_price, 6)
    levels.append({
        "price": level_price,
        "side": side,
        "size": size,
        "status": "pending"
    })

state = {
    "active": True,
    "asset": asset,
    "wallet": cfg.get("wallet", ""),
    "strategyId": cfg.get("strategyId", ""),
    "center": center,
    "rangeHigh": range_high,
    "rangeLow": range_low,
    "gridCount": grid_count,
    "gridSpacing": grid_spacing,
    "leverage": leverage,
    "orderSizeUsd": order_size_usd,
    "activeOrders": levels,
    "fills": [],
    "totalGridPnl": 0,
    "totalFills": 0,
    "startedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "lastCheck": None,
    "rebalanceCount": 0,
    "fundingAccrued": 0,
    "hardStopPct": cfg.get("hardStopPct", 5.0),
    "maxGridPnlLoss": cfg.get("maxGridPnlLoss", -100),
    "rebalanceThreshold": cfg.get("rebalanceThreshold", 0.5)
}

with open(STATE_FILE, "w") as f:
    json.dump(state, f, indent=2)

print(json.dumps({
    "status": "initialized",
    "asset": asset,
    "center": center,
    "range": f"{range_low} - {range_high}",
    "gridCount": grid_count,
    "gridSpacing": grid_spacing,
    "orderSizeUsd": order_size_usd,
    "totalAllocation": total_allocation,
    "levels": levels
}, indent=2))

print(f"\nGrid initialized. {len(levels)} orders to place.", file=sys.stderr)
print("Agent should now place each order via create_position with orderType: LIMIT", file=sys.stderr)
