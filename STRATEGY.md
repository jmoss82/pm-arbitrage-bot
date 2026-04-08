# Strategy: BTC 15-Minute Cross-Platform Convergence

## Purpose

This document replaces the original project strategy note.

The current working thesis is narrower and more concrete:

- Focus on **BTC 15-minute directional markets**
- Trade **cross-platform convergence** between **Kalshi** and **Polymarket**
- Use short-lived windows to get faster feedback, tighter testing loops, and quicker capital recycling
- Treat this as a **microstructure and execution problem**, not a prediction problem

We are not trying to forecast Bitcoin. We are trying to exploit temporary disagreement between two venues listing effectively the same short-duration contract.

## Current Thesis

BTC 15-minute markets appear to be the best cross-platform opportunity discovered so far because they combine:

- Frequent market creation
- Short holding periods
- Repeatable structure every 15 minutes
- Enough volatility to create price dislocations
- Enough event cadence to gather a lot of data in one session

This is better suited to testing than long-dated political or sports markets because the feedback cycle is measured in minutes instead of hours or days.

## Core Trade

When the two venues disagree on the implied probability for the same BTC 15-minute outcome:

- Buy **UP / YES** on the cheaper venue
- Buy **DOWN / NO** on the more expensive venue
- Exit both when the spread compresses

Profit comes from **convergence**, not resolution.

We do not want to hold these positions to settlement unless forced to by an execution failure or an operational issue.

## Why BTC 15-Minute Markets

BTC 15-minute markets have several properties that make them attractive:

- The market definition is simple and repeats every window
- Both venues can list the same window with similar semantics
- The underlying reference asset trades continuously elsewhere, which tends to discipline extreme mispricings
- Short windows mean a bad trade is not capital-locked for long
- A single evening session can produce many independent observations

This makes the market useful both for eventual deployment and for strategy research.

## Important Market Reality

Do not assume the two Polymarket outcome prices sum to exactly `1.00`.

Observed behavior:

- The two sides are often **overround**, sometimes materially
- Brief **underround** periods can also happen
- Temporary totals above `1.00` are not necessarily a data bug
- Midpoint-based comparisons can overstate edge if they ignore overround and book shape

Because of this, the strategy must distinguish between:

- **Midpoint divergence**: useful as a monitoring signal
- **Executable edge**: the only number that matters for entry decisions

The monitor now logs both.

## Entry Logic

An entry is only interesting if the edge exists on executable prices, not just mids.

Current implementation bias:

- Poll every ~2 seconds in BTC-only mode for active window decisions
- Use marketable-limit pricing for entries to reduce missed dislocations
- Apply strict no-trade filters (time-in-window, persistence, liquidity, overround, signal age)

The two valid entry directions are:

1. **Kalshi YES bid > Polymarket YES ask**
   Buy BTC-up on Polymarket and BTC-down on Kalshi.

2. **Polymarket YES bid > Kalshi YES ask**
   Buy BTC-up on Kalshi and BTC-down on Polymarket.

For every candidate trade, evaluate:

- Gross executable edge
- Estimated round-trip fees
- Net edge after fees
- Total capital required to enter both legs
- Visible liquidity at the quoted levels

The current working threshold is still around **5 cents gross edge**, but this is a testing threshold, not a final production constant.

## Exit Logic

The existing convergence framework still applies:

- Take profit when the spread compresses materially from entry
- Cut the trade if the spread widens well beyond the original entry width
- Avoid sitting in stale positions just because the market has not resolved yet

For BTC 15-minute windows, the bias should be toward **faster exits**, because:

- The contracts are short-lived
- New windows create new opportunities constantly
- Capital trapped in an old window is capital not available for the next one

Current implementation bias:

- Exit is limit-first by default (protect edge capture)
- Stop-loss and target signals still drive close decisions
- If partials occur, treat as operational risk and escalate quickly

## Current Risk Profile (Dry Run)

The current paper profile is intentionally conservative while validating behavior:

