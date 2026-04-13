# Prediction Market Arbitrage

Cross-platform spread trading engine for **Polymarket** and **Kalshi**. Monitors equivalent markets on both platforms, detects pricing divergences, and trades short-duration convergence opportunities rather than hold-to-resolution betting.

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

The repo is a general cross-platform arbitrage engine, but the current live trading focus is **BTC 15-minute markets**.

Why BTC 15-minute windows are the primary venue:

- They repeat every 15 minutes, so one session produces a lot of observations
- They show frequent cross-platform dislocations
- They offer shorter holding periods and faster feedback than long-dated political or sports markets

The dedicated monitor for this work is `btc15m_monitor.py`, which auto-discovers the active 15-minute window on both venues, rotates on expiry, and logs CSV data for later analysis.

Runtime execution is **BTC-only by default** (`ARB_BTC15_ONLY=true`), so `scan`, `monitor`, and `execute` focus on one active BTC 15-minute window pair instead of broad multi-market matching.

Important: on Polymarket, the two outcome prices do **not** reliably sum to `1.00`. They are often overround and occasionally briefly underround. The live scanner measures the cross-platform divergence in YES pricing directly and does **not** synthesize `NO = 1 - YES` in the execution path. When a Polymarket book is too thin to have standing quotes, the scanner falls back to the CLOB midpoint so that detection is not silently blocked.

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

**Phase: Live trading on Railway with BTC 15-minute markets.**

What's built:

- [x] **Kalshi Python client** - Full REST API with RSA-PSS request signing
- [x] **Polymarket client** - CLOB order execution + Gamma API market discovery, runtime key derivation
- [x] **Cross-platform market matcher** - Event-level fuzzy matching with entity extraction
- [x] **Spread scanner** - Detects cross-platform YES price divergences and estimates round-trip fees; uses midpoint fallback on thin books to avoid silent detection gaps
- [x] **Position manager** - Tracks open arb positions, monitors spread compression, generates exit signals (target hit, stop-loss, or time stop)
- [x] **Arb executor** - Handles entry and exit on both platforms with marketable-limit orders, entry fill verification, escalating exit logic, and emergency flatten
- [x] **REST latency tuning** - Kalshi and Polymarket snapshot fetches run in parallel, the monitor loop targets a fixed scan cadence, and live Polymarket quotes skip midpoint lookups
- [x] **CLI with 7 modes** - `discover`, `match`, `scan`, `monitor`, `execute`, `positions`, `status`
- [x] **Persistence** - Open positions saved to `data/open_positions.json` (survives restarts locally; ephemeral on Railway/Docker unless a volume is mounted)
- [x] **BTC 15-minute monitor** - Auto-discovers active BTC windows, logs midpoint spreads, executable edge, and venue overround
- [x] **BTC-only runtime mode** - `scan`/`monitor`/`execute` restricted to the active BTC 15-minute pair
- [x] **Execution telemetry** - signal/execution/lifecycle logs under `data/`
- [x] **Deployment** - `Dockerfile`, `railway.json`, and `render.yaml`; live on Railway (Amsterdam region)
- [x] **Polymarket CLOB workarounds** - Conditional allowance refresh, progressive sell-size reduction, and post-exit cooldown to handle the known balance cache bug
- [x] **Emergency safety** - Partial fill detection triggers emergency flatten + entry halt; stuck positions are flagged for manual intervention
- [x] **DST-safe BTC window discovery** - BTC 15-minute market IDs are generated in `America/New_York` rather than a fixed UTC offset

What's next:

- [ ] Add persistent volume or external state store for Railway so positions survive redeploys
- [ ] Add WebSocket feeds for real-time price monitoring (reduce polling overhead)
- [ ] Add alerting (Discord/Telegram) for entry/exit signals and emergency stops
- [ ] Investigate Polymarket settlement latency and dynamic cooldown based on on-chain confirmation

## Project Structure

```
Prediction Market Arbitrage/
|
|-- main.py                  CLI entry point (7 modes)
|-- config.py                Unified env config for both platforms
|-- kalshi_client.py         Kalshi Trade API v2 (RSA-PSS auth, REST, pagination)
|-- polymarket_client.py     Polymarket CLOB + Gamma + Data API wrapper
|-- market_matcher.py        Cross-platform market pairing (fuzzy + manual)
|-- arb_scanner.py           Cross-platform divergence detection with fee model
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

Copy `.env.example` to `.env` and fill in credentials from both platforms. Polymarket API keys are IP-bound and get derived at runtime from your private key — do **not** set `POLY_API_KEY`/`POLY_API_SECRET`/`POLY_API_PASSPHRASE` in hosted environments; let the bot derive them on the server.

For Kalshi, make sure the key and base URL point at the same environment:

- `KALSHI_ENV=prod` -> `https://trading-api.kalshi.com/trade-api/v2`
- `KALSHI_ENV=demo` -> `https://api.elections.kalshi.com/trade-api/v2`

