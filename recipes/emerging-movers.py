#!/usr/bin/env python3
"""Emerging Movers Detector v1
Tracks SM market concentration rank changes over time.
Flags assets accelerating up the ranks before they hit top 3.

Runs every 1-3 min. Stores history in a JSON file.
Outputs alerts when an asset's rank is climbing consistently.

Key signals:
- Rank climbing: moved up 3+ positions in last 2-3 scans
- Contribution acceleration: pct_of_top_traders_gain increasing scan-over-scan
- Fresh entry: appeared in top 25 for first time (wasn't there before)
- Price confirmation: price moving in the SM direction

Uses: leaderboard_get_markets (single API call)
"""
import json, subprocess, sys, os
from datetime import datetime, timezone

HISTORY_FILE = os.environ.get("EMERGING_HISTORY", "/data/workspace/emerging-movers-history.json")
MAX_HISTORY = 60  # keep last 60 scans (~60 min at 1-min intervals)
TOP_N = 25  # track top 25 markets
RANK_CLIMB_THRESHOLD = 3  # alert if climbed 3+ ranks
CONTRIBUTION_ACCEL_THRESHOLD = 0.003  # 0.3% contribution increase (more sensitive)
MIN_SCANS_FOR_TREND = 2  # need at least 2 prior scans to detect trend

# ─── Load history ───
try:
    with open(HISTORY_FILE) as f:
        history = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    history = {"scans": []}

# ─── Fetch current market concentration ───
try:
    r = subprocess.run(
        ["mcporter", "call", "senpi", "leaderboard_get_markets", "limit=100"],
        capture_output=True, text=True, timeout=30
    )
    result = json.loads(r.stdout)
    if not result.get("success"):
        print(json.dumps({"status": "error", "error": "API call failed", "detail": r.stdout[:500]}))
        sys.exit(1)
    
    raw_markets = result["data"]["markets"]["markets"]
except Exception as e:
    print(json.dumps({"status": "error", "error": str(e)}))
    sys.exit(1)

# ─── Parse current scan ───
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
current_scan = {
    "time": now,
    "markets": []
}

for i, m in enumerate(raw_markets[:TOP_N]):
    current_scan["markets"].append({
        "token": m["token"],
        "dex": m.get("dex", ""),
        "rank": i + 1,
        "direction": m["direction"],
        "contribution": round(m["pct_of_top_traders_gain"], 6),
        "traders": m["trader_count"],
        "price_chg_4h": round(m.get("token_price_change_pct_4h") or 0, 4)
    })

# ─── Analyze trends ───
alerts = []
prev_scans = history["scans"]

