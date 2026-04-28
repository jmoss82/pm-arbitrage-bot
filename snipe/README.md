# Snipe Strategy — End-of-Window Favorite Sniping on Polymarket BTC 5-min Markets

This is the master reference for the `snipe/` package.  It is a standalone
trading strategy that lives in the same repo as the older cross-platform
arbitrage bot but does not share configuration, kill switches, or decision
logic with it.  The two can run in the same container or on the same host
without interfering.

---

## 1. Strategy in one paragraph

On Polymarket, BTC up/down markets close every 5 minutes with a strict
binary outcome: price is either above or below the reference level at the
stroke of the minute.  In the final ~3–10 seconds of every window, one
outcome typically dominates and trades at $0.95–0.99 while the other
collapses toward $0.00.  **The snipe bot enters the favored side at that
moment and holds the position to resolution**, collecting the $0.01–0.05
spread per share.  Most windows win.  A single wrong-side entry wipes out
the gains from ~20–100 prior wins, which is why entry-gate discipline
(narrow time band, narrow price band, top-of-book size floor, and a hard
per-window entry cap) is the entire game.

---

## 2. Repo layout

```text
snipe/
├── __init__.py           Package marker + docstring
├── README.md             This file
├── config.py             All SNIPE_* env vars and defaults
├── window.py             5-min window math + Gamma market discovery
├── loop.py               Shared async scan loop (used by monitor and run)
├── monitor.py            Read-only CSV logger (Phase 1)
├── positions.py          SnipePosition dataclass + JSON persistence
├── reference_price.py    Chainlink RTDS feed + ReferenceSnapshot
├── scanner.py            evaluate_tick() -- entry decision logic
├── executor.py           FAK (Fill-And-Kill) order submission + dry-run sim
├── settler.py            Gamma resolution poller + P&L
└── main.py               argparse CLI: probe / status / monitor / run / ...
```

Data files live in `data/snipe/` (configurable via `SNIPE_DATA_DIR`):

```text
data/snipe/
├── btc5m_snipe_ticks_YYYYMMDD_HHMMSS.csv      one row per book snapshot
├── btc5m_snipe_windows_YYYYMMDD_HHMMSS.csv    one row per closed window
├── btc5m_snipe_signals_YYYYMMDD_HHMMSS.csv    one row per scanner decision
└── positions.json                             durable snipe position store
```

The CSV filenames carry a session timestamp so multiple restarts in a day
don't clobber each other; `positions.json` is a single shared file across
all runs so position state survives restarts.

---

## 3. Architecture at a glance

```
                 ┌────────────────────────────────────────┐
                 │            run_window_loop             │
                 │        (snipe/loop.py, async)          │
                 │                                        │
                 │  1. resolve current 5-min window       │
                 │  2. poll Polymarket CLOB book          │
                 │     (1.0s normal, 0.3s in tail)        │
                 │  3. build Tick, fan out to handlers:   │
                 └──────────────┬─────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
 ┌───────────────┐     ┌──────────────────┐     ┌──────────────┐
 │ tick CSV +    │     │ scanner handler  │     │ settler      │
 │ window CSV +  │     │ (main.make_      │     │ handler      │
 │ TTY printer   │     │  scanner_handler)│     │ (every 30s)  │
 │               │     │                  │     │              │
 │ monitor.py    │     │ evaluate_tick -> │     │ Gamma resolve│
 │ handlers      │     │ register_attempt │     │ -> P&L       │
 │ (read-only)   │     │ -> execute_entry │     │ -> persist   │
 └───────────────┘     │ -> register_fill │     └──────────────┘
                       │ -> persist pos   │
                       └──────────────────┘
```

The loop is the same in monitor mode and run mode; the difference is just
the set of handlers bound to it.

---

## 4. CLI reference

Always run from the **repo root** (not from `snipe/`):

```bash
python -m snipe.main probe                    # verify slug resolves to a real market
python -m snipe.main status                   # dump effective config as JSON
python -m snipe.main preflight                # check pUSD + arming state
python -m snipe.main monitor                  # read-only logger (runs forever)
python -m snipe.main monitor --duration 10    # read-only logger (10 minutes)
python -m snipe.main run                      # scanner + executor (DRY-RUN by default)
python -m snipe.main run --duration 60        # run for 60 minutes then exit
python -m snipe.main run --yes                # skip the 5-second live confirmation prompt
python -m snipe.main positions                # list open + recent positions
python -m snipe.main positions --last 20      # last 20 only
python -m snipe.main settle                   # one-shot Gamma resolution sweep
```

