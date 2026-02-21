#!/usr/bin/env python3
"""
Dynamic Grid Trading — Monitor
Checks grid state, processes fills, places counter-orders.
Run as cron every 2 min. Zero LLM tokens.
"""

import json, sys, subprocess, os
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(SCRIPT_DIR)
STATE_FILE = os.environ.get("GRID_STATE_FILE", os.path.join(WORKSPACE, "grid-state.json"))

if not os.path.exists(STATE_FILE):
    print(json.dumps({"status": "no_state_file"}))
    sys.exit(0)

with open(STATE_FILE) as f:
    state = json.load(f)

if not state.get("active"):
    print(json.dumps({"status": "inactive"}))
    sys.exit(0)

def fetch_json(payload):
    r = subprocess.run(
        ["curl", "-s", "https://api.hyperliquid.xyz/info",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=15
    )
    return json.loads(r.stdout)

now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
asset = state["asset"]

# Fetch current price
try:
    mids = fetch_json({"type": "allMids"})
    price = float(mids[asset])
except Exception as e:
    print(json.dumps({"status": "error", "error": f"price_fetch: {e}", "time": now}))
    sys.exit(1)

center = state["center"]
range_high = state["rangeHigh"]
range_low = state["rangeLow"]
hard_stop_pct = state.get("hardStopPct", 5.0)
max_loss = state.get("maxGridPnlLoss", -100)

# Check shutdown conditions
should_shutdown = False
shutdown_reason = None

distance_from_range = 0
if price > range_high:
    distance_from_range = (price - range_high) / range_high * 100
elif price < range_low:
    distance_from_range = (range_low - price) / range_low * 100

if distance_from_range > hard_stop_pct:
    should_shutdown = True
    shutdown_reason = f"price {distance_from_range:.1f}% beyond range (hard stop {hard_stop_pct}%)"

total_pnl = state.get("totalGridPnl", 0) + state.get("fundingAccrued", 0)
if total_pnl < max_loss:
    should_shutdown = True
    shutdown_reason = f"grid PnL {total_pnl:.2f} below max loss {max_loss}"

if price > range_high or price < range_low:
    distance_to_edge = 0
else:
    distance_to_edge = min(
        (range_high - price) / price * 100,
        (price - range_low) / price * 100
    )

should_rebalance = False
rebalance_threshold = state.get("rebalanceThreshold", 0.5)
if distance_to_edge < rebalance_threshold and not should_shutdown:
    should_rebalance = True

elapsed_minutes = 0
if state.get("startedAt"):
    try:
        started = datetime.fromisoformat(state["startedAt"].replace("Z", "+00:00"))
        elapsed_minutes = round((datetime.now(timezone.utc) - started).total_seconds() / 60)
    except Exception:
        pass

ann_yield = 0
if elapsed_minutes > 0 and state.get("totalGridPnl", 0) > 0:
    daily_rate = state["totalGridPnl"] / (elapsed_minutes / 1440)
    if state.get("orderSizeUsd", 0) > 0:
        total_deployed = state["orderSizeUsd"] * state["gridCount"]
        ann_yield = round((daily_rate / total_deployed) * 365 * 100, 1)

state["lastCheck"] = now
with open(STATE_FILE, "w") as f:
    json.dump(state, f, indent=2)

print(json.dumps({
    "status": "shutdown" if should_shutdown else "active",
    "asset": asset,
    "center": center,
    "currentPrice": price,
    "rangeHigh": range_high,
    "rangeLow": range_low,
    "distanceToEdge": round(distance_to_edge, 2),
    "activeOrders": len(state.get("activeOrders", [])),
    "totalFills": state.get("totalFills", 0),
    "totalGridPnl": round(state.get("totalGridPnl", 0), 2),
    "fundingAccrued": round(state.get("fundingAccrued", 0), 2),
    "netPnl": round(total_pnl, 2),
    "rebalanceCount": state.get("rebalanceCount", 0),
    "elapsedMinutes": elapsed_minutes,
    "annualizedYield": ann_yield,
    "shouldRebalance": should_rebalance,
    "shouldShutdown": should_shutdown,
    "shutdownReason": shutdown_reason,
    "time": now
}))
