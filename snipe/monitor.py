"""
Research monitor for the BTC 5-minute snipe strategy.

Runs ``run_window_loop`` from :mod:`snipe.loop` with a read-only set of
handlers that log tick-level book state and per-window summaries to CSV.
No orders are ever submitted from this module.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from polymarket_client import PolymarketClient

from . import config
from .loop import (
    LoopContext,
    SNAPSHOT_THRESHOLDS_S,
    Tick,
    WindowAccumulator,
    run_window_loop,
)
from .window import Window

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("snipe.monitor")


TICK_FIELDS = [
    "ts_iso",
    "window_slug",
    "window_start_utc",
    "window_end_utc",
    "seconds_remaining",
    "elapsed_s",
    "up_bid", "up_bid_size", "up_ask", "up_ask_size", "up_mid",
    "down_bid", "down_bid_size", "down_ask", "down_ask_size", "down_mid",
    "total_mid",
    "leader_side",
    "leader_mid",
    "leader_ask",
]


WINDOW_FIELDS = [
    "window_slug",
    "window_start_utc",
    "window_end_utc",
    "condition_id",
    "final_up_mid",
    "final_down_mid",
    "final_leader_side",
    "tick_count",
    "entries_submitted",
] + [
    f"{thr}s_{col}"
    for thr in SNAPSHOT_THRESHOLDS_S
    for col in ("leader_side", "leader_mid", "leader_ask", "up_mid", "down_mid")
]


def out(msg: str = "") -> None:
    print(msg, flush=True)


def _fmt(v) -> str:
    if v is None:
        return ""
    try:
        return f"{float(v):.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return ""


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def tick_row(t: Tick) -> dict:
    return {
        "ts_iso": t.ts_utc.isoformat(),
        "window_slug": t.window_slug,
        "window_start_utc": t.window_start_utc.isoformat(),
        "window_end_utc": t.window_end_utc.isoformat(),
        "seconds_remaining": f"{t.seconds_remaining:.2f}",
        "elapsed_s": f"{t.elapsed_s:.2f}",
        "up_bid": _fmt(t.up.get("bid")),
        "up_bid_size": _fmt(t.up.get("bid_size")),
        "up_ask": _fmt(t.up.get("ask")),
        "up_ask_size": _fmt(t.up.get("ask_size")),
        "up_mid": _fmt(t.up.get("mid")),
        "down_bid": _fmt(t.down.get("bid")),
        "down_bid_size": _fmt(t.down.get("bid_size")),
        "down_ask": _fmt(t.down.get("ask")),
        "down_ask_size": _fmt(t.down.get("ask_size")),
        "down_mid": _fmt(t.down.get("mid")),
        "total_mid": _fmt(t.total_mid),
        "leader_side": t.leader_side or "",
        "leader_mid": _fmt(t.leader_mid),
        "leader_ask": _fmt(t.leader_ask),
    }


def window_summary_row(acc: WindowAccumulator) -> dict:
    row = {
        "window_slug": acc.window.slug,
        "window_start_utc": acc.window.start.isoformat(),
        "window_end_utc": acc.window.end.isoformat(),
        "condition_id": acc.window.condition_id or "",
        "final_up_mid": "",
        "final_down_mid": "",
        "final_leader_side": "",
        "tick_count": acc.tick_count,
        "entries_submitted": acc.entries_submitted,
    }
    if acc.last_tick is not None:
        row["final_up_mid"] = _fmt(acc.last_tick.up.get("mid"))
        row["final_down_mid"] = _fmt(acc.last_tick.down.get("mid"))
        row["final_leader_side"] = acc.last_tick.leader_side or ""

    for thr in SNAPSHOT_THRESHOLDS_S:
        snap = acc.snapshots.get(thr)
        prefix = f"{thr}s_"
        if snap is None:
            row[prefix + "leader_side"] = ""
            row[prefix + "leader_mid"] = ""
            row[prefix + "leader_ask"] = ""
            row[prefix + "up_mid"] = ""
            row[prefix + "down_mid"] = ""
            continue
        row[prefix + "leader_side"] = snap.leader_side or ""
        row[prefix + "leader_mid"] = _fmt(snap.leader_mid)
        row[prefix + "leader_ask"] = _fmt(snap.leader_ask)
        row[prefix + "up_mid"] = _fmt(snap.up.get("mid"))
        row[prefix + "down_mid"] = _fmt(snap.down.get("mid"))
    return row


def setup_csv_writers(
    session_ts: Optional[str] = None,
    filename_prefix: str = "btc5m_snipe",
) -> tuple[Path, Path]:
    """Create the tick and window CSV files (with headers) and return their paths."""
    data_dir = Path(config.SNIPE_DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)

    session_ts = session_ts or datetime.now().strftime("%Y%m%d_%H%M%S")
    ticks_path = data_dir / f"{filename_prefix}_ticks_{session_ts}.csv"
    windows_path = data_dir / f"{filename_prefix}_windows_{session_ts}.csv"

    with open(ticks_path, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=TICK_FIELDS).writeheader()
    with open(windows_path, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=WINDOW_FIELDS).writeheader()
    return ticks_path, windows_path


def make_tick_csv_handler(ticks_path: Path):
    """Return a TickHandler that appends one row per tick."""
    async def _handler(ctx: LoopContext) -> None:
        with open(ticks_path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=TICK_FIELDS).writerow(tick_row(ctx.tick))
    _handler.__name__ = "tick_csv"
    return _handler


def make_window_csv_handler(windows_path: Path):
    """Return a WindowEndHandler that appends one summary row per window."""
    async def _handler(acc: WindowAccumulator) -> None:
        with open(windows_path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=WINDOW_FIELDS).writerow(window_summary_row(acc))
    _handler.__name__ = "window_csv"
    return _handler


def make_tty_handler():
    """Return a TickHandler that pretty-prints a single line per tick."""
    async def _handler(ctx: LoopContext) -> None:
        t = ctx.tick

        def _c(v):
            return f"{float(v)*100:5.1f}" if v is not None else "  -  "

        def _sz(v):
            if v is None:
                return "    -"
            try:
                return f"{float(v):5.0f}"
            except (TypeError, ValueError):
                return "    -"

        marker = ""
        ask = t.leader_ask
        if (
            ask is not None
            and t.leader_side is not None
            and t.seconds_remaining <= config.SNIPE_MAX_SECONDS_REMAINING
            and t.seconds_remaining >= config.SNIPE_MIN_SECONDS_REMAINING
            and config.SNIPE_MIN_ENTRY_PRICE <= ask <= config.SNIPE_MAX_ENTRY_PRICE
        ):
            marker = f"  <-- candidate ({t.leader_side.upper()} @ {ask:.2f})"

        out(
            f"  {_ts()}  t-{t.seconds_remaining:5.1f}s  "
            f"UP {_c(t.up.get('bid'))}/{_c(t.up.get('ask'))} sz {_sz(t.up.get('ask_size'))}  "
            f"DN {_c(t.down.get('bid'))}/{_c(t.down.get('ask'))} sz {_sz(t.down.get('ask_size'))}  "
            f"tot {_c(t.total_mid)}{marker}"
        )
    _handler.__name__ = "tty"
    return _handler


def make_new_window_announcer():
    async def _handler(window: Window) -> None:
        out()
        out(f"  === New window: {window.slug} "
            f"({window.start.strftime('%H:%M')}-{window.end.strftime('%H:%M UTC')}) ===")
        if window.has_tokens():
            out(f"  UP token:   {window.up_token[:20]}...")
            out(f"  DOWN token: {window.down_token[:20]}...")
        else:
            out("  WARNING: Gamma has not listed this window yet -- will retry each poll")
    return _handler


async def monitor(duration_minutes: Optional[float] = None) -> None:
    out("  BTC 5-Minute Snipe Monitor (research mode, read-only)")
    out("  " + "=" * 56)
    out()
    out("  Initializing Polymarket client...")
    poly = PolymarketClient()
    out("  Ready.")
    out()

    ticks_path, windows_path = setup_csv_writers()

    out(f"  Window size:   {config.SNIPE_WINDOW_MINUTES} min")
    out(f"  Slug pattern:  {config.SNIPE_POLY_SLUG_PATTERN}")
    out(f"  Poll:          {config.SNIPE_POLL_INTERVAL_S}s normal / "
        f"{config.SNIPE_POLL_INTERVAL_TAIL_S}s in tail "
        f"(last {config.SNIPE_TAIL_WINDOW_S:.0f}s)")
    out(f"  Tick log:      {ticks_path}")
    out(f"  Window log:    {windows_path}")
    if duration_minutes is not None:
        out(f"  Duration:      {duration_minutes:.1f} min (then exit)")
    out()

    await run_window_loop(
        poly,
        on_tick=[
            make_tick_csv_handler(ticks_path),
            make_tty_handler(),
        ],
        on_window_end=[
            make_window_csv_handler(windows_path),
        ],
        on_new_window=make_new_window_announcer(),
        duration_minutes=duration_minutes,
    )

    out()
    out(f"  Ticks:   {ticks_path}")
    out(f"  Windows: {windows_path}")


def main() -> None:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="BTC 5-minute snipe research monitor")
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Exit after N minutes (default: run forever)",
    )
    args = parser.parse_args()
    asyncio.run(monitor(duration_minutes=args.duration))


if __name__ == "__main__":
    main()
