#!/usr/bin/env python3
"""DSL v3 — Enhanced 2-phase with configurable tier ratcheting.
Supports LONG and SHORT. Auto-closes positions on breach via mcporter.
v3 adds: error handling, retry logic, per-tier retrace, breach decay,
velocity tracking, and enriched output. All new features are configurable
and backward-compatible with v2 state files."""
import json, sys, subprocess, os
from datetime import datetime, timezone

STATE_FILE = os.environ.get("DSL_STATE_FILE", "/data/workspace/trailing-stop-state.json")

with open(STATE_FILE) as f:
    state = json.load(f)

if not state.get("active"):
    if state.get("pendingClose"):
        pass  # continue to retry close
    else:
        print(json.dumps({"status": "inactive"}))
        sys.exit(0)

direction = state.get("direction", "LONG").upper()
is_long = direction == "LONG"
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ─── Configurable behavior (v3, with v2 defaults) ───
breach_decay_mode = state.get("breachDecay", "hard")
close_retries = state.get("closeRetries", 2)
close_retry_delay = state.get("closeRetryDelaySec", 3)
verify_close = state.get("verifyClose", False)

# ─── Fetch price (with error handling) ───
try:
    r = subprocess.run(
        ["curl", "-s", "https://api.hyperliquid.xyz/info",
         "-H", "Content-Type: application/json",
         "-d", '{"type":"allMids"}'],
        capture_output=True, text=True, timeout=15
    )
    mids = json.loads(r.stdout)
    price = float(mids[state["asset"]])
except Exception as e:
    state["consecutiveFetchFailures"] = state.get("consecutiveFetchFailures", 0) + 1
    state["lastCheck"] = now
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(json.dumps({
        "status": "error",
        "error": f"price_fetch_failed: {str(e)}",
        "asset": state.get("asset"),
        "consecutive_failures": state["consecutiveFetchFailures"],
        "pending_close": state.get("pendingClose", False),
        "time": now
    }))
    sys.exit(1)

state["consecutiveFetchFailures"] = 0

entry = state["entryPrice"]
size = state["size"]
hw = state["highWaterPrice"]
phase = state["phase"]
breach_count = state["currentBreachCount"]
tier_idx = state["currentTierIndex"]
tier_floor = state["tierFloorPrice"]
tiers = state["tiers"]

# ─── If pending close from a previous failed attempt, force close logic ───
force_close = state.get("pendingClose", False)

# ─── Velocity tracking ───
velocity = 0
velocity_toward_floor = 0
if state.get("lastPrice") and state.get("lastCheck"):
    try:
        last_time = datetime.fromisoformat(state["lastCheck"].replace("Z", "+00:00"))
        elapsed_sec = (datetime.now(timezone.utc) - last_time).total_seconds()
        elapsed_min = elapsed_sec / 60
        if elapsed_min > 0:
            price_chg_pct = (price - state["lastPrice"]) / state["lastPrice"] * 100
            velocity = round(price_chg_pct / elapsed_min, 4)
    except (ValueError, TypeError):
        pass

# ─── uPnL (leveraged return on margin) — direction-aware ───
if is_long:
    upnl = (price - entry) * size
else:
    upnl = (entry - price) * size
margin = entry * size / state["leverage"]
upnl_pct = upnl / margin * 100

# ─── Update high water — direction-aware ───
if is_long and price > hw:
    hw = price
    state["highWaterPrice"] = hw
elif not is_long and price < hw:
    hw = price
    state["highWaterPrice"] = hw

# ─── Check tier upgrades ───
previous_tier_idx = tier_idx
tier_changed = False

for i, tier in enumerate(tiers):
    if i <= tier_idx:
        continue
    if upnl_pct >= tier["triggerPct"]:
        tier_idx = i
        tier_changed = True
        if is_long:
            tier_floor = round(entry * (1 + tier["lockPct"] / 100 / state["leverage"]), 4)
        else:
            tier_floor = round(entry * (1 - tier["lockPct"] / 100 / state["leverage"]), 4)
        state["currentTierIndex"] = tier_idx
        state["tierFloorPrice"] = tier_floor
        if phase == 1:
            phase = 2
            state["phase"] = 2
            breach_count = 0
            state["currentBreachCount"] = 0

# ─── Determine effective floor — direction-aware ───
if phase == 1:
    retrace = state["phase1"]["retraceThreshold"]
    breaches_needed = state["phase1"]["consecutiveBreachesRequired"]
    abs_floor = state["phase1"]["absoluteFloor"]
    if is_long:
        trailing_floor = round(hw * (1 - retrace), 4)
        effective_floor = max(abs_floor, trailing_floor)
    else:
        trailing_floor = round(hw * (1 + retrace), 4)
        effective_floor = min(abs_floor, trailing_floor)
else:
    # v3: per-tier retrace override — falls back to global phase2 value
    if tier_idx >= 0:
        retrace = tiers[tier_idx].get("retrace", state["phase2"]["retraceThreshold"])
    else:
        retrace = state["phase2"]["retraceThreshold"]
    breaches_needed = state["phase2"]["consecutiveBreachesRequired"]
    if is_long:
        trailing_floor = round(hw * (1 - retrace), 4)
        effective_floor = max(tier_floor or 0, trailing_floor)
    else:
        trailing_floor = round(hw * (1 + retrace), 4)
        effective_floor = min(tier_floor or float('inf'), trailing_floor)

state["floorPrice"] = round(effective_floor, 4)

