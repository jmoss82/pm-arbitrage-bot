"""
Live Chainlink BTC/USD reference feed for the snipe strategy.

This module owns a long-running asyncio task that maintains a WebSocket
subscription to Polymarket's Real-Time Data Socket
(``wss://ws-live-data.polymarket.com``, topic ``crypto_prices_chainlink``,
symbol ``btc/usd``).  That feed is the exact oracle stream Polymarket
displays on the UI as "Current Price" and uses to derive the window's
"Price to Beat" (the first Chainlink tick at or after the window boundary).

The scanner consumes this feed via :meth:`ReferencePriceFeed.snapshot` to
compute the live distance between the current BTC price and the window's
"Price to Beat" -- the single strongest gate we have against the class of
losses caused by entering a $0.98 favorite that flips by $0.17 in the
last 500ms.

Protocol quirks (learned the hard way, see ``track_btc_5m_price.py``):

* The server's filter parser is whitespace-strict.  The filter payload
  MUST be the exact bytes ``{"symbol":"btc/usd"}`` -- adding a space
  after the colon causes the server to silently stop sending live updates
  after its initial history snapshot.
* Application-level keep-alives must be lowercase ``ping`` text frames,
  not the protocol-level pings that the ``websockets`` library sends by
  default.  Protocol pings are disabled (``ping_interval=None``) and we
  emit ``await ws.send("ping")`` on a fixed cadence.
* The initial history batch comes back with ``topic=crypto_prices`` and
  ``type=subscribe``; only live ticks carry ``topic=crypto_prices_chainlink``
  and ``type=update``.  We filter strictly to avoid treating historical
  ticks as live.

The feed fails-closed: until we have observed the opening tick of the
current window, ``window_partial`` stays True and the scanner refuses
to enter.  If the feed disconnects or stalls, the snapshot's
``last_tick_ts_utc`` ages out and :meth:`ReferenceSnapshot.is_fresh`
returns False -- again causing the scanner to refuse entries.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

from . import config

logger = logging.getLogger("snipe.reference_price")

RTDS_URL = "wss://ws-live-data.polymarket.com"
TOPIC = "crypto_prices_chainlink"
SYMBOL = "btc/usd"

# Exact bytes required by the RTDS filter parser.  Do not "prettify" this
# with json.dumps; a space after the colon breaks live updates.
CHAINLINK_BTC_FILTER = '{"symbol":"btc/usd"}'

APP_PING_INTERVAL_S = 5.0
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0

# A fresh connection that lands more than this many ms past a boundary is
# treated as a "partial" window: we did not observe the true opening tick,
# so price_to_beat stays null until the next boundary crossing.
PARTIAL_WINDOW_SLACK_MS = 1500


@dataclass(frozen=True)
class ReferenceSnapshot:
    """Immutable view of the feed's latest state.

    Returned by :meth:`ReferencePriceFeed.snapshot`.  All optional fields
    are ``None`` until the feed has produced at least one tick.

    Fields:
        current_price: Latest Chainlink BTC/USD price (USD).
        price_to_beat: Opening Chainlink tick of the current window (USD),
            or ``None`` if we joined mid-window and have not yet crossed
            a boundary.
        window_slug: Slug derived from the window the last tick belongs to,
            e.g. ``btc-updown-5m-1714000800``.  Matches the slug the
            scanner computes independently from wall-clock time; the two
            should agree except for the ~150ms surrounding a boundary.
        window_start_utc / window_end_utc: UTC boundaries of the current
            window.
        window_partial: True if we cannot trust ``price_to_beat`` because
            we joined mid-window.  The scanner MUST refuse entries when
            this is True.
        last_tick_recv_utc: Wall-clock time when we received the last
            live tick.  Used by :meth:`is_fresh` to reject stale data.
        last_tick_ts_ms: Chainlink's own timestamp on the tick (unix ms),
            for research / post-mortem.  Zero if unknown.
        ticks_seen: Cumulative count of live update ticks since the feed
            started.  Useful for the scanner to log "feed is producing
            data" during the first few seconds after startup.
    """
    current_price: Optional[float] = None
    price_to_beat: Optional[float] = None
    window_slug: str = ""
    window_start_utc: Optional[datetime] = None
    window_end_utc: Optional[datetime] = None
    window_partial: bool = True
    last_tick_recv_utc: Optional[datetime] = None
    last_tick_ts_ms: int = 0
    ticks_seen: int = 0

    def distance_usd(self) -> Optional[float]:
        """``current_price - price_to_beat`` in USD, or None if either is missing."""
        if self.current_price is None or self.price_to_beat is None:
            return None
        return self.current_price - self.price_to_beat

    def distance_bps(self) -> Optional[float]:
        """Distance expressed in basis points of price-to-beat."""
        d = self.distance_usd()
        if d is None or not self.price_to_beat:
            return None
        return (d / self.price_to_beat) * 10_000.0

    def implied_side(self) -> Optional[str]:
        """``up`` if price is above PTB, ``down`` if below, ``flat`` on tie, None if unknown."""
        d = self.distance_usd()
        if d is None:
            return None
        if d > 0:
            return "up"
        if d < 0:
            return "down"
        return "flat"

    def is_fresh(self, max_age_s: float, now: Optional[datetime] = None) -> bool:
        """True if the last live tick was received within ``max_age_s`` seconds."""
        if self.last_tick_recv_utc is None:
            return False
        now = now or datetime.now(timezone.utc)
        return (now - self.last_tick_recv_utc).total_seconds() <= max_age_s

    def is_usable(self, max_age_s: float, now: Optional[datetime] = None) -> bool:
        """True iff the scanner may trust distance / PTB for a live decision.

        All four conditions must hold:

        1. We have received at least one live tick (``current_price`` set).
        2. That tick is fresh within ``max_age_s``.
        3. We captured the window's opening reference (not partial).
        4. ``price_to_beat`` is set.
        """
        return (
            self.current_price is not None
            and self.is_fresh(max_age_s, now=now)
            and not self.window_partial
            and self.price_to_beat is not None
        )


class ReferencePriceFeed:
    """Background asyncio task that maintains the Chainlink subscription.

    Usage::

        feed = ReferencePriceFeed()
        feed.start()                       # schedules the task on the loop
        ...
        snap = feed.snapshot()             # cheap, non-blocking
        await feed.stop()                  # awaits clean shutdown

    Only ONE instance should run per process.  The snapshot is a frozen
    dataclass, so callers can stash a reference without worrying about
    mutation races.
    """

    def __init__(
        self,
        window_minutes: Optional[int] = None,
        slug_pattern: Optional[str] = None,
        on_event: Optional[callable] = None,
    ) -> None:
        self._window_minutes = window_minutes or config.SNIPE_WINDOW_MINUTES
        self._slug_pattern = slug_pattern or config.SNIPE_POLY_SLUG_PATTERN
        self._on_event = on_event  # optional hook: called with ("connected"|"disconnected"|"first_tick"|"new_window", detail)

        self._snapshot = ReferenceSnapshot()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ── public API ──────────────────────────────────────────────────────

    def start(self) -> asyncio.Task:
        """Start the background task (idempotent)."""
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(
                self._run(), name="snipe.reference_price"
            )
        return self._task

    async def stop(self) -> None:
        """Request clean shutdown and wait for the task to exit."""
        self._stop.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=3.0)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception:
            pass

    def snapshot(self) -> ReferenceSnapshot:
        """Return the current snapshot.  Cheap; safe to call every tick."""
        return self._snapshot

    # ── internals ───────────────────────────────────────────────────────

    def _emit(self, event: str, detail: object = None) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, detail)
        except Exception:
            logger.exception("reference feed on_event hook failed")

    def _window_for(self, moment: datetime) -> tuple[datetime, datetime, str]:
        minute = (moment.minute // self._window_minutes) * self._window_minutes
        start = moment.replace(minute=minute, second=0, microsecond=0)
        end = start + timedelta(minutes=self._window_minutes)
        slug = self._slug_pattern.format(ts=int(start.timestamp()))
        return start, end, slug

    async def _run(self) -> None:
        backoff = RECONNECT_MIN_S
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    RTDS_URL,
                    ping_interval=None,  # RTDS uses app-level "ping" text frames
                    close_timeout=2,
                    open_timeout=10,
                    max_size=2**22,
                ) as ws:
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{
                            "topic": TOPIC,
                            "type": "*",
                            "filters": CHAINLINK_BTC_FILTER,
                        }],
                    }))
                    logger.info("RTDS subscribed topic=%s symbol=%s", TOPIC, SYMBOL)
                    self._emit("connected", None)
                    backoff = RECONNECT_MIN_S

                    pinger = asyncio.create_task(self._ping_loop(ws))
                    try:
                        await self._consume(ws)
                    finally:
                        pinger.cancel()
                        try:
                            await pinger
                        except (asyncio.CancelledError, Exception):
                            pass
            except ConnectionClosed as e:
                logger.warning("RTDS closed (%s %s); reconnect in %.1fs",
                               e.code, e.reason, backoff)
                self._emit("disconnected", f"closed {e.code}")
            except OSError as e:
                logger.warning("RTDS network error %s: %s; reconnect in %.1fs",
                               type(e).__name__, e, backoff)
                self._emit("disconnected", f"net {type(e).__name__}")
            except Exception:
                logger.exception("RTDS unexpected error; reconnect in %.1fs", backoff)
                self._emit("disconnected", "error")

            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                return  # stop requested during backoff
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2.0, RECONNECT_MAX_S)

    async def _ping_loop(self, ws) -> None:
        while not self._stop.is_set():
            try:
                await ws.send("ping")
            except Exception:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=APP_PING_INTERVAL_S)
            except asyncio.TimeoutError:
                continue

    async def _consume(self, ws) -> None:
        async for raw in ws:
            if self._stop.is_set():
                return
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            if not raw:
                continue
            stripped = raw.strip()
            if not stripped or stripped.lower() in ("ping", "pong"):
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue

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
            self._ingest(tick_ms, value)

    def _ingest(self, tick_ms: int, value: float) -> None:
        now = datetime.now(timezone.utc)
        if tick_ms > 0:
            tick_dt = datetime.fromtimestamp(tick_ms / 1000.0, tz=timezone.utc)
        else:
            tick_dt = now

        start, end, slug = self._window_for(tick_dt)
        prev = self._snapshot

        if slug != prev.window_slug:
            # Boundary crossing (or first-ever tick).  Decide whether this
            # is the true opening observation or a mid-window join.
            ms_into = int((tick_dt - start).total_seconds() * 1000)
            if prev.window_slug == "":
                partial = ms_into > PARTIAL_WINDOW_SLACK_MS
                self._emit("first_tick", {"slug": slug, "partial": partial, "value": value})
            else:
                # Transition from a prior window we were already tracking.
                # This tick is definitionally the first observation of the
                # fresh window, so it IS the new price-to-beat.
                partial = False
                self._emit("new_window", {"slug": slug, "value": value})

            price_to_beat = None if partial else value
            self._snapshot = ReferenceSnapshot(
                current_price=value,
                price_to_beat=price_to_beat,
                window_slug=slug,
                window_start_utc=start,
                window_end_utc=end,
                window_partial=partial,
                last_tick_recv_utc=now,
                last_tick_ts_ms=tick_ms,
                ticks_seen=prev.ticks_seen + 1,
            )
            logger.info(
                "reference window %s start=%s partial=%s ptb=%s value=%.2f",
                slug, start.strftime("%H:%M"), partial,
                "??" if price_to_beat is None else f"{price_to_beat:,.2f}",
                value,
            )
            return

        # Same window, update current price (and possibly capture ptb if
        # this is the very first non-partial tick we've seen in a new
        # session -- though boundary-crossing logic above usually handles
        # that case).
        price_to_beat = prev.price_to_beat
        window_partial = prev.window_partial
        if price_to_beat is None and not window_partial:
            price_to_beat = value

        self._snapshot = replace(
            prev,
            current_price=value,
            price_to_beat=price_to_beat,
            last_tick_recv_utc=now,
            last_tick_ts_ms=tick_ms,
            ticks_seen=prev.ticks_seen + 1,
        )
