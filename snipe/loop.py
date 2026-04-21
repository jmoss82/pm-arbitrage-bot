"""
Shared window loop used by both the research monitor and the live run loop.

A single tick captures the full book state for the active BTC up/down
window on Polymarket.  Callers attach lists of async ``TickHandler`` and
``WindowEndHandler`` callables.  Handlers are awaited sequentially so that
latency-sensitive handlers (e.g. the executor) can assume they run on a
stable snapshot of the tick and on a predictable schedule.

Polymarket CLOB reads are synchronous inside the SDK, so ``_poll_window``
performs them directly on the asyncio loop.  Gamma discovery uses aiohttp.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import aiohttp

from polymarket_client import PolymarketClient

from . import config
from .window import Window, current_window_boundaries, resolve_window

logger = logging.getLogger("snipe.loop")

# Seconds-remaining checkpoints captured into per-window summary rows.
# Ordered from largest to smallest so ``record`` can walk once per tick.
SNAPSHOT_THRESHOLDS_S = (30, 15, 10, 5, 2, 0)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _pick_best(levels, side: str) -> tuple[Optional[float], Optional[float]]:
    """Return (best_price, size_at_best) across a list of CLOB book levels.

    Levels may be raw dicts or SDK objects with ``.price`` / ``.size``.
    Bids collapse to max-price; asks to min-price.  Multiple levels at the
    best price are summed into ``size_at_best``.
    """
    if not levels:
        return None, None

    best_price: Optional[float] = None
    size_at_best: float = 0.0

    for lvl in levels:
        price = lvl.price if hasattr(lvl, "price") else lvl.get("price")
        size = lvl.size if hasattr(lvl, "size") else lvl.get("size")
        try:
            price_f = float(price)
            size_f = float(size)
        except (TypeError, ValueError):
            continue

        if best_price is None:
            best_price, size_at_best = price_f, size_f
            continue

        if side == "bid":
            if price_f > best_price:
                best_price, size_at_best = price_f, size_f
            elif price_f == best_price:
                size_at_best += size_f
        else:
            if price_f < best_price:
                best_price, size_at_best = price_f, size_f
            elif price_f == best_price:
                size_at_best += size_f

    return best_price, size_at_best


def _book_snapshot(poly: PolymarketClient, token_id: str) -> dict:
    """Return top-of-book prices and sizes for one Polymarket token.

    Any value may be ``None`` if the book read fails or the book is empty.
    Callers are expected to tolerate partial snapshots rather than halting.
    """
    snap: dict[str, Optional[float]] = {
        "bid": None,
        "bid_size": None,
        "ask": None,
        "ask_size": None,
        "mid": None,
    }
    try:
        book = poly.get_orderbook(token_id)
    except Exception as e:
        logger.warning("orderbook %s: %s", token_id[:12], e)
        return snap

    bids = book.bids if hasattr(book, "bids") else book.get("bids", [])
    asks = book.asks if hasattr(book, "asks") else book.get("asks", [])

    bid, bid_size = _pick_best(bids, "bid")
    ask, ask_size = _pick_best(asks, "ask")
    snap["bid"] = bid
    snap["bid_size"] = bid_size
    snap["ask"] = ask
    snap["ask_size"] = ask_size
    if bid is not None and ask is not None:
        snap["mid"] = (bid + ask) / 2.0
    elif bid is not None:
        snap["mid"] = bid
    elif ask is not None:
        snap["mid"] = ask
    return snap


@dataclass
class Tick:
    ts_utc: datetime
    window_slug: str
    window_start_utc: datetime
    window_end_utc: datetime
    seconds_remaining: float
    elapsed_s: float
    up: dict
    down: dict

    @property
    def total_mid(self) -> Optional[float]:
        if self.up.get("mid") is None or self.down.get("mid") is None:
            return None
        return self.up["mid"] + self.down["mid"]

    @property
    def leader_side(self) -> Optional[str]:
        u, d = self.up.get("mid"), self.down.get("mid")
        if u is None and d is None:
            return None
        if u is None:
            return "down"
        if d is None:
            return "up"
        if u == d:
            return None
        return "up" if u > d else "down"

    @property
    def leader_ask(self) -> Optional[float]:
        side = self.leader_side
        if side == "up":
            return self.up.get("ask")
        if side == "down":
            return self.down.get("ask")
        return None

    @property
    def leader_ask_size(self) -> Optional[float]:
        side = self.leader_side
        if side == "up":
            return self.up.get("ask_size")
        if side == "down":
            return self.down.get("ask_size")
        return None

    @property
    def leader_mid(self) -> Optional[float]:
        side = self.leader_side
        if side == "up":
            return self.up.get("mid")
        if side == "down":
            return self.down.get("mid")
        return None

    @property
    def leader_token_id(self) -> Optional[str]:
        # Populated by callers who have the window available.
        return getattr(self, "_leader_token_id", None)


@dataclass
class WindowAccumulator:
    """Collects per-window metadata and checkpoint snapshots for summary output."""
    window: Window
    tick_count: int = 0
    last_tick: Optional[Tick] = None
    snapshots: dict[int, Tick] = field(default_factory=dict)
    entries_submitted: int = 0

    def record(self, tick: Tick) -> None:
        self.tick_count += 1
        self.last_tick = tick
        for thr in SNAPSHOT_THRESHOLDS_S:
            if thr in self.snapshots:
                continue
            if tick.seconds_remaining <= thr:
                self.snapshots[thr] = tick


@dataclass
class LoopContext:
    """Per-tick payload passed to every handler."""
    tick: Tick
    window: Window
    acc: WindowAccumulator
    poly: PolymarketClient


TickHandler = Callable[[LoopContext], Awaitable[None]]
WindowEndHandler = Callable[[WindowAccumulator], Awaitable[None]]


async def _poll_window(poly: PolymarketClient, window: Window) -> Tick:
    up_snap = _book_snapshot(poly, window.up_token) if window.up_token else {}
    down_snap = _book_snapshot(poly, window.down_token) if window.down_token else {}
    now = _now_utc()
    tick = Tick(
        ts_utc=now,
        window_slug=window.slug,
        window_start_utc=window.start,
        window_end_utc=window.end,
        seconds_remaining=window.seconds_remaining(now),
        elapsed_s=window.elapsed_s(now),
        up=up_snap,
        down=down_snap,
    )
    if tick.leader_side == "up":
        tick._leader_token_id = window.up_token  # type: ignore[attr-defined]
    elif tick.leader_side == "down":
        tick._leader_token_id = window.down_token  # type: ignore[attr-defined]
    return tick


async def run_window_loop(
    poly: PolymarketClient,
    on_tick: list[TickHandler],
    on_window_end: list[WindowEndHandler],
    *,
    poll_interval_s: Optional[float] = None,
    tail_interval_s: Optional[float] = None,
    tail_window_s: Optional[float] = None,
    duration_minutes: Optional[float] = None,
    on_start: Optional[Callable[[], Awaitable[None]]] = None,
    on_new_window: Optional[Callable[[Window], Awaitable[None]]] = None,
) -> None:
    """
    Discover the current window, poll books, and fan out to handlers.

    The loop rotates on window expiry, invoking ``on_window_end`` with the
    accumulator for the completed window before resetting for the next one.
    Handlers are awaited in list order; if a handler raises it is logged
    and skipped so one misbehaving handler cannot wedge the loop.

    Polling cadence tightens from ``poll_interval_s`` to ``tail_interval_s``
    when the window has less than ``tail_window_s`` seconds remaining.
    """
    poll_interval_s = poll_interval_s if poll_interval_s is not None else config.SNIPE_POLL_INTERVAL_S
    tail_interval_s = tail_interval_s if tail_interval_s is not None else config.SNIPE_POLL_INTERVAL_TAIL_S
    tail_window_s = tail_window_s if tail_window_s is not None else config.SNIPE_TAIL_WINDOW_S

    if on_start is not None:
        await on_start()

    session_start = time.time()
    acc: Optional[WindowAccumulator] = None

    async with aiohttp.ClientSession() as session:
        try:
            while True:
                if duration_minutes is not None:
                    if (time.time() - session_start) / 60.0 >= duration_minutes:
                        break

                start, end = current_window_boundaries()

                if acc is None or acc.window.start != start:
                    if acc is not None:
                        await _dispatch_window_end(acc, on_window_end)
                    window = await resolve_window(session, start, end)
                    acc = WindowAccumulator(window=window)
                    if on_new_window is not None:
                        try:
                            await on_new_window(window)
                        except Exception:
                            logger.exception("on_new_window handler failed")
                elif not acc.window.has_tokens():
                    acc.window = await resolve_window(session, start, end)
                    if acc.window.has_tokens() and on_new_window is not None:
                        try:
                            await on_new_window(acc.window)
                        except Exception:
                            logger.exception("on_new_window handler failed")

                if acc.window.has_tokens():
                    tick = await _poll_window(poly, acc.window)
                    acc.record(tick)
                    ctx = LoopContext(tick=tick, window=acc.window, acc=acc, poly=poly)
                    for handler in on_tick:
                        try:
                            await handler(ctx)
                        except Exception:
                            logger.exception("tick handler %s failed", handler.__name__)

                tail = (
                    acc.window.has_tokens()
                    and acc.window.seconds_remaining() <= tail_window_s
                )
                await asyncio.sleep(tail_interval_s if tail else poll_interval_s)
        except KeyboardInterrupt:
            pass
        finally:
            if acc is not None and acc.tick_count > 0:
                await _dispatch_window_end(acc, on_window_end)


async def _dispatch_window_end(
    acc: WindowAccumulator,
    handlers: list[WindowEndHandler],
) -> None:
    for handler in handlers:
        try:
            await handler(acc)
        except Exception:
            logger.exception("window-end handler %s failed", handler.__name__)