- BTC-only runtime path (single active 15-minute pair)
- 2-second polling cadence
- Small sizing (`$5` max per paired position, `$25` max daily spend)
- Live mode remains gated behind explicit arming controls

## What We Are Actually Testing Right Now

The current objective is not full automation. It is to answer a tighter set of questions:

- Does executable cross-platform edge appear often enough in BTC 15-minute windows?
- How large is the real edge after fees and spread?
- How often does midpoint divergence survive contact with the book?
- How quickly do these dislocations compress?
- Which venue is usually the lagging venue?
- How much slippage appears between detection and order placement?
- How often are both legs realistically fillable at size?

The dedicated BTC monitor exists to answer these questions with repeated short-window observations.

## Operational Rules

These are the current working rules while the system is still in research mode:

- Keep `ARB_DRY_RUN=true` unless deliberately testing live execution
- Prefer read-only monitoring and CSV capture over premature automation
- Treat quoted opportunities as suspect until verified against executable prices
- Do not reuse stale market identifiers across windows
- Retry market discovery during the active window because listings are not always available immediately
- Log venue totals and overround explicitly instead of forcing synthetic complementarity on Polymarket

## Risks Specific to BTC 15-Minute Markets

### Listing Synchronization Risk

The two venues do not always list the new 15-minute window at the same moment.

Implication:

- A window can look unavailable on one venue for part of its life
- Using stale tokens or stale tickers can corrupt the data

### Book Quality Risk

A visible midpoint does not guarantee executable liquidity.

Implication:

- Edge can disappear at the actual best bid/ask
- Thin resting size can make a nominal opportunity untradeable

### Overround Risk

Polymarket can trade with totals above `1.00` for non-trivial periods.

Implication:

- A naive assumption that `down = 1 - up` is wrong often enough to hurt analysis
- Both sides should be fetched and logged directly when available

### Timing Risk

These windows are short and can reprice fast.

Implication:

- Detection latency matters
- Any slower polling can miss short-lived spreads
- Eventual websocket integration may still be useful if 2-second polling proves insufficient

## What Success Looks Like

This BTC-specific strategy is promising if the data shows all of the following:

- Executable edge appears regularly, not just midpoint divergence
- Net edge remains positive after a conservative fee model
- Both legs are fillable often enough to matter
- Convergence usually happens quickly enough to recycle capital
- Losses from spread widening are controlled and infrequent

If those conditions are not met, BTC 15-minute markets are still useful as a research harness, but not yet a deployment target.

## Current Tooling

Relevant components in the current repo:

- [`btc15m_monitor.py`](C:\Users\jmoss\OneDrive\Desktop\Prediction%20Market%20Arbitrage\btc15m_monitor.py): dedicated BTC 15-minute monitor with CSV logging
- [`arb_scanner.py`](C:\Users\jmoss\OneDrive\Desktop\Prediction%20Market%20Arbitrage\arb_scanner.py): executable spread logic and fee estimates
- [`kalshi_client.py`](C:\Users\jmoss\OneDrive\Desktop\Prediction%20Market%20Arbitrage\kalshi_client.py): Kalshi API client
- [`polymarket_client.py`](C:\Users\jmoss\OneDrive\Desktop\Prediction%20Market%20Arbitrage\polymarket_client.py): Polymarket API and book access
- [`data/`](C:\Users\jmoss\OneDrive\Desktop\Prediction%20Market%20Arbitrage\data): captured BTC monitoring sessions and other analysis artifacts

## Next Decisions

The next strategy decisions should come from fresh BTC monitor output, especially the new executable-edge columns:

- Set a real minimum entry threshold based on executable edge, not midpoint spread
- Decide whether position sizing should vary by observed book depth
- Finalize entry aggressiveness and exit passivity offsets
- Decide whether the first live execution test should be fully simultaneous or primary-secondary
- Determine whether websocket feeds are necessary for this market type
- Decide whether BTC 15-minute becomes the first production target or remains a research market