The Railway container `CMD` is `python -m snipe.main run --yes`.

---

## 5. Configuration (environment variables)

All live in `snipe/config.py`.  Defaults are coded; overrides go in Railway
env vars (or local `.env`).  Nothing from the old `ARB_*` namespace is
read by snipe code.

### Market identity
| Var | Default | Meaning |
|---|---|---|
| `SNIPE_WINDOW_MINUTES` | `5` | Window length, in minutes |
| `SNIPE_POLY_SLUG_PATTERN` | `btc-updown-5m-{ts}` | Slug template with `{ts}` unix-seconds placeholder |
| `SNIPE_GAMMA_SEARCH_QUERY` | `bitcoin up or down` | Fallback search when slug lookup fails |

### Polling cadence
| Var | Default | Meaning |
|---|---|---|
| `SNIPE_POLL_INTERVAL_S` | `1.0` | Normal-cadence poll interval (seconds) |
| `SNIPE_POLL_INTERVAL_TAIL_S` | `0.3` | Tail-cadence poll interval (seconds) |
| `SNIPE_TAIL_WINDOW_S` | `45` | Switch to tail cadence when seconds_remaining ≤ this |

### Entry gates
| Var | Default | Meaning |
|---|---|---|
| `SNIPE_MIN_SECONDS_REMAINING` | `3` | Earliest (smallest) `t_remaining` still acceptable |
| `SNIPE_MAX_SECONDS_REMAINING` | `15` | Latest (largest) `t_remaining` still acceptable |
| `SNIPE_MIN_ENTRY_PRICE` | `0.95` | Minimum ask price on leader side |
| `SNIPE_MAX_ENTRY_PRICE` | `0.99` | Maximum ask price on leader side |
| `SNIPE_MIN_LEADER_PERSIST_TICKS` | `2` | Leader must stay leader for this many ticks |
| `SNIPE_MIN_TOP_OF_BOOK_SIZE` | `10` | Minimum size at the leader's ask |

### Chainlink reference-price gate
Polymarket's Chainlink BTC/USD feed is exposed via the Real-Time Data
Socket (`wss://ws-live-data.polymarket.com`, topic
`crypto_prices_chainlink`, symbol `btc/usd`).  The scanner consults a
live snapshot from that feed on every tick.  The "Price to Beat" is the
first Chainlink tick at/after a window boundary; live distance is
`current_price - price_to_beat`.

| Var | Default | Meaning |
|---|---|---|
| `SNIPE_MIN_REF_DISTANCE_USD` | `25.0` | Minimum `|current - PTB|` in USD to allow an entry |
| `SNIPE_REF_STALE_S` | `3.0` | Max age of the last live tick; older → refuse entry |
| `SNIPE_REF_REQUIRE_DIRECTIONAL_AGREEMENT` | `true` | Book leader side must match sign(distance) |
| `SNIPE_REQUIRE_REF_FEED` | `true` | When true, a missing/uninitialized snapshot hard-rejects |

See section 6 for where these gates sit in the decision flow and what
their reject reasons look like in the signals CSV.

### Pre-submit book guard
| Var | Default | Meaning |
|---|---|---|
| `SNIPE_PRESUBMIT_MIN_ASK_PRICE` | `0.90` | Immediately before a live FAK submit, re-read the raw best ask for the chosen token and abort if it has collapsed below this floor; set `0` to disable |

### Sizing & budgets
| Var | Default | Meaning |
|---|---|---|
| `SNIPE_POSITION_USD` | `5.0` | USD per entry |
| `SNIPE_MAX_ENTRIES_PER_WINDOW` | `1` | Hard cap per 5-min window |
| `SNIPE_MAX_SPEND_PER_DAY_USD` | `50.0` | Session-wide spend cap (UTC day) |
| `SNIPE_MAX_OPEN_POSITIONS` | `3` | Pause entries if this many positions still open |

### Live arming (fail-closed)
| Var | Default | Meaning |
|---|---|---|
| `SNIPE_DRY_RUN` | `true` | Simulate orders; do not submit |
| `SNIPE_ENABLE_LIVE` | `false` | Required in addition to `SNIPE_DRY_RUN=false` to go live |
| `SNIPE_REQUIRE_BALANCE_CHECK` | `true` | Block live start if pUSD balance unreadable |
| `SNIPE_MIN_POLY_BALANCE_USD` | `10.0` | Refuse to start live if pUSD below this |