if len(prev_scans) >= MIN_SCANS_FOR_TREND:
    # Build lookup for previous scans
    def get_market_in_scan(scan, token, dex=""):
        for m in scan["markets"]:
            if m["token"] == token and m.get("dex", "") == dex:
                return m
        return None
    
    latest_prev = prev_scans[-1]
    oldest_available = prev_scans[-min(len(prev_scans), 5)]  # look back up to 5 scans
    
    for market in current_scan["markets"]:
        token = market["token"]
        dex = market.get("dex", "")
        current_rank = market["rank"]
        current_contrib = market["contribution"]
        
        prev_market = get_market_in_scan(latest_prev, token, dex)
        old_market = get_market_in_scan(oldest_available, token, dex)
        
        alert_reasons = []
        
        # 1. Fresh entry — wasn't in top 25 last scan
        if prev_market is None and current_rank <= 20:
            alert_reasons.append(f"NEW_ENTRY at rank #{current_rank}")
        
        # 2. Rank climbing
        if prev_market:
            rank_change_1 = prev_market["rank"] - current_rank  # positive = climbing
            if rank_change_1 >= 2:
                alert_reasons.append(f"RANK_UP +{rank_change_1} (#{prev_market['rank']}→#{current_rank})")
        
        if old_market:
            rank_change_total = old_market["rank"] - current_rank
            if rank_change_total >= RANK_CLIMB_THRESHOLD:
                alert_reasons.append(f"CLIMBING +{rank_change_total} over {min(len(prev_scans), 5)} scans")
        
        # 3. Contribution acceleration
        if prev_market:
            contrib_delta = current_contrib - prev_market["contribution"]
            if contrib_delta >= CONTRIBUTION_ACCEL_THRESHOLD:
                alert_reasons.append(f"ACCEL +{contrib_delta:.3f} contribution")
        
        # 4. Consistent climb — check if rank improved in each of last 3 scans
        if len(prev_scans) >= 3:
            ranks = []
            for scan in prev_scans[-3:]:
                m = get_market_in_scan(scan, token, dex)
                if m:
                    ranks.append(m["rank"])
                else:
                    ranks.append(TOP_N + 1)  # wasn't in top N
            ranks.append(current_rank)
            
            # Check monotonic improvement (each rank <= previous)
            if all(ranks[i] >= ranks[i+1] for i in range(len(ranks)-1)) and ranks[0] > ranks[-1]:
                streak = ranks[0] - ranks[-1]
                if streak >= 2:
                    alert_reasons.append(f"STREAK climbing {streak} ranks over 4 checks")
        
        # Calculate contribution velocity (avg change per scan over last 5)
        contrib_velocity = 0
        recent_contribs = []
        for scan in prev_scans[-5:]:
            m = get_market_in_scan(scan, token, dex)
            if m:
                recent_contribs.append(m["contribution"])
        recent_contribs.append(current_contrib)
        
        if len(recent_contribs) >= 2:
            deltas = [recent_contribs[i+1] - recent_contribs[i] for i in range(len(recent_contribs)-1)]
            contrib_velocity = sum(deltas) / len(deltas)
            
            # Alert on sustained positive velocity even without other triggers
            if contrib_velocity > 0.002 and len(recent_contribs) >= 3 and not alert_reasons:
                alert_reasons.append(f"VELOCITY +{contrib_velocity*100:.3f}%/scan sustained")
        
        if alert_reasons:
            # Build contribution history for context
            contrib_history = []
            for scan in prev_scans[-5:]:
                m = get_market_in_scan(scan, token, dex)
                if m:
                    contrib_history.append(round(m["contribution"] * 100, 2))
                else:
                    contrib_history.append(None)
            contrib_history.append(round(current_contrib * 100, 2))
            
            rank_history = []
            for scan in prev_scans[-5:]:
                m = get_market_in_scan(scan, token, dex)
                if m:
                    rank_history.append(m["rank"])
                else:
                    rank_history.append(None)
            rank_history.append(current_rank)
            
            dir_label = market["direction"].upper()
            alerts.append({
                "token": token,
                "dex": dex if dex else None,
                "signal": f"{token} {dir_label}",
                "direction": dir_label,
                "currentRank": current_rank,
                "contribution": round(current_contrib * 100, 3),
                "contribVelocity": round(contrib_velocity * 100, 4),
                "traders": market["traders"],
                "priceChg4h": market["price_chg_4h"],
                "reasons": alert_reasons,
                "reasonCount": len(alert_reasons),
                "rankHistory": rank_history,
                "contribHistory": contrib_history
            })

# ─── Save history ───
history["scans"].append(current_scan)
if len(history["scans"]) > MAX_HISTORY:
    history["scans"] = history["scans"][-MAX_HISTORY:]

with open(HISTORY_FILE, "w") as f:
    json.dump(history, f, indent=2)

# ─── Output ───
# Sort alerts by number of reasons (most signals = most interesting)
alerts.sort(key=lambda a: len(a["reasons"]), reverse=True)

output = {
    "status": "ok",
    "time": now,
    "totalMarkets": len(current_scan["markets"]),
    "scansInHistory": len(history["scans"]),
    "alerts": alerts,
    "hasEmergingMover": len(alerts) > 0,
    "top5": [
        {"signal": f"{m['token']} {m['direction'].upper()}", "rank": m["rank"],
         "contribution": round(m["contribution"]*100, 2), "traders": m["traders"],
         "priceChg4h": m["price_chg_4h"]}
        for m in current_scan["markets"][:5]
    ]
}

print(json.dumps(output))
