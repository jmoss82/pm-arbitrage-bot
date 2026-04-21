"""
Order execution for the snipe strategy.

We submit **Fill-And-Kill** (IOC) BUY orders only.  Any unmatched remainder
is cancelled by the exchange immediately, which is the only safe behavior
for a strategy that enters seconds before resolution: a resting GTC order
at $0.99 could otherwise match minutes or hours later against a loser token.

There is no exit leg.  Positions are held to settlement and resolved by
``snipe/settler.py`` after the window closes.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from py_clob_client.clob_types import OrderArgs, OrderType

from polymarket_client import PolymarketClient

from . import positions as positions_mod
from .loop import Tick
from .scanner import EntryDecision
from .window import Window

logger = logging.getLogger("snipe.executor")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_no_match_error(text: Optional[str]) -> bool:
    if not text:
        return False
    msg = text.lower()
    return "no orders found to match" in msg or "fak_no_match" in msg


def submit_fak_buy(
    poly: PolymarketClient,
    token_id: str,
    price: float,
    size: float,
) -> dict:
    """Submit a Fill-And-Kill BUY on Polymarket CLOB.

    Returns the raw response dict from the SDK.  Any portion of the order
    that is not matched against standing asks is cancelled by the exchange
    -- there is never a resting order left behind.
    """
    args = OrderArgs(token_id=token_id, price=price, size=size, side="BUY")
    signed = poly.clob.create_order(args)
    return poly.clob.post_order(signed, orderType=OrderType.FAK)


def _extract_order_id(resp: dict | None) -> Optional[str]:
    if not isinstance(resp, dict):
        return None
    for key in ("orderID", "orderId", "order_id", "id"):
        v = resp.get(key)
        if v:
            return str(v)
    # Some responses nest it.
    order = resp.get("order")
    if isinstance(order, dict):
        return _extract_order_id(order)
    return None


def _extract_order_id_from_error(exc: Exception) -> Optional[str]:
    """Best-effort parse of an order id embedded in SDK exception text."""
    for attr in ("error_message", "message", "args"):
        value = getattr(exc, attr, None)
        if isinstance(value, dict):
            order_id = _extract_order_id(value)
            if order_id:
                return order_id
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, dict):
                    order_id = _extract_order_id(item)
                    if order_id:
                        return order_id
                if isinstance(item, str):
                    match = re.search(r"0x[a-fA-F0-9]{64}", item)
                    if match:
                        return match.group(0)
        if isinstance(value, str):
            match = re.search(r"0x[a-fA-F0-9]{64}", value)
            if match:
                return match.group(0)

    text = str(exc)
    match = re.search(r"0x[a-fA-F0-9]{64}", text)
    return match.group(0) if match else None


def _extract_status(resp: dict | None) -> Optional[str]:
    if not isinstance(resp, dict):
        return None
    for key in ("status", "state"):
        v = resp.get(key)
        if v:
            return str(v).lower()
    return None


def _extract_filled_size(resp: dict | None) -> Optional[float]:
    if not isinstance(resp, dict):
        return None
    for key in ("matched_size", "matchedSize", "size_matched", "filled_size"):
        v = resp.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _extract_avg_price(resp: dict | None) -> Optional[float]:
    if not isinstance(resp, dict):
        return None
    for key in ("average_price", "averagePrice", "avg_price"):
        v = resp.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _refresh_fill_state(
    poly: PolymarketClient,
    position: positions_mod.SnipePosition,
) -> tuple[float, Optional[float], Optional[str]]:
    """Best-effort follow-up read for ambiguous immediate post-order responses."""
    if not position.order_id:
        return 0.0, None, position.submit_status
    try:
        filled, partial, status = poly.get_order_fill_state(position.order_id)
        avg = poly.get_order_avg_fill_price(position.order_id)
        if filled:
            matched = position.requested_size
        elif partial:
            order = poly.get_order(position.order_id) or {}
            raw = (
                order.get("size_matched")
                or order.get("matchedSize")
                or order.get("filled_size")
            )
            matched = float(raw) if raw is not None else 0.0
        else:
            matched = 0.0
        return matched, avg, status
    except Exception:
        logger.exception("follow-up get_order failed for %s", position.order_id)
        return 0.0, None, position.submit_status


def execute_entry(
    poly: PolymarketClient,
    decision: EntryDecision,
    window: Window,
    tick: Tick,
    dry_run: bool,
) -> positions_mod.SnipePosition:
    """
    Execute (or simulate) a single-leg BUY and persist the resulting position.

    Returns the saved ``SnipePosition`` in all cases.  ``position.status``
    reflects the outcome:

    * ``open``         -- order submitted successfully (dry-run or live)
    * ``entry_failed`` -- submission raised or was rejected

    The position record is written to disk before we return, so a crash
    mid-call still leaves a trace we can investigate against the exchange.
    """
    position = positions_mod.make_position(
        dry_run=dry_run,
        window_slug=window.slug,
        window_start_utc=window.start.isoformat(),
        window_end_utc=window.end.isoformat(),
        condition_id=window.condition_id,
        token_id=decision.token_id,  # type: ignore[arg-type]
        side=decision.side,  # type: ignore[arg-type]
        requested_price=decision.limit_price,  # type: ignore[arg-type]
        requested_size=decision.size,  # type: ignore[arg-type]
        seconds_remaining_at_signal=tick.seconds_remaining,
        leader_mid_at_signal=tick.leader_mid,
        leader_ask_at_signal=tick.leader_ask,
        leader_ask_size_at_signal=tick.leader_ask_size,
    )
    position.extra["consume_window_slot"] = True

    if dry_run:
        # Optimistic fill simulation: assume the quoted ask matches at its
        # quoted price.  This makes dry-run telemetry comparable to live
        # telemetry when the book actually holds up.  The settler will
        # still compare side-vs-outcome to score the trade, so wins and
        # losses in dry-run mode are realistic even if fills are idealized.
        position.submit_status = "dry_run"
        position.filled = True
        position.partial = False
        position.filled_size = decision.size
        position.avg_fill_price = decision.limit_price
        position.entry_cost_usd = round(
            (decision.limit_price or 0.0) * (decision.size or 0.0), 6
        )
        position.entry_fee_rate_bps = 0
        position.entry_fee_usd = 0.0
        positions_mod.upsert_position(position)
        return position

    # Live path -- hot code.  Everything up to the submit should have
    # happened on the caller's clock; we only measure the submit round-trip
    # here and store it for calibration.
    submit_started = time.perf_counter()
    try:
        resp = submit_fak_buy(
            poly,
            decision.token_id,  # type: ignore[arg-type]
            decision.limit_price,  # type: ignore[arg-type]
            decision.size,  # type: ignore[arg-type]
        )
    except Exception as e:
        position.order_id = _extract_order_id_from_error(e)
        position.submit_error = f"{type(e).__name__}: {e}"
        position.submit_latency_ms = (time.perf_counter() - submit_started) * 1000.0
        if position.order_id:
            matched, avg, status = _refresh_fill_state(poly, position)
            position.submit_status = status
            position.filled = matched > 0 and matched >= (decision.size or 0.0)
            position.partial = 0.0 < matched < (decision.size or 0.0)
            position.filled_size = matched if matched > 0 else 0.0
            position.avg_fill_price = avg if matched > 0 else None
            position.entry_cost_usd = (
                round(avg * matched, 6) if (matched > 0 and avg is not None) else 0.0
            )
            if position.filled or position.partial:
                try:
                    position.entry_fee_rate_bps = poly.get_fee_rate_bps(decision.token_id)  # type: ignore[arg-type]
                    position.entry_fee_usd = round(
                        (position.entry_fee_rate_bps / 10_000.0) * (position.entry_cost_usd or 0.0),
                        6,
                    )
                except Exception:
                    position.entry_fee_rate_bps = None
                    position.entry_fee_usd = None
                positions_mod.upsert_position(position)
                return position
        position.status = positions_mod.STATUS_ENTRY_FAILED
        position.extra["consume_window_slot"] = not _is_no_match_error(position.submit_error)
        positions_mod.upsert_position(position)
        logger.exception("submit_fak_buy failed for %s", window.slug)
        return position

    position.submit_latency_ms = (time.perf_counter() - submit_started) * 1000.0
    position.submit_status = _extract_status(resp)
    position.order_id = _extract_order_id(resp)

    matched = _extract_filled_size(resp) or 0.0
    avg = _extract_avg_price(resp) or decision.limit_price
    if matched <= 0.0 and position.order_id:
        matched, followup_avg, followup_status = _refresh_fill_state(poly, position)
        if followup_status:
            position.submit_status = followup_status
        if followup_avg is not None:
            avg = followup_avg
    position.filled = matched > 0 and (decision.size or 0.0) > 0 and matched >= (decision.size or 0.0)
    position.partial = 0.0 < matched < (decision.size or 0.0)
    position.filled_size = matched if matched > 0 else 0.0
    position.avg_fill_price = avg if matched > 0 else None
    position.entry_cost_usd = (
        round(avg * matched, 6) if (matched > 0 and avg is not None) else 0.0
    )

    # FAK either filled or was cancelled.  A zero-match response means
    # the order died without taking any liquidity -- treat it as a failed
    # entry rather than an open position so the book is not polluted.
    if not (position.filled or position.partial):
        position.status = positions_mod.STATUS_ENTRY_FAILED
        position.submit_error = position.submit_error or "fak_no_match"
        position.extra["consume_window_slot"] = not _is_no_match_error(position.submit_error)
    else:
        # Fetch the live fee rate in the background path only; in the hot
        # loop this extra call is ~50-200ms and not worth the latency
        # unless we are live.  Rate changes rarely so a post-submit read is
        # fine for accounting.
        try:
            position.entry_fee_rate_bps = poly.get_fee_rate_bps(decision.token_id)  # type: ignore[arg-type]
            position.entry_fee_usd = round(
                (position.entry_fee_rate_bps / 10_000.0) * (position.entry_cost_usd or 0.0),
                6,
            )
        except Exception:
            position.entry_fee_rate_bps = None
            position.entry_fee_usd = None

    positions_mod.upsert_position(position)
    return position