### Storage
| Var | Default | Meaning |
|---|---|---|
| `SNIPE_DATA_DIR` | `data/snipe` | Where CSVs and `positions.json` live |

### To arm live
Both of these must be set explicitly (fail-closed):
```
SNIPE_DRY_RUN=false
SNIPE_ENABLE_LIVE=true
```
On startup the bot will print `>>> LIVE MODE ARMED <<<` and pause 5 seconds
for you to abort (unless `--yes` is passed).

---

## 6. Decision flow (scanner)

`evaluate_tick()` in `scanner.py` applies gates in the following order.
All must pass for an entry.

1. **`no_leader`** / **`no_ask_on_leader`** — neither side has a clear best ask
2. **`ask_out_of_band`** — leader's best ask outside `[min, max]`
3. **`too_early` / `too_late`** — `seconds_remaining` outside `[min, max]`
4. **`thin_book`** — size at leader's best ask below floor
5. **`leader_unstable`** — leader flipped during the last few ticks
6. **Reference-price gate** (see section 5 "Chainlink reference-price gate"):
   - **`ref_feed_missing`** — snapshot not initialized yet (cold start)
   - **`ref_stale`** — last live tick older than `SNIPE_REF_STALE_S`
   - **`ref_window_partial`** — feed joined mid-window; `price_to_beat` unknown
   - **`ref_no_price_to_beat`** / **`ref_no_current_price`** / **`ref_no_distance`** — snapshot incomplete
   - **`ref_slug_mismatch`** — book-derived slug and feed-derived slug disagree (boundary race)
   - **`ref_too_close`** — `|current − price_to_beat| < SNIPE_MIN_REF_DISTANCE_USD`
   - **`ref_disagree`** — book leader side disagrees with sign of distance
7. **`window_entry_cap`** — per-window entry cap already reached
8. **`max_open_positions`** — too many still-open positions
9. **`daily_cap_hit`** — today's cumulative spend would exceed the daily cap
10. **`accept`** — all gates pass → `execute_entry`

`execute_entry()` applies one final live-only guard before sending the FAK:
it re-reads the raw CLOB best ask for the selected token and refuses the
entry if that ask is below `SNIPE_PRESUBMIT_MIN_ASK_PRICE`.  This catches the
adverse-selection pattern where a $0.98 signal collapses to a $0.70-$0.80
fill during the submit round trip.

### Why the reference-price gate matters

The book alone is not enough.  A leader at $0.97 UP with 300+ size at
the ask still loses if BTC is within a few dollars of the threshold
when the final Chainlink tick lands.  Observed in a live dry-run:

```
t-4.0s  book UP 0.96/0.97  ref current=77773  PTB=77764  dist=+9$
t-3.4s  book UP 0.89/0.97  ref current=77773  PTB=77764  dist=+9$
t-2.8s  book DN 0.80/0.92  ref current=77719  PTB=77764  dist=-54$  (flipped)
```

A `SNIPE_MIN_REF_DISTANCE_USD=25` gate refuses the t-4.0s and t-3.4s
entries (distance +9 < +25) and avoids the catastrophic flip.

When accepted, the flow is:

```
register_attempt(window_slug)      <-- unconditional, BEFORE executor call
    -> execute_entry(...)           <-- FAK order or dry-run sim
        -> on success: register_fill(cost)   <-- updates daily spend
        -> on failure: log reject, do NOT register_fill
    -> persist SnipePosition to positions.json
```

The `register_attempt` call happens **before** the executor, which is the
key to not re-submitting if the SDK call stalls during its first-use
warmup.  Each new 5-min window resets its counter automatically because
the slug changes.

---

## 7. Order execution

`executor.py` uses the Polymarket CLOB SDK's **Fill-And-Kill** order type
(`OrderType.FAK`, equivalent to IOC).  This is the only order type we will
ever send from this bot, because:

- We do not want a resting limit order.  If the market moves away from our
  price in the final second, the order must die, not linger into the next
  window.
- We do not want partial fills we can't control.  FAK fills what it can
  immediately and cancels the rest.

Dry-run mode bypasses the SDK entirely and generates a `SnipePosition`
with `dry_run=True`, a simulated fill at the tick's leader ask, and a
zero-latency `submit_latency_ms`.  Signals written to the signals CSV in
dry-run are clearly tagged.