In Railway, prefer `KALSHI_PRIVATE_KEY_BASE64` or a single-line `KALSHI_PRIVATE_KEY_PEM` with literal `\n` escapes. Multiline PEM pastes are easy to mangle in hosted env UIs.

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
| `ARB_BTC15_ONLY` | `true` | Restrict monitor/scan/execute to the current BTC 15-minute market pair only |
| `ARB_MIN_EDGE` | `0.05` | Minimum net edge (divergence minus fees) to enter |
| `ARB_MAX_POSITION_USD` | `50.0` | Max USD per position (both legs combined) |
| `ARB_MAX_DAILY_SPEND` | `500.0` | Daily spend cap across all entries |
| `ARB_DRY_RUN` | `true` | Paper trading mode. Keep `true` during calibration |
| `ARB_ENABLE_LIVE` | `false` | Additional hard gate. Live only allowed when this is true |
| `ARB_REQUIRE_BALANCE_CHECK` | `true` | Enforce account balance checks before live trading |
| `ARB_MIN_KALSHI_BALANCE_USD` | `25.0` | Minimum Kalshi cash required for live preflight |
| `ARB_MIN_POLY_BALANCE_USD` | `25.0` | Minimum Polymarket USDC required for live preflight |
| `ARB_MAX_OPEN_POSITIONS` | `1` | Cap concurrent open arb positions |

### BTC 15-minute entry controls

| Variable | Default | Description |
|---|---|---|
| `ARB_BTC15_TIME_GATING` | `true` | Enable entry-time gating for BTC 15m windows |
| `ARB_ENTRY_MIN_SECONDS_IN_WINDOW` | `45` | Do not enter too early in a fresh 15m window |
| `ARB_ENTRY_MAX_SECONDS_IN_WINDOW` | `600` | Stop entering near window end (ensures minimum 5 min holding time) |
| `ARB_ENTRY_COOLDOWN_SECONDS_AFTER_ROLLOVER` | `20` | Cooldown right after rollover before new entries |
| `ARB_FORCE_EXIT_SECONDS_REMAINING` | `120` | Force BTC exits when the window is too close to expiry. Also blocks new entries when remaining time is at or below this threshold |
| `ARB_MIN_EDGE_PERSIST_SCANS` | `2` | Edge must persist for N scans before entering |
| `ARB_MAX_POLY_OVERROUND` | `0.04` | Reject entries when the live Polymarket YES+NO total is too distorted |
| `ARB_MIN_KALSHI_LEVEL_QTY` | `10` | Require minimum size at the Kalshi level used for entry |
| `ARB_MAX_SIGNAL_AGE_SECONDS` | `8` | Reject stale signals between detection and order submit |
| `ARB_EXIT_TARGET_PCT` | `0.60` | Exit when divergence compresses by this fraction (0.60 = 60% compression) |
| `ARB_STOP_LOSS_PCT` | `1.00` | Exit when divergence widens by this fraction beyond entry (1.00 = doubles from entry) |

### Execution controls

| Variable | Default | Description |
|---|---|---|
| `ARB_ENTRY_MARKETABLE` | `true` | Use marketable-limit orders on entry for fast fills |
| `ARB_POLY_ENTRY_AGGRESSION` | `0.01` | Extra price added to Poly entry limits to cross the spread |
| `ARB_KALSHI_ENTRY_AGGRESSION_CENTS` | `1` | Extra cents added to Kalshi entry limits to cross the spread |
| `ARB_EXIT_LIMIT_ONLY` | `false` | When false, exits use marketable-limit orders (cross the spread); when true, exits rest passively on the book |
| `ARB_POLY_LIMIT_OFFSET` | `0.00` | Buy adds offset, sell subtracts offset from Poly limit price |
| `ARB_KALSHI_LIMIT_OFFSET_CENTS` | `0` | Add cents to Kalshi buy limits for fill aggressiveness |
| `ARB_POLY_EXIT_PASSIVE_OFFSET` | `0.01` | Passive premium above bid for Poly exit limits (used when `EXIT_LIMIT_ONLY=true`) |
| `ARB_KALSHI_EXIT_PASSIVE_OFFSET_CENTS` | `1` | Passive premium above bid for Kalshi exit limits (used when `EXIT_LIMIT_ONLY=true`) |
| `ARB_EXIT_REPRICE_ATTEMPTS` | `2` | Number of exit repricing attempts before final aggressive flatten |
| `ARB_EXIT_FILL_TIMEOUT_SECONDS` | `2` | Seconds to wait for each exit attempt to fill before cancel/reprice |
| `ARB_ORDER_REPRICE_ATTEMPTS` | `0` | Reserved for entry repricing policy |
| `ARB_ORDER_TIMEOUT_SECONDS` | `4` | Seconds to wait for entry fills before cancelling unfilled orders |
| `ARB_ALLOW_PARTIAL_FILLS` | `false` | If false, partials trigger emergency flatten + entry halt |
| `ARB_ESTIMATED_ROUND_TRIP_SLIPPAGE` | `0.01` | Additional fixed friction buffer added to round-trip cost estimates |

