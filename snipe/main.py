"""
CLI entry point for the snipe strategy.

Subcommands:

    probe         Verify the Polymarket slug pattern for the current window.
    status        Print effective configuration and arming state.
    monitor       Run the read-only research logger.
    run           Run the live scanner + executor loop (dry-run by default).
    preflight     Check USDC balance and live-arming state.
    positions     List open + recent settled positions.
    settle        Query Gamma to resolve outstanding positions.

Examples:

    python -m snipe.main probe
    python -m snipe.main monitor --duration 60
    python -m snipe.main run --duration 120
    python -m snipe.main positions
    python -m snipe.main settle
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp

from polymarket_client import PolymarketClient

from . import config, positions as positions_mod
from .executor import execute_entry
from .loop import LoopContext, run_window_loop
from .monitor import (
    make_new_window_announcer,
    make_tick_csv_handler,
    make_tty_handler,
    make_window_csv_handler,
    setup_csv_writers,
)
from .positions import SnipePosition
from .reference_price import ReferencePriceFeed, ReferenceSnapshot
from .scanner import SessionState, evaluate_tick
from .settler import settle_open_positions
from .window import (
    build_slug,
    current_window_boundaries,
    resolve_window,
    search_btc_markets,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)


def _reconfigure_stdio() -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass


def out(msg: str = "") -> None:
    print(msg, flush=True)


# ── probe ───────────────────────────────────────────────────────────────


async def _cmd_probe(args: argparse.Namespace) -> int:
    start, end = current_window_boundaries()
    out(f"  Current window (UTC):  {start.isoformat()} -> {end.isoformat()}")
    out(f"  Window size:           {config.SNIPE_WINDOW_MINUTES} min")
    out(f"  Configured slug:       {build_slug(start)}")
    out()

    async with aiohttp.ClientSession() as session:
        window = await resolve_window(session, start, end)
        if window.has_tokens():
            out("  [ok] Slug resolves. Market details:")
            out(f"       question:      {window.question}")
            out(f"       condition_id:  {window.condition_id}")
            out(f"       up_token:      {window.up_token}")
            out(f"       down_token:    {window.down_token}")
            out(f"       end_date_iso:  {window.end_date_iso}")
            return 0

        out("  [warn] Configured slug did not resolve.")
        out("  Attempting Gamma search fallback...")
        out()

        hits = await search_btc_markets(session, limit=args.limit)
        if not hits:
            out("  No BTC markets returned from Gamma search.")
            return 1

        hits.sort(key=lambda m: str(m.get("endDate") or ""))
        for mkt in hits[: args.limit]:
            slug = mkt.get("slug", "")
            question = mkt.get("question", "")
            end_date = mkt.get("endDate", "")
            out(f"  - {slug}")
            if question:
                out(f"      question: {question}")
            if end_date:
                out(f"      ends:     {end_date}")
        return 2


# ── status ──────────────────────────────────────────────────────────────


async def _cmd_status(args: argparse.Namespace) -> int:
    start, end = current_window_boundaries()
    summary = {
        "window_minutes": config.SNIPE_WINDOW_MINUTES,
        "slug_pattern": config.SNIPE_POLY_SLUG_PATTERN,
        "current_window_utc": f"{start.isoformat()} -> {end.isoformat()}",
        "current_slug": build_slug(start),
        "poll_interval_s": config.SNIPE_POLL_INTERVAL_S,
        "poll_interval_tail_s": config.SNIPE_POLL_INTERVAL_TAIL_S,
        "tail_window_s": config.SNIPE_TAIL_WINDOW_S,
        "entry_window_s": (config.SNIPE_MIN_SECONDS_REMAINING, config.SNIPE_MAX_SECONDS_REMAINING),
        "entry_price_band": (config.SNIPE_MIN_ENTRY_PRICE, config.SNIPE_MAX_ENTRY_PRICE),
        "min_top_of_book_size": config.SNIPE_MIN_TOP_OF_BOOK_SIZE,
        "position_usd": config.SNIPE_POSITION_USD,
        "max_entries_per_window": config.SNIPE_MAX_ENTRIES_PER_WINDOW,
        "max_spend_per_day_usd": config.SNIPE_MAX_SPEND_PER_DAY_USD,
        "max_open_positions": config.SNIPE_MAX_OPEN_POSITIONS,
        "ref_min_distance_usd": config.SNIPE_MIN_REF_DISTANCE_USD,
        "ref_stale_s": config.SNIPE_REF_STALE_S,
        "ref_require_directional_agreement": config.SNIPE_REF_REQUIRE_DIRECTIONAL_AGREEMENT,
        "ref_required": config.SNIPE_REQUIRE_REF_FEED,
        "presubmit_min_ask_price": config.SNIPE_PRESUBMIT_MIN_ASK_PRICE,
        "dry_run": config.SNIPE_DRY_RUN,
        "enable_live": config.SNIPE_ENABLE_LIVE,
        "live_armed": config.snipe_live_mode_requested(),
        "data_dir": config.SNIPE_DATA_DIR,
        "now_utc": datetime.now(timezone.utc).isoformat(),
    }
    out(json.dumps(summary, indent=2, default=str))
    if config.snipe_live_mode_requested():
        out()
        out("  >>> LIVE MODE ARMED -- orders will be submitted to Polymarket <<<")
    return 0


# ── monitor ─────────────────────────────────────────────────────────────


async def _cmd_monitor(args: argparse.Namespace) -> int:
    from .monitor import monitor as run_monitor
    await run_monitor(duration_minutes=args.duration)
    return 0


# ── preflight ───────────────────────────────────────────────────────────


async def _cmd_preflight(args: argparse.Namespace) -> int:
    """
    Report balance + arming state.  This does NOT trigger the on-chain
    USDC allowance approval on its own -- the Polymarket SDK handles that
    transparently on first order.  The point of preflight is to catch
    misconfiguration (missing key, insufficient USDC, wrong env) before
    we ever get near a live order submission.
    """
    out("  Preflight checks")
    out("  " + "=" * 56)
    out(f"  Live requested:   {config.snipe_live_mode_requested()}")
    out(f"  Dry run:          {config.SNIPE_DRY_RUN}")
    out(f"  Enable live:      {config.SNIPE_ENABLE_LIVE}")
    out()

    out("  Initializing Polymarket client...")
    try:
        poly = PolymarketClient()
    except Exception as e:
        out(f"  [FAIL] PolymarketClient init: {e}")
        return 1
    out("  [ok] client initialized.")

    try:
        usdc = poly.get_usdc_balance()
    except Exception as e:
        out(f"  [FAIL] get_usdc_balance: {e}")
        return 1
    if usdc is None:
        out("  [FAIL] USDC balance returned None")
        return 1

    out(f"  [ok] USDC balance: ${usdc:.4f}")

    ok = True
    min_bal = config.SNIPE_MIN_POLY_BALANCE_USD
    if config.SNIPE_REQUIRE_BALANCE_CHECK and usdc < min_bal:
        out(f"  [FAIL] balance below SNIPE_MIN_POLY_BALANCE_USD (${min_bal:.2f})")
        ok = False

    if config.snipe_live_mode_requested():
        out()
        out("  >>> LIVE MODE ARMED <<<")
    else:
        out()
        out("  (dry-run mode: no orders will be submitted during run)")

    return 0 if ok else 1


# ── run ─────────────────────────────────────────────────────────────────


TRADE_SIGNAL_FIELDS = [
    "ts_iso",
    "window_slug",
    "seconds_remaining",
    "leader_side",
    "leader_mid",
    "leader_ask",
    "leader_ask_size",
    "decision",
    "reason",
    "ref_current_price",
    "ref_price_to_beat",
    "ref_distance_usd",
    "ref_distance_bps",
    "ref_side",
    "ref_window_slug",
    "ref_window_partial",
    "ref_age_ms",
    "position_id",
    "dry_run",
]


def _append_signal_row(path: Path, row: dict) -> None:
    exists = path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_SIGNAL_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow(row)


def _ref_row_fields(ref: Optional[ReferenceSnapshot], at: datetime) -> dict:
    """Flatten a reference snapshot into the signal-row schema.

    Always returns every ``ref_*`` key so CSV writer sees a full dict.
    Missing values are empty strings so downstream `pandas.read_csv`
    behaves (empty -> NaN) rather than mixing `None` and float in the
    same column.
    """
    if ref is None:
        return {
            "ref_current_price": "",
            "ref_price_to_beat": "",
            "ref_distance_usd": "",
            "ref_distance_bps": "",
            "ref_side": "",
            "ref_window_slug": "",
            "ref_window_partial": "",
            "ref_age_ms": "",
        }
    d_usd = ref.distance_usd()
    d_bps = ref.distance_bps()
    age_ms = ""
    if ref.last_tick_recv_utc is not None:
        age_ms = f"{(at - ref.last_tick_recv_utc).total_seconds() * 1000.0:.0f}"
    return {
        "ref_current_price": "" if ref.current_price is None else f"{ref.current_price:.2f}",
        "ref_price_to_beat": "" if ref.price_to_beat is None else f"{ref.price_to_beat:.2f}",
        "ref_distance_usd": "" if d_usd is None else f"{d_usd:+.2f}",
        "ref_distance_bps": "" if d_bps is None else f"{d_bps:+.3f}",
        "ref_side": ref.implied_side() or "",
        "ref_window_slug": ref.window_slug or "",
        "ref_window_partial": "" if ref.window_slug == "" else str(bool(ref.window_partial)).lower(),
        "ref_age_ms": age_ms,
    }


def _signal_row_from(
    ctx: LoopContext,
    decision_label: str,
    reason: str,
    position: Optional[SnipePosition],
    ref: Optional[ReferenceSnapshot] = None,
) -> dict:
    t = ctx.tick
    base = {
        "ts_iso": t.ts_utc.isoformat(),
        "window_slug": t.window_slug,
        "seconds_remaining": f"{t.seconds_remaining:.2f}",
        "leader_side": t.leader_side or "",
        "leader_mid": "" if t.leader_mid is None else f"{t.leader_mid:.4f}",
        "leader_ask": "" if t.leader_ask is None else f"{t.leader_ask:.4f}",
        "leader_ask_size": "" if t.leader_ask_size is None else f"{t.leader_ask_size:.2f}",
        "decision": decision_label,
        "reason": reason,
        "position_id": position.id if position else "",
        "dry_run": str(position.dry_run).lower() if position else "",
    }
    base.update(_ref_row_fields(ref, t.ts_utc))
    return base


def make_scanner_handler(
    session_state: SessionState,
    dry_run: bool,
    signal_csv: Path,
    ref_feed: Optional[ReferencePriceFeed] = None,
):
    """
    Tick handler that consults the scanner and invokes the executor on
    accepted entries.  All signals (accepted and rejected) are logged to
    ``signal_csv`` so we can post-mortem the rejection distribution.

    If ``ref_feed`` is provided, its latest snapshot is passed into
    ``evaluate_tick`` so the scanner can apply the Chainlink distance
    gate.  The same snapshot is recorded verbatim on every logged signal
    row for offline calibration.
    """
    async def _handler(ctx: LoopContext) -> None:
        ref_snap = ref_feed.snapshot() if ref_feed is not None else None
        decision = evaluate_tick(ctx.tick, ctx.window, session_state, ref=ref_snap)

        if not decision.should_enter:
            # Only log "interesting" rejects to keep the file small.  A
            # reject is interesting if the leader is in the entry price
            # band OR inside the entry time band -- i.e. the tick was at
            # least in the neighborhood of a decision.
            t = ctx.tick
            near_price = (
                t.leader_ask is not None
                and config.SNIPE_MIN_ENTRY_PRICE <= t.leader_ask <= config.SNIPE_MAX_ENTRY_PRICE
            )
            near_time = (
                config.SNIPE_MIN_SECONDS_REMAINING
                <= t.seconds_remaining
                <= config.SNIPE_MAX_SECONDS_REMAINING
            )
            if near_price or near_time:
                _append_signal_row(
                    signal_csv,
                    _signal_row_from(ctx, "reject", decision.reason, None, ref=ref_snap),
                )
            return

        # Accepted.  Hot path starts now.  Register the attempt BEFORE
        # invoking the executor -- if the SDK call stalls (first-call
        # warmup, network flap, etc.) we must not re-enter this same
        # window on the next tick.  The next 5-minute window gets its
        # own slug and therefore its own fresh attempt counter.
        session_state.register_attempt(ctx.window.slug)

        position = execute_entry(
            poly=ctx.poly,
            decision=decision,
            window=ctx.window,
            tick=ctx.tick,
            dry_run=dry_run,
        )

        session_state.open_positions_count = len(positions_mod.open_positions())
        if position.status == positions_mod.STATUS_ENTRY_FAILED:
            if not position.extra.get("consume_window_slot", True):
                session_state.release_attempt(ctx.window.slug)
            _append_signal_row(
                signal_csv,
                _signal_row_from(
                    ctx, "accept_but_failed",
                    position.submit_error or "", position, ref=ref_snap,
                ),
            )
            latency = position.submit_latency_ms or 0.0
            retry_note = (
                "window slot released; may retry this window"
                if not position.extra.get("consume_window_slot", True)
                else "window slot consumed"
            )
            confirm_note = (
                f" confirmed_no_fill={position.extra.get('confirmed_no_fill')}"
                if "confirmed_no_fill" in position.extra
                else ""
            )
            if position.extra.get("failure_kind") == "pre_submit_guard":
                out(
                    f"  -- entry SKIPPED for {ctx.tick.window_slug} "
                    f"({position.submit_error}) -- {retry_note}"
                )
            else:
                out(f"  !! entry FAILED for {ctx.tick.window_slug} "
                    f"({position.submit_error}) after {latency:.0f}ms -- "
                    f"{retry_note}{confirm_note}")
            return

        session_state.register_fill(position.entry_cost_usd or 0.0)
        ctx.acc.entries_submitted += 1
        _append_signal_row(
            signal_csv,
            _signal_row_from(ctx, "enter", decision.reason, position, ref=ref_snap),
        )

        tag = "DRY" if dry_run else "LIVE"
        if position.avg_fill_price is not None:
            fill_note = (
                f"requested={position.requested_size:.2f} @ {position.requested_price:.4f}  "
                f"actual={position.filled_size or 0:.2f} @ {position.avg_fill_price:.4f}"
            )
        else:
            fill_note = (
                f"requested={position.requested_size:.2f} @ {position.requested_price:.4f}  "
                "actual=no_fill_reported"
            )
        out(
            f"  >> [{tag}] {ctx.tick.leader_side.upper()} "
            f"t-{ctx.tick.seconds_remaining:.1f}s  {fill_note}  "
            f"({position.submit_latency_ms or 0:.0f}ms)"
        )
    _handler.__name__ = "scanner"
    return _handler


SETTLER_TICK_INTERVAL_S = 30.0


def make_settler_handler():
    """
    Tick handler that runs the settlement resolver roughly every
    ``SETTLER_TICK_INTERVAL_S`` wall-clock seconds.  The resolver itself
    applies per-position backoff so chatty unresolved windows do not blow
    up Gamma request volume; this interval just governs how often the
    handler *wakes up* to consult the resolver.
    """
    last_run = {"at": 0.0}

    async def _handler(ctx: LoopContext) -> None:
        now = time.time()
        if now - last_run["at"] < SETTLER_TICK_INTERVAL_S:
            return
        last_run["at"] = now
        try:
            async with aiohttp.ClientSession() as session:
                summary = await settle_open_positions(session=session)
        except Exception:
            logging.getLogger("snipe.settler").exception("settle_open_positions failed")
            return
        if summary.get("settled", 0) > 0:
            out(f"  .. settled {summary['settled']} positions "
                f"({summary['pending']} pending, {summary['errors']} errors)")
    _handler.__name__ = "settler"
    return _handler


async def _cmd_run(args: argparse.Namespace) -> int:
    dry_run = not config.snipe_live_mode_requested()

    out("  BTC 5-Minute Snipe Runner")
    out("  " + "=" * 56)
    out(f"  Mode:             {'DRY-RUN' if dry_run else 'LIVE'}")
    out(f"  Entry window:     {config.SNIPE_MIN_SECONDS_REMAINING:.1f}s - "
        f"{config.SNIPE_MAX_SECONDS_REMAINING:.1f}s remaining")
    out(f"  Price band:       {config.SNIPE_MIN_ENTRY_PRICE} - {config.SNIPE_MAX_ENTRY_PRICE}")
    out(f"  Position size:    ${config.SNIPE_POSITION_USD}")
    out(f"  Per-window cap:   {config.SNIPE_MAX_ENTRIES_PER_WINDOW}")
    out(f"  Daily spend cap:  ${config.SNIPE_MAX_SPEND_PER_DAY_USD}")
    out()

    out("  Initializing Polymarket client...")
    poly = PolymarketClient()
    out("  Ready.")

    if not dry_run:
        out()
        out("  Running live preflight...")
        try:
            usdc = poly.get_usdc_balance()
        except Exception as e:
            out(f"  [FAIL] preflight USDC read: {e}")
            return 2
        if usdc is None or (
            config.SNIPE_REQUIRE_BALANCE_CHECK
            and usdc < config.SNIPE_MIN_POLY_BALANCE_USD
        ):
            out(f"  [FAIL] USDC ${usdc} below SNIPE_MIN_POLY_BALANCE_USD "
                f"(${config.SNIPE_MIN_POLY_BALANCE_USD:.2f})")
            return 2
        out(f"  [ok] USDC ${usdc:.2f}")
        out("  Warming live order path...")
        try:
            warm = poly.warm_up_live_trading()
            errors = warm.get("errors") or []
            if errors:
                out(f"  [warn] live warmup partial: {'; '.join(str(e) for e in errors)}")
            else:
                out("  [ok] live warmup complete")
        except Exception as e:
            out(f"  [warn] live warmup failed: {e}")
        out()
        out("  >>> LIVE MODE ARMED -- orders WILL be submitted to Polymarket <<<")
        if not args.yes:
            out("  Press Ctrl-C within 5s to abort...")
            try:
                await asyncio.sleep(5)
            except KeyboardInterrupt:
                out("  aborted.")
                return 1

    data_dir = Path(config.SNIPE_DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ticks_path, windows_path = setup_csv_writers(session_ts=session_ts)
    signal_csv = data_dir / f"btc5m_snipe_signals_{session_ts}.csv"

    out(f"  Tick log:      {ticks_path}")
    out(f"  Window log:    {windows_path}")
    out(f"  Signal log:    {signal_csv}")
    if args.duration is not None:
        out(f"  Duration:      {args.duration:.1f} min")
    out()

    session_state = SessionState()
    session_state.total_spend_today_usd = positions_mod.spend_today_usd()
    session_state.open_positions_count = len(positions_mod.open_positions())
    # Restore the per-window attempt counter so that a process restart
    # mid-window (Railway deploy, crash+recover, manual restart) does not
    # re-enter a window we've already touched.  We look back 1 hour --
    # that safely covers the in-flight window plus any recently closed
    # ones whose positions may still be unsettled.
    from datetime import timedelta as _td
    cutoff = (datetime.now(timezone.utc) - _td(hours=1)).isoformat()
    session_state.entries_by_window = positions_mod.attempts_by_window_since(cutoff)
    if session_state.entries_by_window:
        out(f"  Restored attempt counters for {len(session_state.entries_by_window)} "
            f"recent window(s) from positions.json")

    # Spin up the Chainlink reference-price feed.  Every scanner tick
    # reads its latest snapshot; entries are refused when the snapshot
    # is missing, stale, partial, or too close to the threshold.  See
    # snipe/reference_price.py and the "Reference-price gate" section
    # of snipe/README.md.
    ref_feed = ReferencePriceFeed(
        on_event=lambda ev, detail: out(f"  [ref] {ev} {detail if detail is not None else ''}"),
    )
    ref_feed.start()
    out("  [ref] Chainlink RTDS feed starting (required for entries)")
    out(f"         min distance: ${config.SNIPE_MIN_REF_DISTANCE_USD:.2f}  "
        f"stale cutoff: {config.SNIPE_REF_STALE_S:.1f}s  "
        f"directional: {config.SNIPE_REF_REQUIRE_DIRECTIONAL_AGREEMENT}")
    out()

    try:
        await run_window_loop(
            poly,
            on_tick=[
                make_scanner_handler(session_state, dry_run, signal_csv, ref_feed=ref_feed),
                make_tick_csv_handler(ticks_path),
                make_tty_handler(),
                make_settler_handler(),
            ],
            on_window_end=[
                make_window_csv_handler(windows_path),
            ],
            on_new_window=make_new_window_announcer(),
            duration_minutes=args.duration,
        )
    finally:
        out()
        out("  [ref] stopping Chainlink feed...")
        await ref_feed.stop()

    out()
    out("  Final settlement sweep...")
    summary = await settle_open_positions(verbose=True)
    out(f"  Settled on shutdown: {summary['settled']} (pending {summary['pending']}, errors {summary['errors']})")
    out()
    out(f"  Ticks:    {ticks_path}")
    out(f"  Windows:  {windows_path}")
    out(f"  Signals:  {signal_csv}")
    return 0


# ── positions ──────────────────────────────────────────────────────────


async def _cmd_positions(args: argparse.Namespace) -> int:
    positions = positions_mod.load_positions()
    if not positions:
        out("  (no positions on file)")
        return 0

    positions.sort(key=lambda p: p.submitted_at_utc or "")
    if args.last:
        positions = positions[-args.last :]

    wins = [p for p in positions if p.status == positions_mod.STATUS_SETTLED_WIN]
    losses = [p for p in positions if p.status == positions_mod.STATUS_SETTLED_LOSS]
    open_rows = [p for p in positions if p.status == positions_mod.STATUS_OPEN]
    failed = [p for p in positions if p.status == positions_mod.STATUS_ENTRY_FAILED]

    total_pnl = sum((p.realized_pnl_usd or 0.0) for p in positions)
    settled = len(wins) + len(losses)
    hit_rate = (len(wins) / settled) if settled else 0.0

    out("  Snipe positions")
    out("  " + "=" * 56)
    out(f"  Total:    {len(positions)} "
        f"(open {len(open_rows)}, settled {settled}, failed {len(failed)})")
    if settled:
        out(f"  Wins:     {len(wins)}")
        out(f"  Losses:   {len(losses)}")
        out(f"  Hit rate: {hit_rate*100:.1f}%")
        out(f"  Net P&L:  ${total_pnl:.4f}")
    out()

    out(f"  {'when':<20}  {'window':<30}  {'side':<4}  "
        f"{'size':>6}  {'fill':>6}  {'status':<14}  {'pnl':>7}")
    out("  " + "-" * 100)
    for p in positions:
        when = (p.submitted_at_utc or "")[:19]
        fill = (
            f"{p.avg_fill_price:.3f}" if p.avg_fill_price is not None else "-"
        )
        size = f"{p.filled_size:.2f}" if p.filled_size else f"{p.requested_size:.2f}"
        pnl = (
            f"{p.realized_pnl_usd:+.4f}" if p.realized_pnl_usd is not None else ""
        )
        tag = p.status + (" (dry)" if p.dry_run else "")
        out(
            f"  {when:<20}  {p.window_slug:<30}  {p.side:<4}  "
            f"{size:>6}  {fill:>6}  {tag:<14}  {pnl:>7}"
        )
    return 0


# ── settle ──────────────────────────────────────────────────────────────


async def _cmd_settle(args: argparse.Namespace) -> int:
    summary = await settle_open_positions(verbose=True)
    out(json.dumps(summary, indent=2))
    return 0


# ── dispatcher ──────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m snipe.main",
        description="BTC 5-minute snipe strategy CLI",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    probe = sub.add_parser("probe", help="Verify the Polymarket slug pattern")
    probe.add_argument("--limit", type=int, default=10)

    monitor = sub.add_parser("monitor", help="Read-only research logger")
    monitor.add_argument("--duration", type=float, default=None)

    run_cmd = sub.add_parser("run", help="Scanner + executor loop (dry-run by default)")
    run_cmd.add_argument("--duration", type=float, default=None)
    run_cmd.add_argument(
        "--yes",
        action="store_true",
        help="Skip the 5s live-arming confirmation pause",
    )

    sub.add_parser("status", help="Print effective config + arming state")
    sub.add_parser("preflight", help="Run balance + arming checks and exit")
    pos = sub.add_parser("positions", help="List open + recent positions")
    pos.add_argument("--last", type=int, default=50, help="Show only the last N (default: 50)")
    sub.add_parser("settle", help="Resolve open positions against Gamma")

    return p


def main() -> None:
    _reconfigure_stdio()
    parser = _build_parser()
    args = parser.parse_args()

    handlers = {
        "probe": _cmd_probe,
        "status": _cmd_status,
        "monitor": _cmd_monitor,
        "run": _cmd_run,
        "preflight": _cmd_preflight,
        "positions": _cmd_positions,
        "settle": _cmd_settle,
    }
    handler = handlers.get(args.cmd)
    if handler is None:
        parser.print_help()
        sys.exit(2)

    rc = asyncio.run(handler(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