Submission latency is logged per attempt as `submit_latency_ms`.  In live
mode this is the number that actually matters for strategy viability: if
it's consistently >1500ms in the tail, the time-band will need to shift.

---

## 8. Settlement & P&L

`settler.py` polls Gamma every 30s via the `make_settler_handler` tick
handler.  For each open position whose window has been closed for at
least `SETTLEMENT_GRACE_SECONDS` (90s), it queries the Gamma `/markets`
endpoint with `closed=true&archived=true` (the only filter combination
that reliably returns resolved short-duration markets) and applies the
per-position `PENDING_RETRY_SECONDS` (45s) backoff.

When a market is resolved:
- If the position's outcome token wins, P&L = `1.0 - avg_fill_price` × `filled_size`
- If it loses, P&L = `-avg_fill_price` × `filled_size`
- Status becomes `settled_win` or `settled_loss` and is persisted to
  `positions.json`.

A final settlement sweep runs on graceful shutdown of `run`.

---

## 9. Data outputs

### `btc5m_snipe_ticks_*.csv`
One row per book snapshot (every 0.3–1.0s).  Columns include timestamps,
window slug, seconds remaining, both sides' bid/ask/size, and derived
signals (leader side, leader mid, total implied probability).  This is the
raw research data for tuning parameters.

### `btc5m_snipe_windows_*.csv`
One row per completed window.  Summarizes min/max/final leader price,
total ticks seen, entries submitted, and the eventual resolved outcome.

### `btc5m_snipe_signals_*.csv`
One row per scanner decision where the tick was in the neighborhood of
the price or time bands.  Use this to audit why entries were or were not
triggered.

Columns:

| Column | Meaning |
|---|---|
| `ts_iso`, `window_slug`, `seconds_remaining` | When / which window / how far in |
| `leader_side`, `leader_mid`, `leader_ask`, `leader_ask_size` | Book state |
| `decision` | `accept`, `reject`, `enter`, or `accept_but_failed` |
| `reason` | Failing-gate name (see section 6 for the full list) |
| `ref_current_price` | Latest Chainlink tick, USD |
| `ref_price_to_beat` | Window's opening Chainlink tick, USD (empty if partial) |
| `ref_distance_usd`, `ref_distance_bps` | `current - PTB` in USD and basis points |
| `ref_side` | `up` / `down` / `flat` derived from the distance sign |
| `ref_window_slug`, `ref_window_partial` | Feed-derived window + partial flag |
| `ref_age_ms` | Wall-clock age of the last live tick at decision time |
| `position_id`, `dry_run` | Populated only on `enter` / `accept_but_failed` |

This schema is designed for post-mortem calibration of
`SNIPE_MIN_REF_DISTANCE_USD`: pull the CSV into pandas, filter to
`decision == "reject"` AND `reason == "ref_too_close"`, and study the
distribution of `ref_distance_usd` at `seconds_remaining < 10` against
the eventual window outcome (from `windows_*.csv`).  Tune down the
minimum distance once you have a few hundred data points.

