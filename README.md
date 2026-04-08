# Prediction Market Arbitrage

Cross-platform spread trading engine for **Polymarket** and **Kalshi**. Monitors equivalent markets on both platforms, detects pricing divergences, and is being tuned around short-duration convergence opportunities rather than hold-to-resolution betting.

## Repository

Primary repository for this project:

- [https://github.com/jmoss82/pm-arbitrage-bot](https://github.com/jmoss82/pm-arbitrage-bot)

## Strategy: Convergence Trading

Both Polymarket and Kalshi offer binary prediction markets on the same real-world events. Because they're separate platforms with separate liquidity pools, the prices for equivalent YES/NO contracts can diverge. When they diverge enough, there's a tradeable spread:

1. **Enter**: Buy YES on the platform where it's cheaper, buy NO on the other
2. **Monitor**: Watch the spread between the two platforms
3. **Exit**: When prices converge (the spread narrows), sell both positions for a profit
4. **Profit** = spread compression - round-trip fees

This is **not** a hold-to-resolution strategy. We never wait for the market to settle. One side of every binary market goes to zero at resolution -- holding through that means your guaranteed loser wipes out more than the winner pays. Instead, we trade the spread: get in, ride the wave of convergence, and get out.

## Current Focus

The repo is still a general cross-platform arbitrage engine, but the current research focus is **BTC 15-minute markets**.

Why BTC 15-minute windows are the main testbed right now:

- They repeat every 15 minutes, so one session produces a lot of observations
- They appear to show more frequent cross-platform dislocations than the earlier generic event scans
- They offer shorter holding periods and faster feedback than long-dated political or sports markets

The dedicated monitor for this work is `btc15m_monitor.py`, which auto-discovers the active 15-minute window on both venues, rotates on expiry, and logs CSV data for later analysis.

Runtime execution is now **BTC-only by default** (`ARB_BTC15_ONLY=true`), so `scan`, `monitor`, and `execute` focus on one active BTC 15-minute window pair instead of broad multi-market matching.

Important: on Polymarket, the two outcome prices do **not** reliably sum to `1.00`. They are often overround and occasionally briefly underround. Because of that, midpoint divergence is only a research signal; executable edge is what matters for entry decisions.

### Why Convergence Instead of Hold-to-Resolution

| | Hold to Resolution | Convergence |
|---|---|---|
| **Outcome** | Wait for market to settle | Exit when spread narrows |
| **Risk** | One position always goes to $0 | Both positions retain value |
| **Max loss** | Full position on the losing side | Round-trip fees + spread widening |
| **Profit source** | Guaranteed $1 payout minus costs | Spread compression |
| **Time in market** | Days to months | Minutes to days |
| **Capital efficiency** | Locked until resolution | Freed on exit, recyclable |

## Current Status

**Phase: Core engine rebuilt around convergence model, with BTC 15-minute monitoring as the primary live research track.**

What's built:

- [x] **Kalshi Python client** - Full REST API with RSA-PSS request signing
- [x] **Polymarket client** - CLOB order execution + Gamma API market discovery, runtime key derivation
- [x] **Cross-platform market matcher** - Event-level fuzzy matching with entity extraction
- [x] **Spread scanner** - Detects cross-platform price divergences, estimates round-trip fees for entry + exit
- [x] **Position manager** - Tracks open arb positions, monitors spread compression, generates exit signals (target hit or stop-loss)
- [x] **Arb executor** - Handles both entry (open spread) and exit (close spread) on both platforms simultaneously
- [x] **CLI with 7 modes** - `discover`, `match`, `scan`, `monitor`, `execute`, `positions`, `status`
- [x] **Persistence** - Open positions saved to disk, survives restarts
- [x] **BTC 15-minute monitor** - Auto-discovers active BTC windows, logs midpoint spreads, executable edge, and venue overround
- [x] **BTC-only runtime mode** - `scan`/`monitor`/`execute` restricted to the active BTC 15-minute pair
- [x] **Execution telemetry** - signal/execution/lifecycle logs under `data/`
- [x] **Deployment scaffolding** - `Dockerfile`, `railway.json`, and `render.yaml`

What's next:

- [ ] Fund Polymarket account for live testing
- [ ] Dry-run the full entry/exit cycle on real BTC 15-minute spreads
- [ ] Tune entry/exit parameters against 2-second BTC polling behavior
- [ ] Add WebSocket feeds for real-time price monitoring (optional next upgrade)
- [ ] Add alerting (Discord/Telegram) for entry/exit signals
- [x] **Live spread monitors** - Single-market tracking with CSV logging for post-event analysis
- [x] **API fixes** - Kalshi `orderbook_fp` format support, Polymarket midpoint-based pricing for sparse books

## Project Structure

```
Prediction Market Arbitrage/
|
|-- main.py                  CLI entry point (7 modes)
|-- config.py                Unified env config for both platforms
|-- kalshi_client.py         Kalshi Trade API v2 (RSA-PSS auth, REST, pagination)
|-- polymarket_client.py     Polymarket CLOB + Gamma + Data API wrapper
|-- market_matcher.py        Cross-platform market pairing (fuzzy + manual)
|-- arb_scanner.py           Spread detection with round-trip fee model
|-- arb_executor.py          Entry + exit execution on both platforms
|-- position_manager.py      Open position tracking, exit signals, persistence
|-- spread_monitor.py        Single-market live spread tracker with CSV export
|-- btc15m_monitor.py        BTC 15-minute auto-discovery monitor with executable edge logging
|
|-- .env                     API credentials (gitignored)
|-- .env.example             Credential template
|-- requirements.txt         Python dependencies
|-- data/                    Position state, pair mappings, BTC monitor CSV captures
```

## Setup

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in credentials from both platforms. Polymarket API keys are IP-bound and get derived at runtime from your private key.

## Usage

```bash
# Check account balances on both platforms
python main.py status

# Browse active markets
python main.py discover

# Find cross-platform market pairs
python main.py match --min-score 65

# One-shot spread scan (read-only)
python main.py scan

# View open arb positions with live P&L
python main.py positions

# Continuous monitoring: scan for entries + watch positions for exits
python main.py monitor --scan-only

# Continuous monitoring with execution (respects ARB_DRY_RUN)
python main.py monitor

# One-shot: scan + enter any found spread opportunities
python main.py execute

# BTC 15-minute research monitor
python btc15m_monitor.py --interval 2
```

## Configuration

All settings in `.env`:

| Variable | Default | Description |
|---|---|---|
| `ARB_SCAN_INTERVAL` | `5` | Seconds between scans in monitor mode |
| `ARB_BTC15_ONLY` | `true` | Restrict monitor/scan/execute to the current BTC 15-minute market pair only. |
| `ARB_MIN_EDGE` | `0.05` | Minimum spread (5 cents) to enter -- currently a working threshold and subject to BTC monitor results |
| `ARB_MAX_POSITION_USD` | `50.0` | Max USD per position (both legs combined) |
| `ARB_MAX_DAILY_SPEND` | `500.0` | Daily spend cap across all entries |
| `ARB_DRY_RUN` | `true` | Paper trading mode. Keep `true` during calibration. |
| `ARB_ENABLE_LIVE` | `false` | Additional hard gate. Live only allowed when this is true. |
| `ARB_LIVE_CONFIRM_EXPECTED` | `I_UNDERSTAND_LIVE_TRADING_RISK` | Required confirmation token value. |
| `ARB_LIVE_CONFIRM` | _(empty)_ | Must exactly match `ARB_LIVE_CONFIRM_EXPECTED` to arm live mode. |
| `ARB_REQUIRE_BALANCE_CHECK` | `true` | Enforce account balance checks before live trading. |
| `ARB_MIN_KALSHI_BALANCE_USD` | `25.0` | Minimum Kalshi cash required for live preflight. |
| `ARB_MIN_POLY_BALANCE_USD` | `25.0` | Minimum Polymarket USDC required for live preflight. |
| `ARB_MAX_OPEN_POSITIONS` | `5` | Cap concurrent open arb positions. |

### BTC 15-minute entry controls

| Variable | Default | Description |
|---|---|---|
| `ARB_BTC15_TIME_GATING` | `true` | Enable entry-time gating for BTC 15m windows. |
| `ARB_ENTRY_MIN_SECONDS_IN_WINDOW` | `45` | Do not enter too early in a fresh 15m window. |
| `ARB_ENTRY_MAX_SECONDS_IN_WINDOW` | `780` | Stop entering near window end (avoid stale/late trades). |
| `ARB_ENTRY_COOLDOWN_SECONDS_AFTER_ROLLOVER` | `20` | Cooldown right after rollover before new entries. |
| `ARB_MIN_EDGE_PERSIST_SCANS` | `2` | Edge must persist for N scans before entering. |
| `ARB_MAX_POLY_OVERROUND` | `0.04` | Reject entries when Polymarket implied total is too distorted. |
| `ARB_MIN_KALSHI_LEVEL_QTY` | `10` | Require minimum size at the Kalshi level used for entry. |
| `ARB_MAX_SIGNAL_AGE_SECONDS` | `8` | Reject stale signals between detection and order submit. |

### Limit execution controls

| Variable | Default | Description |
|---|---|---|
| `ARB_POLY_LIMIT_OFFSET` | `0.00` | Buy adds offset, sell subtracts offset from Poly limit price. |
| `ARB_KALSHI_LIMIT_OFFSET_CENTS` | `0` | Add cents to Kalshi buy limits for fill aggressiveness. |
| `ARB_ORDER_REPRICE_ATTEMPTS` | `0` | Reserved for repricing policy. |
| `ARB_ORDER_TIMEOUT_SECONDS` | `4` | Reserved for cancel/timeout policy. |
| `ARB_ALLOW_PARTIAL_FILLS` | `false` | If false, partials are flagged for manual handling. |
| `ARB_ENTRY_MARKETABLE` | `true` | Use marketable-limit behavior on entry for fast fills. |
| `ARB_POLY_ENTRY_AGGRESSION` | `0.01` | Extra price added to Poly entry limits. |
| `ARB_KALSHI_ENTRY_AGGRESSION_CENTS` | `1` | Extra cents added to Kalshi entry limits. |
| `ARB_EXIT_LIMIT_ONLY` | `true` | Keep exits limit-first by default. |
| `ARB_POLY_EXIT_PASSIVE_OFFSET` | `0.01` | Passive premium above bid for Poly exit limits. |
| `ARB_KALSHI_EXIT_PASSIVE_OFFSET_CENTS` | `1` | Passive premium above bid for Kalshi exit limits. |

## Current paper-trading profile

Current local `.env` tuning for conservative dry-run validation:

- `ARB_BTC15_ONLY=true`
- `ARB_SCAN_INTERVAL=2`
- `ARB_MIN_EDGE=0.02`
- `ARB_MAX_POSITION_USD=5.0`
- `ARB_MAX_DAILY_SPEND=25.0`
- Entry style: marketable-limit in (`ARB_ENTRY_MARKETABLE=true`)
- Exit style: limit-first out (`ARB_EXIT_LIMIT_ONLY=true`)

## Dry-run to live arming

Live execution is fail-closed. The bot only sends live orders when all are true:

1. `ARB_DRY_RUN=false`
2. `ARB_ENABLE_LIVE=true`
3. `ARB_LIVE_CONFIRM` exactly equals `ARB_LIVE_CONFIRM_EXPECTED`
4. Live preflight checks pass (balances, guardrails)

If any check fails, monitor/execute mode aborts with a preflight error.

## Trade telemetry

The bot writes structured logs under `data/`:

- `trade_signals.jsonl` -- signal seen, accepted/rejected, and rejection reason
- `trade_executions.jsonl` -- per-leg execution outcomes and errors
- `trade_lifecycle.csv` -- position lifecycle summary and realized P&L on close

Use these for dry-run acceptance gates before enabling live mode.

## Deployment

### Railway (first target)

- `Dockerfile` is the source of truth for build/run.
- `railway.json` configures restart policy.
- Default runtime is safe (`ARB_DRY_RUN=true`, `ARB_ENABLE_LIVE=false`).

### Render parity

- `render.yaml` defines a worker using the same Docker image.
- Keep identical environment defaults to avoid behavior drift.

## Operator runbook (pre-live)

1. Run dry mode continuously and review telemetry quality:
   - signal quality,
   - filtered late-window entries,
   - partial fill frequency,
   - lifecycle P&L behavior.
2. Tune strategy controls (`time gating`, `persistence`, `overround`, `liquidity`).
3. Reduce sizing limits for first live session.
4. Arm live mode with explicit confirm token.
5. Watch first live session closely and disable live gate immediately if behavior deviates.

## Fee Model (Round-Trip)

Fees are estimated for the full round trip: entry + exit, no resolution.

| Platform | Fee Rate | Applied to |
|---|---|---|
| Polymarket | ~2% | Profit on each individual trade (buy low, sell higher) |
| Kalshi | ~7% | Profit on each individual trade |

The scanner calculates whether the detected spread is wide enough to cover fees on both the entry and exit trades. If you buy at 0.40 and later sell at 0.48, the fee applies to the 0.08 profit -- not the full position. If you sell at a loss, no fee on that leg.

For BTC 15-minute monitoring, the important distinction is:

- midpoint spread is useful for observing dislocations,
- executable edge is the relevant measure for whether a trade is actually there.

## Exit Logic

The position manager watches each open position and signals an exit in two cases:

| Signal | Trigger | Action |
|---|---|---|
| **Target** | Spread compresses by 60% of entry width | Take profit -- close both legs |
| **Stop-loss** | Spread widens by 50% beyond entry width | Cut losses -- close both legs |

These thresholds are configurable. The target/stop percentages are set in the `PositionManager` constructor and can be tuned as you observe real spread behavior.

## Risk Considerations

| Risk | Description | Mitigation |
|---|---|---|
| **Execution** | One leg fills, the other fails | System warns on partial fills; manual review needed |
| **Liquidity** | Thin books cause slippage | Position sizing is conservative; scan checks book depth |
| **Timing** | Prices move between placing both orders | Orders placed near-simultaneously; limit orders prevent overpay |
| **Overround / underround** | Venue totals may not sum to `1.00`, especially on Polymarket | Log both sides directly; do not force synthetic complementarity on research data |
| **Spread widening** | Spread moves against you after entry | Stop-loss exit signal triggers at configured threshold |
| **Fee changes** | Platform fee structures can change | Fee rates are configurable constants |
| **Matching** | False positive on different events | Manual pair verification before live; entity-aware scoring |

**Always start with `ARB_DRY_RUN=true`** and validate matching, pricing, and exit signals before committing real capital.
