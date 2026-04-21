"""
Snipe position state and JSON persistence.

Each ``SnipePosition`` represents one BUY executed (or simulated) against
a single Polymarket outcome token and held to window resolution.  There is
no exit leg; positions are closed when the settler records the outcome and
computes realized P&L.

State is stored in ``data/snipe/positions.json`` as a list of position
dicts.  The same file is read + rewritten on every update; the expected
steady-state size is small (tens to hundreds of rows), so full-file
rewrites are fine and avoid partial-write corruption risk.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import config

logger = logging.getLogger("snipe.positions")


STATUS_OPEN = "open"
STATUS_SETTLED_WIN = "settled_win"
STATUS_SETTLED_LOSS = "settled_loss"
STATUS_SETTLED_VOID = "settled_void"
STATUS_ENTRY_FAILED = "entry_failed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SnipePosition:
    """One snipe entry, from submit through settlement."""
    id: str
    dry_run: bool

    # Market identity
    window_slug: str
    window_start_utc: str
    window_end_utc: str
    condition_id: Optional[str]
    token_id: str
    side: str  # "up" | "down"

    # Entry parameters
    requested_price: float
    requested_size: float
    requested_cost_usd: float

    # Submission + fill
    order_id: Optional[str] = None
    submitted_at_utc: Optional[str] = None
    submit_latency_ms: Optional[float] = None
    submit_status: Optional[str] = None        # raw SDK status ("matched", "posted", ...)
    submit_error: Optional[str] = None

    filled: bool = False
    partial: bool = False
    filled_size: Optional[float] = None
    avg_fill_price: Optional[float] = None
    entry_cost_usd: Optional[float] = None     # actual filled cost
    entry_fee_rate_bps: Optional[int] = None
    entry_fee_usd: Optional[float] = None

    # Signal context (useful for post-mortem calibration)
    seconds_remaining_at_signal: Optional[float] = None
    leader_mid_at_signal: Optional[float] = None
    leader_ask_at_signal: Optional[float] = None
    leader_ask_size_at_signal: Optional[float] = None

    # Settlement
    status: str = STATUS_OPEN
    settlement_checked_at_utc: Optional[str] = None
    resolved_outcome: Optional[str] = None     # "up" | "down" | ...
    proceeds_usd: Optional[float] = None
    realized_pnl_usd: Optional[float] = None
    settlement_notes: Optional[str] = None

    # Free-form diagnostics (latency breakdowns, retry info, etc.)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SnipePosition":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        # Unknown keys are preserved in ``extra`` so an old JSON file does
        # not hard-fail after the schema evolves.
        extras = {k: v for k, v in d.items() if k not in known}
        clean = {k: v for k, v in d.items() if k in known}
        existing_extra = clean.get("extra") or {}
        clean["extra"] = {**existing_extra, **extras}
        return cls(**clean)


def _positions_path() -> Path:
    return Path(config.SNIPE_DATA_DIR) / "positions.json"


def new_position_id() -> str:
    return uuid.uuid4().hex[:12]


def load_positions() -> list[SnipePosition]:
    path = _positions_path()
    if not path.exists():
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except Exception as e:
        logger.error("Failed to read %s: %s", path, e)
        return []
    if not isinstance(data, list):
        return []
    out: list[SnipePosition] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(SnipePosition.from_dict(entry))
        except Exception as e:
            logger.warning("skipping malformed position row: %s", e)
    return out


def _atomic_write(path: Path, payload: str) -> None:
    """Write JSON to a temp file and rename into place to avoid torn writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.write(payload)
    os.replace(tmp, path)


def save_positions(positions: Iterable[SnipePosition]) -> None:
    path = _positions_path()
    payload = json.dumps([p.to_dict() for p in positions], indent=2, default=str)
    _atomic_write(path, payload)


def upsert_position(position: SnipePosition) -> None:
    """Insert new or replace existing position (by ``id``)."""
    positions = load_positions()
    replaced = False
    for i, p in enumerate(positions):
        if p.id == position.id:
            positions[i] = position
            replaced = True
            break
    if not replaced:
        positions.append(position)
    save_positions(positions)


def open_positions() -> list[SnipePosition]:
    return [p for p in load_positions() if p.status == STATUS_OPEN]


def count_entries_for_window(window_slug: str) -> int:
    return sum(
        1
        for p in load_positions()
        if p.window_slug == window_slug and p.status != STATUS_ENTRY_FAILED
    )


def count_attempts_for_window(window_slug: str) -> int:
    """Count every submission attempt for a window, including failures.

    Used on startup to rebuild the in-memory per-window counter, so a
    Railway restart mid-window cannot re-submit into the slot we already
    took (or tried to take) pre-restart.
    """
    return sum(
        1
        for p in load_positions()
        if p.window_slug == window_slug and p.extra.get("consume_window_slot", True)
    )


def attempts_by_window_since(cutoff_utc_iso: str) -> dict[str, int]:
    """Return attempt counts keyed by window slug for all positions whose
    ``submitted_at_utc`` is >= the cutoff.

    Used to restore the in-memory per-window counter across process
    restarts.  The cutoff prevents us from dragging in ancient history;
    callers typically pass "an hour ago" so only the current and
    recently-closed windows are reloaded.
    """
    out: dict[str, int] = {}
    for p in load_positions():
        if not p.submitted_at_utc:
            continue
        if p.submitted_at_utc < cutoff_utc_iso:
            continue
        if not p.extra.get("consume_window_slot", True):
            continue
        out[p.window_slug] = out.get(p.window_slug, 0) + 1
    return out


def spend_today_usd() -> float:
    """Sum of entry costs for positions submitted (or simulated) today (UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0.0
    for p in load_positions():
        if not p.submitted_at_utc:
            continue
        if not p.submitted_at_utc.startswith(today):
            continue
        if p.status == STATUS_ENTRY_FAILED:
            continue
        cost = p.entry_cost_usd if p.entry_cost_usd is not None else p.requested_cost_usd
        total += cost or 0.0
    return total


def make_position(
    *,
    dry_run: bool,
    window_slug: str,
    window_start_utc: str,
    window_end_utc: str,
    condition_id: Optional[str],
    token_id: str,
    side: str,
    requested_price: float,
    requested_size: float,
    seconds_remaining_at_signal: float,
    leader_mid_at_signal: Optional[float],
    leader_ask_at_signal: Optional[float],
    leader_ask_size_at_signal: Optional[float],
) -> SnipePosition:
    cost = round(requested_price * requested_size, 6)
    return SnipePosition(
        id=new_position_id(),
        dry_run=dry_run,
        window_slug=window_slug,
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
        condition_id=condition_id,
        token_id=token_id,
        side=side,
        requested_price=requested_price,
        requested_size=requested_size,
        requested_cost_usd=cost,
        seconds_remaining_at_signal=seconds_remaining_at_signal,
        leader_mid_at_signal=leader_mid_at_signal,
        leader_ask_at_signal=leader_ask_at_signal,
        leader_ask_size_at_signal=leader_ask_size_at_signal,
        submitted_at_utc=_now_iso(),
    )