### `positions.json`
Durable list of every `SnipePosition` we've ever created (live or dry).
The `run` command rehydrates this on startup to restore:
- Daily spend counter (summed over today's fills)
- Open-position count (for the 3-open cap)
- Per-window attempt counter (so a restart doesn't re-enter a window we
  already touched in the last hour)

---

## 10. Deployment (Railway)

- **Entrypoint**: `CMD ["python", "-m", "snipe.main", "run", "--yes"]` in
  `Dockerfile`.
- **Default env**: `SNIPE_DRY_RUN=true` and `SNIPE_ENABLE_LIVE=false` are
  baked into the Dockerfile as fail-closed defaults.  Railway env vars
  override these.
- **To run the old arb bot instead**: set a custom start command in the
  Railway service settings → `python main.py monitor`.
- **Log shipping lag**: Railway's web log viewer trails real-time by
  roughly 1–3s (sometimes more under load).  This is purely display; the
  bot's internal decision latency is the poll interval + REST round trip
  (~300–500ms in tail).  Use the ticks/signals CSVs for accurate timing.

---

## 11. Current parameter state (as of 2026-04-20)

| Parameter | Value | Rationale |
|---|---|---|
| Entry time band | 3.0s – 15.0s remaining | Wider than the initial 3–5s; needs empirical tightening |
| Entry price band | 0.95 – 0.99 | Below 0.95 is a coin flip, 0.99+ is no spread to capture |
| Tail cadence | 0.3s from 45s out | Matched against observed poll-to-poll ~0.31s cadence |
| Position size | $5 | Small enough that fees dominate and we can trial-and-error |
| Per-window cap | 1 | Single shot per window while we calibrate |
| Daily cap | $50 | ~10 positions/day worst case |
| Open-position cap | 3 | Hedge against settlement backlog |

**Known open question**: In the single window inspected post-launch
(01:55–02:00 UTC on 2026-04-21), the market only resolved to a clear
favorite at t ≈ 2.5s — below the 3.0s floor.  If that pattern is common,
`SNIPE_MIN_SECONDS_REMAINING` may need to drop to 1.5 or 2.0.  Run the
monitor for a few hours and grep the `windows_*.csv` output for the
distribution of "time-to-settle" before making that change.

---

## 12. Safety rails (summary)

1. **Fail-closed arming**: `SNIPE_DRY_RUN=true` AND `SNIPE_ENABLE_LIVE=false`
   are the Dockerfile defaults.  Live mode requires **both** env vars set.
2. **FAK-only orders**: we cannot accidentally leave a resting limit order
   on Polymarket.
3. **Chainlink reference-price gate**: entries are refused when BTC is
   within `SNIPE_MIN_REF_DISTANCE_USD` of the window's Price to Beat,
   when the feed is stale or partial, or when the book leader disagrees
   with the oracle's implied side.  See section 6 and the dedicated
   config section.  The gate is ON by default (`SNIPE_REQUIRE_REF_FEED=true`).
4. **Per-window cap (default 1)**: even if the scanner fires repeatedly,
   we can't double up on a single window.
5. **Daily spend cap**: caps blast radius of any mis-tuned session.
6. **Restart safety**: attempt counters persist across process restarts
   via `positions.json` so Railway redeploys mid-window don't re-enter.
7. **Settler grace period**: we never ask Gamma to resolve a market until
   90s past its close, to avoid spamming for in-limbo markets.
8. **Balance preflight**: live mode refuses to start if pUSD is unreadable
   or below `SNIPE_MIN_POLY_BALANCE_USD`.

---

## 13. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Railway deploys the arb bot instead of snipe | Custom start command in Railway settings overrides Dockerfile CMD | Clear "Custom Start Command" or set it to `python -m snipe.main run --yes` |
| Bot crashes with `kalshi balance $0.00 < $25.00` | Old arb bot running, not snipe | See above — this is the arb bot's preflight, not ours |
| `probe` prints "Configured slug did not resolve" | Polymarket changed slug format | Set `SNIPE_POLY_SLUG_PATTERN` in env to match what `probe`'s fallback search returns |
| "no_leader" or "thin_book" rejects every tick | Market's being polled before it picks up liquidity | Normal at window start; check that rejects clear by t ≈ 60s |
| No entries even in clear-favorite windows | Settlement happened outside entry time band | Inspect `signals_*.csv` for the `reason` column; likely `time_out_of_band` — consider lowering `SNIPE_MIN_SECONDS_REMAINING` |
| Positions stay `open` forever | Settler grace window not met, or Gamma filter mismatch | `python -m snipe.main settle` for a one-shot sweep; check settler.py filter combo |
| Railway log display lags 5–10s | Railway log shipping, not bot | Ignore for decision-making; use CSVs for timing analysis |

---

## 14. Where to go next

1. Let dry-run accumulate a few hours of data on Railway.
2. Pull the `btc5m_snipe_ticks_*.csv` and `btc5m_snipe_windows_*.csv` files
   off the container for analysis.
3. Fit the `(time_to_settle, final_leader_price)` distribution.  Tune
   `SNIPE_MIN_SECONDS_REMAINING`, `SNIPE_MAX_SECONDS_REMAINING`,
   `SNIPE_MIN_ENTRY_PRICE`, and `SNIPE_MIN_TOP_OF_BOOK_SIZE` accordingly.
4. Arm live with `SNIPE_POSITION_USD=5` and the new parameters.  Watch
   `submit_latency_ms` in the signals CSV — that's the live-viability number.
5. Iterate.  Scale `SNIPE_POSITION_USD` once the strategy is net-positive
   over at least a few hundred live fills.
