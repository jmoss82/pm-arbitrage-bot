"""
Local BTC 5-minute price tracker using Polymarket's Real-Time Data Socket.

Connects to ``wss://ws-live-data.polymarket.com`` and subscribes to the
``crypto_prices_chainlink`` topic for ``btc/usd``. This is the exact
Chainlink BTC/USD feed Polymarket displays on the UI as "Current Price"
and uses to derive "Price to Beat" (the first tick at/after each
5-minute window boundary).

The tracker records per-tick data to CSV so we can study distance-to-
threshold near window close and compare it against our live entries.
It does NOT place any orders and is not used by the live bot.

Examples:
    python track_btc_5m_price.py
    python track_btc_5m_price.py --duration 60
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed


RTDS_URL = "wss://ws-live-data.polymarket.com"
TOPIC = "crypto_prices_chainlink"
SYMBOL = "btc/usd"

WINDOW_MINUTES = int(os.getenv("SNIPE_WINDOW_MINUTES", "5"))
SLUG_PATTERN = os.getenv("SNIPE_POLY_SLUG_PATTERN", "btc-updown-5m-{ts}")

# RTDS requires periodic app-level text "ping" frames to keep the
# connection alive and live data flowing. The official TypeScript client
# sends lowercase "ping" every ~5s after each pong; we approximate with a
# simple 5s cadence.
APP_PING_INTERVAL_S = 5.0

# The server's ``filters`` parser is strict about whitespace: any space
# after the colon and live updates silently stop flowing (only the initial
# snapshot is delivered). Use the exact bytes the docs show.
CHAINLINK_BTC_FILTER = '{"symbol":"btc/usd"}'

# Reconnect backoff bounds.
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0

# A window is considered "partial" if we joined it more than this many
# milliseconds past the boundary. In that case we did not observe the
# true opening tick and ``price_to_beat`` is left null.
PARTIAL_WINDOW_SLACK_MS = 1500


FIELDS = [
    "ts_iso",              # wall-clock UTC when the row was written
    "tick_ts_ms",          # Chainlink tick timestamp (unix ms)
    "tick_age_ms",         # recv_ms - tick_ts_ms (network/oracle latency)
    "window_slug",
    "window_start_utc",
    "window_end_utc",
    "window_partial",      # True if we joined mid-window (price_to_beat is null)
    "seconds_remaining",
    "price_to_beat",
    "current_price",
    "distance_usd",        # current_price - price_to_beat
    "distance_bps",        # distance / price_to_beat * 10_000
    "side",                # up / down / flat
    "price_1s_delta",
    "price_3s_delta",
    "price_5s_delta",
    "cross_count_window",  # direction flips observed this window
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def window_boundaries_for(moment: datetime) -> tuple[datetime, datetime]:
    minute = (moment.minute // WINDOW_MINUTES) * WINDOW_MINUTES
    start = moment.replace(minute=minute, second=0, microsecond=0)
    end = start + timedelta(minutes=WINDOW_MINUTES)
    return start, end


def build_slug(window_start: datetime) -> str:
    return SLUG_PATTERN.format(ts=int(window_start.timestamp()))


def fmt(value: Optional[float], places: int = 6) -> str:
    if value is None:
        return ""
    return f"{value:.{places}f}"


@dataclass
class WindowState:
    slug: str
    start: datetime
    end: datetime
    price_to_beat: Optional[float] = None
    window_partial: bool = False
    last_side: Optional[str] = None
    cross_count: int = 0


def price_delta(history: list[tuple[float, float]], seconds: float) -> Optional[float]:
    if not history:
        return None
    target = history[-1][0] - seconds
    prior = None
    for ts, price in reversed(history):
        if ts <= target:
            prior = price
            break
    if prior is None:
        return None
    return history[-1][1] - prior


def output_path(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / f"btc5m_price_tracker_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"


async def app_ping_loop(ws, stop: asyncio.Event) -> None:
    """Send lowercase text "ping" frames on a fixed cadence.

    The official TS client sends "ping" on open and again on each pong.
    A simple timer is functionally equivalent for keeping the server
    feeding us live updates, and is simpler than wiring a pong handler.
    """
    while not stop.is_set():
        try:
            await ws.send("ping")
        except Exception:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=APP_PING_INTERVAL_S)
        except asyncio.TimeoutError:
            continue


async def consume(
    ws,
    out_path: Path,
    args: argparse.Namespace,
    deadline: Optional[float],
) -> None:
    state: Optional[WindowState] = None
    history: list[tuple[float, float]] = []

    async for raw in ws:
        if deadline is not None and time.time() >= deadline:
            return
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        if not raw:
            continue
        stripped = raw.strip()
        if stripped.lower() in ("pong", "ping", ""):
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue

        # The server labels the initial history batch as ``topic=crypto_prices``
        # with ``type=subscribe`` and a ``payload.data`` array. Live ticks come
        # back as ``topic=crypto_prices_chainlink`` with ``type=update`` and a
        # scalar ``payload.value``. We only care about live ticks here.
        if msg.get("topic") != TOPIC or msg.get("type") != "update":
            continue
        payload = msg.get("payload") or {}
        if str(payload.get("symbol", "")).lower() != SYMBOL:
            continue
        try:
            value = float(payload["value"])
        except (KeyError, TypeError, ValueError):
            continue

        tick_ms = int(payload.get("timestamp") or 0)
        recv_ms = int(time.time() * 1000)
        wall_now = now_utc()

        # Place the tick in a window by its own timestamp so network jitter
        # at the boundary cannot misclassify it.
        if tick_ms > 0:
            tick_dt = datetime.fromtimestamp(tick_ms / 1000.0, tz=timezone.utc)
        else:
            tick_dt = wall_now
        tick_start, tick_end = window_boundaries_for(tick_dt)
        tick_slug = build_slug(tick_start)

        if state is None or state.slug != tick_slug:
            # On script start the first tick may land mid-window; flag partial
            # and refuse to synthesize a fake opening reference.
            ms_into = int((tick_dt - tick_start).total_seconds() * 1000)
            if state is None:
                partial = ms_into > PARTIAL_WINDOW_SLACK_MS
            else:
                # True boundary crossing between consecutive windows: the
                # new tick is by definition the first observation of the
                # fresh window, so it IS the opening reference.
                partial = False
            state = WindowState(
                slug=tick_slug,
                start=tick_start,
                end=tick_end,
                window_partial=partial,
            )
            history = []
            marker = " (partial)" if partial else ""
            print(
                f"\n=== Window {tick_slug} "
                f"{tick_start.strftime('%H:%M')}-{tick_end.strftime('%H:%M UTC')}{marker}",
                flush=True,
            )

        if state.price_to_beat is None and not state.window_partial:
            state.price_to_beat = value
            print(f"    price-to-beat = {value:,.2f}", flush=True)

        history.append((time.time(), value))
        if len(history) > 2000:
            history = history[-2000:]

        distance: Optional[float] = None
        distance_bps: Optional[float] = None
        side: Optional[str] = None
        if state.price_to_beat is not None:
            distance = value - state.price_to_beat
            distance_bps = (distance / state.price_to_beat) * 10_000.0
            if distance > 0:
                side = "up"
            elif distance < 0:
                side = "down"
            else:
                side = "flat"
            if state.last_side and side != state.last_side and side != "flat":
                state.cross_count += 1
            if side != "flat":
                state.last_side = side

        # ``seconds_remaining`` reflects time until the current wall-clock
        # window closes, which is what matters for entry timing. For a tick
        # landing exactly at the boundary this equals ``tick_end - tick_dt``.
        wall_start, wall_end = window_boundaries_for(wall_now)
        seconds_remaining = max(0.0, (wall_end - wall_now).total_seconds())

        row = {
            "ts_iso": wall_now.isoformat(),
            "tick_ts_ms": tick_ms,
            "tick_age_ms": max(0, recv_ms - tick_ms) if tick_ms else "",
            "window_slug": state.slug,
            "window_start_utc": state.start.isoformat(),
            "window_end_utc": state.end.isoformat(),
            "window_partial": state.window_partial,
            "seconds_remaining": fmt(seconds_remaining, 3),
            "price_to_beat": fmt(state.price_to_beat, 2),
            "current_price": fmt(value, 2),
            "distance_usd": fmt(distance, 2),
            "distance_bps": fmt(distance_bps, 3),
            "side": side or "",
            "price_1s_delta": fmt(price_delta(history, 1.0), 2),
            "price_3s_delta": fmt(price_delta(history, 3.0), 2),
            "price_5s_delta": fmt(price_delta(history, 5.0), 2),
            "cross_count_window": state.cross_count,
        }
        with open(out_path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerow(row)

        if seconds_remaining <= args.print_tail_seconds:
            if state.price_to_beat is not None and distance is not None and distance_bps is not None:
                print(
                    f"{wall_now.strftime('%H:%M:%S')} t-{seconds_remaining:5.1f}s "
                    f"btc={value:,.2f} ptb={state.price_to_beat:,.2f} "
                    f"dist={distance:+.2f} ({distance_bps:+.2f}bps) side={side} "
                    f"crosses={state.cross_count}",
                    flush=True,
                )
            else:
                print(
                    f"{wall_now.strftime('%H:%M:%S')} t-{seconds_remaining:5.1f}s "
                    f"btc={value:,.2f} ptb=?? (partial window)",
                    flush=True,
                )


async def run_tracker(args: argparse.Namespace) -> Path:
    out_path = output_path(Path(args.data_dir))
    with open(out_path, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    print(f"Tracker writing: {out_path}", flush=True)
    print(
        f"Source: Polymarket RTDS {RTDS_URL} topic={TOPIC} symbol={SYMBOL}",
        flush=True,
    )

    started = time.time()
    deadline: Optional[float] = (
        started + args.duration * 60.0 if args.duration is not None else None
    )
    backoff = RECONNECT_MIN_S

    while True:
        if deadline is not None and time.time() >= deadline:
            break

        stop = asyncio.Event()
        try:
            async with websockets.connect(
                RTDS_URL,
                # Disable protocol-level pings; RTDS uses app-level PING text.
                ping_interval=None,
                close_timeout=2,
                max_size=2**22,
                open_timeout=10,
            ) as ws:
                subscription = {
                    "action": "subscribe",
                    "subscriptions": [
                        {
                            "topic": TOPIC,
                            "type": "*",
                            "filters": CHAINLINK_BTC_FILTER,
                        }
                    ],
                }
                await ws.send(json.dumps(subscription))
                print(f"[ws] subscribed {TOPIC} {SYMBOL}", flush=True)
                backoff = RECONNECT_MIN_S

                ping_task = asyncio.create_task(app_ping_loop(ws, stop))
                try:
                    await consume(ws, out_path, args, deadline)
                finally:
                    stop.set()
                    ping_task.cancel()
                    try:
                        await ping_task
                    except (asyncio.CancelledError, Exception):
                        pass
        except ConnectionClosed as e:
            print(
                f"[ws] closed ({e.code} {e.reason!s}); "
                f"reconnecting in {backoff:.1f}s",
                flush=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)
        except OSError as e:
            print(
                f"[ws] network error: {type(e).__name__}: {e}; "
                f"reconnecting in {backoff:.1f}s",
                flush=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)
        except Exception as e:
            print(
                f"[ws] unexpected: {type(e).__name__}: {e}; "
                f"reconnecting in {backoff:.1f}s",
                flush=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)

    return out_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Track Polymarket BTC Chainlink price across 5-minute windows."
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Run duration in minutes (default: run until Ctrl-C).",
    )
    parser.add_argument(
        "--data-dir",
        default="data/snipe_price",
        help="CSV output directory (default: data/snipe_price).",
    )
    parser.add_argument(
        "--print-tail-seconds",
        type=float,
        default=15.0,
        help="Only print rows to the terminal within this many seconds of window close.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run_tracker(args))
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)


if __name__ == "__main__":
    main()