# ─── Compute velocity toward floor ───
if effective_floor and velocity != 0:
    if is_long:
        velocity_toward_floor = max(0, -velocity)
    else:
        velocity_toward_floor = max(0, velocity)

# ─── Check breach — direction-aware ───
if is_long:
    breached = price <= effective_floor
else:
    breached = price >= effective_floor

# v3: configurable breach decay
if breached:
    breach_count += 1
else:
    if breach_decay_mode == "soft":
        breach_count = max(0, breach_count - 1)
    else:
        breach_count = 0
state["currentBreachCount"] = breach_count

should_close = breach_count >= breaches_needed or force_close

# ─── Auto-close on breach (with retry logic) ───
closed = False
close_result = None
close_verified = None

if should_close:
    wallet = state.get("wallet", "")
    asset = state["asset"]
    if wallet:
        for attempt in range(close_retries):
            try:
                cr = subprocess.run(
                    ["mcporter", "call", "senpi", "close_position", "--args",
                     json.dumps({
                         "strategyWalletAddress": wallet,
                         "coin": asset,
                         "reason": f"DSL breach: Phase {phase}, {breach_count}/{breaches_needed} breaches"
                     })],
                    capture_output=True, text=True, timeout=30
                )
                close_result = cr.stdout.strip()
                if cr.returncode == 0 and "error" not in close_result.lower():
                    closed = True
                    state["active"] = False
                    state["pendingClose"] = False
                    state["closedAt"] = now
                    state["closeReason"] = f"DSL breach: Phase {phase}, price {price}, floor {effective_floor}"
                    break
                else:
                    close_result = f"api_error_attempt_{attempt+1}: {close_result}"
            except Exception as e:
                close_result = f"error_attempt_{attempt+1}: {str(e)}"
            if attempt < close_retries - 1:
                import time; time.sleep(close_retry_delay)

        if not closed:
            state["pendingClose"] = True

        # v3: optional close verification
        if closed and verify_close:
            try:
                vr = subprocess.run(
                    ["mcporter", "call", "senpi", "strategy_get_clearinghouse_state", "--args",
                     json.dumps({"strategyWalletAddress": wallet})],
                    capture_output=True, text=True, timeout=15
                )
                ch_state = json.loads(vr.stdout)
                positions = ch_state.get("data", {}).get("assetPositions", [])
                still_open = any(
                    p.get("position", {}).get("coin") == asset
                    for p in positions
                )
                close_verified = not still_open
                if still_open:
                    state["pendingClose"] = True
                    state["active"] = True
                    close_result += " | VERIFY_FAILED: position still open"
            except Exception:
                close_verified = None
    else:
        close_result = "error: no wallet in state file"
        state["pendingClose"] = True

# ─── Save state ───
state["lastCheck"] = now
state["lastPrice"] = price
with open(STATE_FILE, "w") as f:
    json.dump(state, f, indent=2)

# ─── Build output ───
if is_long:
    retrace_from_hw = (1 - price / hw) * 100 if hw > 0 else 0
else:
    retrace_from_hw = (price / hw - 1) * 100 if hw > 0 else 0

tier_name = f"Tier {tier_idx+1} ({tiers[tier_idx]['triggerPct']}%→lock {tiers[tier_idx]['lockPct']}%)" if tier_idx >= 0 else "None"
previous_tier_name = None
if tier_changed and previous_tier_idx >= 0:
    t = tiers[previous_tier_idx]
    previous_tier_name = f"Tier {previous_tier_idx+1} ({t['triggerPct']}%→lock {t['lockPct']}%)"
elif tier_changed:
    previous_tier_name = "None (Phase 1)"

if tier_floor:
    if is_long:
        locked_profit = round((tier_floor - entry) * size, 2)
    else:
        locked_profit = round((entry - tier_floor) * size, 2)
else:
    locked_profit = 0

# Elapsed minutes since trade opened
elapsed_minutes = 0
if state.get("createdAt"):
    try:
        created = datetime.fromisoformat(state["createdAt"].replace("Z", "+00:00"))
        elapsed_minutes = round((datetime.now(timezone.utc) - created).total_seconds() / 60)
    except (ValueError, TypeError):
        pass

# Distance to next tier
distance_to_next_tier = None
next_tier_idx = tier_idx + 1
if next_tier_idx < len(tiers):
    distance_to_next_tier = round(tiers[next_tier_idx]["triggerPct"] - upnl_pct, 2)

print(json.dumps({
    "status": "inactive" if closed else ("error" if state.get("pendingClose") else "active"),
    "asset": state["asset"],
    "direction": direction,
    "price": price,
    "upnl": round(upnl, 2),
    "upnl_pct": round(upnl_pct, 2),
    "phase": phase,
    "hw": hw,
    "floor": effective_floor,
    "trailing_floor": trailing_floor,
    "tier_floor": tier_floor,
    "tier_name": tier_name,
    "locked_profit": locked_profit,
    "retrace_pct": round(retrace_from_hw, 2),
    "breach_count": breach_count,
    "breaches_needed": breaches_needed,
    "breached": breached,
    "should_close": should_close,
    "closed": closed,
    "close_result": close_result,
    "close_verified": close_verified,
    "time": now,
    # v3 enriched fields
    "tier_changed": tier_changed,
    "previous_tier": previous_tier_name,
    "elapsed_minutes": elapsed_minutes,
    "distance_to_next_tier_pct": distance_to_next_tier,
    "velocity": velocity,
    "velocity_toward_floor": round(velocity_toward_floor, 4),
    "pending_close": state.get("pendingClose", False),
    "consecutive_failures": state.get("consecutiveFetchFailures", 0)
}))
