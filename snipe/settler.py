"""
Post-window settlement resolver.

Walks open snipe positions, queries Polymarket Gamma for the resolved
outcome, and records realized P&L.  Polymarket BTC 5-minute markets
resolve programmatically (no UMA dispute window), so resolution typically
lands within seconds of window close -- but we build in a grace period
anyway to avoid hammering Gamma with "not yet" queries.

P&L math (per position):

    proceeds_usd      = filled_size if won else 0
    realized_pnl_usd  = proceeds_usd - entry_cost_usd - entry_fee_usd

There is no exit fee on Polymarket settlement (winning shares redeem at
$1 on-chain with no taker fee), so ``entry_fee_usd`` is the only friction.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

from . import positions as positions_mod
from .positions import SnipePosition
from .window import GAMMA_API

logger = logging.getLogger("snipe.settler")


# Don't ask Gamma about a window until it has ended + this grace period.
# Empirically, Polymarket BTC 5-minute markets lag 4-10 minutes between
# window close and Gamma reporting ``closed: true`` with final outcome
# prices.  We start checking after 90s so the first query is speculative
# but cheap, then rely on per-position backoff below to avoid spamming.
SETTLEMENT_GRACE_SECONDS = 90

# After a "not yet resolved" response, wait at least this long before the
# next Gamma check for the same position.  This bounds Gamma chatter to
# a handful of calls per position across the typical 4-10 min lag window.
PENDING_RETRY_SECONDS = 45


def _parse_json_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    return []


def _extract_winner(mkt: dict) -> Optional[str]:
    """Return ``"up"`` / ``"down"`` / ``None``.

    A market is considered resolved when ``closed`` is true and
    ``outcomePrices`` contains exactly one value of 1.0 (or "1").  Any
    other shape -- unresolved, void, multi-winner -- returns None so the
    caller knows to retry or flag the position for manual review.
    """
    if not isinstance(mkt, dict):
        return None
    if not mkt.get("closed"):
        return None

    outcomes = _parse_json_list(mkt.get("outcomes", "[]"))
    prices = _parse_json_list(mkt.get("outcomePrices", "[]"))
    if len(outcomes) != len(prices) or not outcomes:
        return None

    winner_labels: list[str] = []
    for label, price in zip(outcomes, prices):
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            return None
        if price_f == 1.0:
            winner_labels.append(str(label).strip().lower())

    if len(winner_labels) != 1:
        return None

    label = winner_labels[0]
    if label in ("up", "yes", "higher"):
        return "up"
    if label in ("down", "no", "lower"):
        return "down"
    return None


async def fetch_market_for_settlement(
    session: aiohttp.ClientSession,
    condition_id: Optional[str],
    slug: Optional[str],
) -> Optional[dict]:
    """Fetch the raw market dict for a window that has ended, if Gamma has
    published a final resolution.

    Empirically, Gamma's default feed filters out markets whose window is
    past ``endDate`` until they flip to ``closed: true`` with final
    outcome prices -- which for BTC 5-min markets lands 4-10 minutes
    after window close.  Only markets in the first ~10 minutes of a
    "recently resolved" state need ``archived=true`` to surface, so we
    include both hints; this combo returns both recently-resolved and
    long-resolved rows, while (counter-intuitively) ``active=false`` or
    ``closed=true`` alone miss one of those classes.
    """
    params_base = {"closed": "true", "archived": "true"}

    for key, value in (("condition_ids", condition_id), ("slug", slug)):
        if not value:
            continue
        params = {**params_base, key: value}
        try:
            async with session.get(
                f"{GAMMA_API}/markets",
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
        except Exception as e:
            logger.warning("gamma settle fetch (%s=%s): %s", key, value, e)
            continue

        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and data:
            return data
    return None


def _window_end_dt(position: SnipePosition) -> Optional[datetime]:
    if not position.window_end_utc:
        return None
    try:
        return datetime.fromisoformat(position.window_end_utc.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_past_grace(position: SnipePosition, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    end = _window_end_dt(position)
    if end is None:
        return True
    return now >= end + timedelta(seconds=SETTLEMENT_GRACE_SECONDS)


def _should_recheck(position: SnipePosition, now: Optional[datetime] = None) -> bool:
    """Backoff to avoid re-hitting Gamma for a ``pending`` position every tick."""
    now = now or datetime.now(timezone.utc)
    last = position.extra.get("last_settlement_check_utc")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return True
    return now >= last_dt + timedelta(seconds=PENDING_RETRY_SECONDS)


def _mark_pending(position: SnipePosition, reason: str) -> None:
    position.extra["last_settlement_check_utc"] = datetime.now(timezone.utc).isoformat()
    position.extra["last_settlement_reason"] = reason


def _record_settlement(position: SnipePosition, winner: str) -> None:
    position.resolved_outcome = winner
    won = winner == position.side
    filled_size = position.filled_size or 0.0
    proceeds = filled_size if won else 0.0
    entry_cost = position.entry_cost_usd or 0.0
    entry_fee = position.entry_fee_usd or 0.0

    position.proceeds_usd = round(proceeds, 6)
    position.realized_pnl_usd = round(proceeds - entry_cost - entry_fee, 6)
    position.status = (
        positions_mod.STATUS_SETTLED_WIN if won else positions_mod.STATUS_SETTLED_LOSS
    )
    position.settlement_checked_at_utc = datetime.now(timezone.utc).isoformat()


async def settle_open_positions(
    session: Optional[aiohttp.ClientSession] = None,
    verbose: bool = False,
) -> dict:
    """Walk open positions and record outcomes for any that have resolved.

    Returns a small summary dict ``{"checked": N, "settled": N, "pending": N,
    "errors": N}`` for logging.  Safe to call repeatedly; already-settled
    rows are skipped.
    """
    open_rows = [p for p in positions_mod.load_positions() if p.status == positions_mod.STATUS_OPEN]
    summary = {"checked": 0, "settled": 0, "pending": 0, "errors": 0}
    if not open_rows:
        return summary

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()

    try:
        for position in open_rows:
            if not _is_past_grace(position):
                summary["pending"] += 1
                continue
            if not _should_recheck(position):
                summary["pending"] += 1
                continue
            summary["checked"] += 1
            mkt = await fetch_market_for_settlement(
                session,  # type: ignore[arg-type]
                position.condition_id,
                position.window_slug,
            )
            if mkt is None:
                # "Limbo" state: Polymarket has ended the window but the
                # market has not yet reappeared in Gamma with outcome
                # prices.  Not an error, just keep retrying after backoff.
                summary["pending"] += 1
                _mark_pending(position, "not_in_gamma")
                positions_mod.upsert_position(position)
                if verbose:
                    logger.info("in-limbo (no gamma market yet): %s", position.window_slug)
                continue

            winner = _extract_winner(mkt)
            if winner is None:
                summary["pending"] += 1
                _mark_pending(position, "not_resolved")
                positions_mod.upsert_position(position)
                if verbose:
                    logger.info("not yet resolved: %s", position.window_slug)
                continue

            _record_settlement(position, winner)
            positions_mod.upsert_position(position)
            summary["settled"] += 1
            if verbose:
                result = "WIN" if position.status == positions_mod.STATUS_SETTLED_WIN else "LOSS"
                logger.info(
                    "%s %s: entered %s @ %.4f, winner=%s, pnl=%.4f",
                    result,
                    position.window_slug,
                    position.side,
                    position.requested_price,
                    winner,
                    position.realized_pnl_usd or 0.0,
                )
    finally:
        if own_session and session is not None:
            await session.close()

    return summary
