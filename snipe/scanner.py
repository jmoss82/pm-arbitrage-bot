"""
Entry-signal evaluator for the snipe strategy.

Stateless-ish: a ``SessionState`` is threaded through to enforce budgets
and per-window uniqueness, but the per-tick decision logic is a pure
function of the tick, window, and session state at call time.

Reject reasons are returned verbatim so the run loop can log them into
the trade-signal telemetry for later audit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import config
from .loop import Tick
from .window import Window


@dataclass
class EntryDecision:
    should_enter: bool
    reason: str
    side: Optional[str] = None
    token_id: Optional[str] = None
    limit_price: Optional[float] = None
    size: Optional[float] = None
    detected_at_utc: Optional[datetime] = None


@dataclass
class SessionState:
    """
    Runs alongside the loop.  Tracks per-window entry counts, daily spend,
    and the most recent entry submission time so we can rate-limit re-entries
    if ``SNIPE_MAX_ENTRIES_PER_WINDOW > 1``.

    ``entries_by_window`` is keyed by window slug and incremented on every
    non-dry-run submission and every dry-run simulated entry.  Spend is
    aggregated across both dry and live to keep the guardrail meaningful
    while testing.
    """
    entries_by_window: dict[str, int] = field(default_factory=dict)
    total_spend_today_usd: float = 0.0
    last_spend_reset_utc_date: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    last_entry_at: Optional[datetime] = None
    open_positions_count: int = 0

    def maybe_reset_daily(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.last_spend_reset_utc_date:
            self.last_spend_reset_utc_date = today
            self.total_spend_today_usd = 0.0

    def register_attempt(self, window_slug: str) -> None:
        """Count one submission attempt against the per-window cap.

        Called unconditionally before the executor runs -- so a submit
        that times out or is rejected still "uses up" the window's single
        entry slot.  This keeps us from burning a second 10-30s warmup
        stall in the same 5-minute window when the first one fails.
        """
        self.entries_by_window[window_slug] = (
            self.entries_by_window.get(window_slug, 0) + 1
        )
        self.last_entry_at = datetime.now(timezone.utc)

    def register_fill(self, cost_usd: float) -> None:
        """Add a confirmed fill's cost to the daily spend budget."""
        self.total_spend_today_usd += cost_usd

    def entries_this_window(self, window_slug: str) -> int:
        return self.entries_by_window.get(window_slug, 0)


def _compute_size(position_usd: float, price: float) -> float:
    """Round share count to 2 decimals; Polymarket accepts fractional sizes."""
    if price <= 0:
        return 0.0
    return round(position_usd / price, 2)


def evaluate_tick(
    tick: Tick,
    window: Window,
    session: SessionState,
) -> EntryDecision:
    """Evaluate a single tick against all entry gates.

    The order of checks matters for the telemetry story: the cheapest gates
    fire first so rejection reasons for common no-ops (price out of band,
    wrong time) dominate the rejection histogram over rarer reasons.
    """
    session.maybe_reset_daily()

    if tick.leader_side is None:
        return EntryDecision(False, "no_leader")

    ask = tick.leader_ask
    if ask is None:
        return EntryDecision(False, "no_ask_on_leader")

    if not (config.SNIPE_MIN_ENTRY_PRICE <= ask <= config.SNIPE_MAX_ENTRY_PRICE):
        return EntryDecision(False, f"ask_out_of_band({ask:.4f})")

    if tick.seconds_remaining > config.SNIPE_MAX_SECONDS_REMAINING:
        return EntryDecision(False, f"too_early({tick.seconds_remaining:.1f}s)")
    if tick.seconds_remaining < config.SNIPE_MIN_SECONDS_REMAINING:
        return EntryDecision(False, f"too_late({tick.seconds_remaining:.1f}s)")

    ask_size = tick.leader_ask_size or 0.0
    if ask_size < config.SNIPE_MIN_TOP_OF_BOOK_SIZE:
        return EntryDecision(False, f"thin_book(size={ask_size:.0f})")

    entries_this_window = session.entries_this_window(window.slug)
    if entries_this_window >= config.SNIPE_MAX_ENTRIES_PER_WINDOW:
        return EntryDecision(
            False,
            f"window_entry_cap({entries_this_window}/{config.SNIPE_MAX_ENTRIES_PER_WINDOW})",
        )

    if session.open_positions_count >= config.SNIPE_MAX_OPEN_POSITIONS:
        return EntryDecision(
            False,
            f"max_open_positions({session.open_positions_count}/{config.SNIPE_MAX_OPEN_POSITIONS})",
        )

    size = _compute_size(config.SNIPE_POSITION_USD, ask)
    if size <= 0:
        return EntryDecision(False, "bad_size_computed")
    est_cost = round(ask * size, 6)

    if session.total_spend_today_usd + est_cost > config.SNIPE_MAX_SPEND_PER_DAY_USD:
        return EntryDecision(
            False,
            f"daily_cap_hit(spent={session.total_spend_today_usd:.2f} + {est_cost:.2f} > {config.SNIPE_MAX_SPEND_PER_DAY_USD:.2f})",
        )

    token_id = window.up_token if tick.leader_side == "up" else window.down_token
    if not token_id:
        return EntryDecision(False, "missing_token_id")

    # Use the quoted ask as the limit.  FAK guarantees we either match
    # immediately or cancel -- there is no need to cross further than
    # the quoted ask, because we are the only one trying to lift this level
    # in the final few seconds before settlement.
    return EntryDecision(
        should_enter=True,
        reason="ok",
        side=tick.leader_side,
        token_id=token_id,
        limit_price=ask,
        size=size,
        detected_at_utc=tick.ts_utc,
    )