### Polymarket CLOB workarounds

| Variable | Default | Description |
|---|---|---|
| `ARB_POLY_SELL_SIZE_FACTOR` | `0.95` | Sell this fraction of held shares to account for taker fees and the CLOB balance cache bug |
| `ARB_EXIT_COOLDOWN_SECONDS` | `60` | Seconds to block re-entry after any exit, giving Polymarket time to settle matched orders on-chain |

## Exit Logic

The position manager tracks the live cross-platform YES divergence for each open position — the same metric used at entry. It signals an exit when:

| Signal | Trigger | Action |
|---|---|---|
| **Target** | Divergence narrows to 40% of entry width (60% compression) **and** unrealized P&L >= $0 | Take profit — close both legs |
| **Stop-loss** | Divergence widens to 200% of entry width (or trailing stop level) | Cut losses — close both legs |
| **Time stop** | BTC window has < 120s remaining | Force exit — close both legs regardless of P&L |

The target exit includes a P&L floor: it will not fire if the actual unrealized P&L is negative, even when the divergence metric has compressed past the threshold. This prevents locking in a loss to bid-ask friction on a "profitable" divergence move. The time stop and stop-loss fire unconditionally.

A **trailing stop** ratchets the stop-loss tighter as the position moves in your favor. Once the divergence has compressed 40% from entry, the stop-loss moves to breakeven (entry spread). At 60% compression, it tightens to 70% of entry spread. The stop never moves back — it only gets tighter.

Exit target and stop-loss thresholds are configurable via `ARB_EXIT_TARGET_PCT` and `ARB_STOP_LOSS_PCT` environment variables.

## Live Execution Notes

- Entry decisions are based on the cross-platform YES divergence. The scanner prefers raw order book prices but falls back to the CLOB midpoint when a book is too thin, so detection is never silently blocked by sparse liquidity on one outcome.
- A position is only opened after both venue legs are confirmed filled. Posted or resting entry orders are cancelled after the entry timeout and are not treated as open arb positions.
- Open-position spread tracking uses the same divergence metric as entry (cross-platform YES gap). P&L uses exitable bids for the held legs.
- Monitor mode now targets a fixed scan cadence. It sleeps only for the remainder of `ARB_SCAN_INTERVAL` after each loop rather than doing `scan work + full interval`.
- Snapshot fetches are partially parallelized: Kalshi market data and the Polymarket quote pass run concurrently, but Polymarket YES/NO book reads stay single-threaded inside one client because concurrent CLOB reads proved unstable in production.
- Polymarket quote reads now retry briefly on request exceptions before the scan gives up on that venue for the current cycle.

### Exit escalation

When an exit is triggered, the executor follows an escalation sequence:

1. **At bid** — first attempt prices the sell at the current bid (when `ARB_EXIT_LIMIT_ONLY=false`)
2. **Reprice** — cancel and resubmit at progressively more aggressive prices (bid minus 1¢ per retry, `ARB_EXIT_REPRICE_ATTEMPTS` rounds)
3. **Final dump** — submit at $0.01 on Polymarket / 1¢ on Kalshi to guarantee a fill

On Polymarket specifically, sells go through a progressive size reduction (95% → 93% → 90% → 85% of held shares) because the CLOB's server-side balance cache doesn't update instantly after a buy fills. A conditional allowance refresh is called before each sell attempt.

### Post-exit cooldown

After every successful exit, the bot blocks re-entry into the same market/direction for `ARB_EXIT_COOLDOWN_SECONDS` (default 60s). This prevents the bot from immediately re-entering while Polymarket tokens from the previous exit are still locked in a matched order awaiting on-chain settlement. Cooldown-blocked opportunities are filtered before execution, so monitor output is less noisy than earlier builds.

## Dry-run to live arming

Live execution is fail-closed. The bot only sends live orders when all are true:

1. `ARB_DRY_RUN=false`
2. `ARB_ENABLE_LIVE=true`
3. Live preflight checks pass (balances, guardrails)

If any check fails, monitor/execute mode aborts with a preflight error.

## Trade telemetry

The bot writes structured logs under `data/`:

- `trade_signals.jsonl` — signal seen, accepted/rejected, and rejection reason
- `trade_executions.jsonl` — per-leg execution outcomes and errors
- `trade_lifecycle.csv` — position lifecycle summary and realized P&L on close

Use these for dry-run acceptance gates before enabling live mode.

