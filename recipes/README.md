# 📦 Senpi Trading Recipes

Automated trading scripts designed for use with [Senpi](https://senpi.ai) on Hyperliquid.
All scripts are zero-LLM-token — pure Python computation using market data via `mcporter` (MCP).

## Recipes

### Core

| Recipe | Description |
|--------|-------------|
| **[dsl-v3.py](dsl-v3.py)** | Dynamic Stop Loss — 2-phase trailing stop with tier ratcheting. Phase 1 (accumulate) gives room; Phase 2 (protect) locks profit at tiers. |
| **[opportunity-scan-v5.py](opportunity-scan-v5.py)** | 4-stage funnel scanner — filters 229 perps down to scored opportunities using smart money, market structure, technicals, and funding. |

### Overlays

| Recipe | Description |
|--------|-------------|
| **[liquidation-cascade.py](liquidation-cascade.py)** | Detects crowded leveraged positions near cascade zones. High scores = imminent liquidation chain = trade into the cascade. |
| **[squeeze-detector.py](squeeze-detector.py)** | Bollinger/Keltner squeeze breakout detector. Scores compression setups with ATR-based targets. |

### Utilities

| Recipe | Description |
|--------|-------------|
| **[opportunity-scan-v4.py](opportunity-scan-v4.py)** | Previous-gen scanner (v4). Still functional, superseded by v5. |
| **[emerging-movers.py](emerging-movers.py)** | Early momentum detector for low-cap movers. |
| **[funding-harvester.py](funding-harvester.py)** | Funding rate arbitrage scanner. |
| **[grid-setup.py](grid-setup.py)** / **[grid-monitor.py](grid-monitor.py)** | Grid trading setup and monitoring. |
| **[backtest-v2.py](backtest-v2.py)** | Backtest framework for scanner strategies. |

## Requirements

- Python 3.10+
- `mcporter` CLI configured with a Senpi MCP server
- No additional pip packages needed (stdlib only)

## Usage

```bash
# Run a scan
mcporter call senpi.read_senpi_guide uri="senpi://guides/senpi-overview"
python3 opportunity-scan-v5.py

# Run DSL on a position
DSL_STATE_FILE=dsl-state-HYPE.json python3 dsl-v3.py

# Run cascade detector
python3 liquidation-cascade.py
```

## Integration

These recipes are designed to run autonomously via heartbeat cron. The [dashboard](https://rheeger.github.io/trading-dashboard/) is auto-updated with each heartbeat cycle.

---

Built with [Senpi](https://senpi.ai) · Powered by [Hyperliquid](https://hyperliquid.xyz)