`trade_executions.jsonl` now includes per-leg order status, partial-fill state, and venue errors so entry verification failures are visible in telemetry.

## Deployment

### Railway (primary)

- `Dockerfile` is the source of truth for build/run.
- `railway.json` configures restart policy.
- Default runtime is safe (`ARB_DRY_RUN=true`, `ARB_ENABLE_LIVE=false`).
- **Region matters**: Polymarket restricts trading by server location. Amsterdam (EU) works. Singapore and some other regions are close-only or blocked.
- **Ephemeral filesystem**: Position state in `data/` is lost on every redeploy. If the bot is restarted with open positions, those positions become orphaned on the exchanges and must be closed manually.
- **Every push restarts the bot.** Railway rebuilds and redeploys on every commit to `main`. If the bot is holding an open position at the time of the push, the restart kills the process mid-trade — both legs may be left open on-exchange with no automated exit. **Do not push while a position is open.** All pushes should be explicitly cleared by the operator first.
- Do **not** set explicit `POLY_API_KEY`/`POLY_API_SECRET`/`POLY_API_PASSPHRASE` in Railway. These are IP-bound and must be derived on the server from `POLY_PRIVATE_KEY`.

### Render parity

- `render.yaml` defines a worker using the same Docker image.
- Keep identical environment defaults to avoid behavior drift.

## Fee Model (Round-Trip)

Fees are estimated for the full round trip: entry + exit, no resolution. The runtime estimate also includes a configurable fixed slippage/fill-risk buffer via `ARB_ESTIMATED_ROUND_TRIP_SLIPPAGE`.

| Platform | Fee Rate | Applied to |
|---|---|---|
| Polymarket | ~2% | Profit on each individual trade (buy low, sell higher) |
| Kalshi | ~7% | Profit on each individual trade |

The scanner measures the cross-platform YES price divergence and subtracts estimated round-trip fees to determine net edge. Fees are applied to each leg's profit individually — if you buy at 0.40 and later sell at 0.48, the fee applies to the 0.08 profit, not the full position. If you sell at a loss, no fee on that leg.

For BTC 15-minute monitoring, the important distinction is:

- midpoint spread is useful for observing dislocations,
- cross-platform divergence (net of fees) is the relevant measure for whether a trade is actually there.

## API / Polling Behavior

The live runtime is still REST-based.

- `ARB_SCAN_INTERVAL` controls the target scan cadence.
- In BTC-only mode, each scan reads one Kalshi orderbook and both Polymarket outcome books for the active window.
- Kalshi and Polymarket are fetched in parallel at the snapshot level.
- Polymarket quote reads use a small retry/backoff on transient request failures.
- Market identity refresh is lightweight and only happens when the 15-minute BTC window rolls over or when the active pair has not been discovered yet.

This keeps the bot materially faster than earlier builds while avoiding the instability seen with fully concurrent Polymarket CLOB reads through a shared client.

## Risk Considerations

| Risk | Description | Mitigation |
|---|---|---|
| **Execution** | One leg fills, the other fails | Emergency stop + best-effort flatten of the exposed leg when partial fills are disabled |
| **Liquidity** | Thin books cause slippage or create fake midpoint edge | Position sizing is conservative; midpoint fallback on thin books is used for detection only — entry prices use visible quotes where available |
| **Timing** | Prices move between placing both orders | Marketable-limit orders placed near-simultaneously |
| **Overround / underround** | Venue totals may not sum to `1.00`, especially on Polymarket | Log both sides directly; live pricing uses actual YES and NO books instead of synthetic complements |
| **Spread widening** | Spread moves against you after entry | Stop-loss exit signal triggers at configured threshold |
| **Fee changes** | Platform fee structures can change | Fee rates are configurable constants |
| **Matching** | False positive on different events | Manual pair verification before live; entity-aware scoring |
| **Polymarket balance cache** | CLOB's server-side balance doesn't update instantly after buys fill, causing sell failures | Conditional allowance refresh + progressive sell-size reduction (95% → 85%) + post-exit cooldown |
| **Token settlement locking** | Polymarket tokens stay locked in matched orders until on-chain settlement completes | 60s post-exit cooldown blocks re-entry; prevents double-spending locked tokens |
| **Resting entry orders** | A posted but unfilled order can create false state if treated as filled | Entry legs are now verified before opening a position; unfilled entries are cancelled after timeout |
| **Position orphaning** | Railway redeploys wipe in-container state; open positions become stranded on exchanges | Position file persists locally; Railway needs a mounted volume or external state store to survive redeploys |
| **Region restrictions** | Polymarket blocks trading from certain server locations | Deploy to Amsterdam or US regions; avoid Singapore and restricted jurisdictions |

**Always start with `ARB_DRY_RUN=true`** and validate matching, pricing, and exit signals before committing real capital.
