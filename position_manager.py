"""
Position manager -- tracks open arb positions and generates exit signals.

Each position is a paired trade across both platforms:
  - YES shares on one platform
  - NO shares on the other platform

The manager monitors the current spread for each open position and
signals an exit when the spread has compressed enough to lock in profit,
or when a stop-loss threshold is hit (spread widening against us).
"""
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from arb_scanner import (
    PriceSnapshot, SpreadDirection, SpreadOpportunity,
    fetch_snapshot, estimate_entry_fees, estimate_exit_fees,
)
from market_matcher import MarketPair
from trade_logger import log_lifecycle_row

logger = logging.getLogger(__name__)

POSITIONS_FILE = Path("data/open_positions.json")


@dataclass
class ArbPosition:
    """A live arb position across both platforms."""
    id: str
    pair_label: str
    direction: str  # SpreadDirection value

    # Entry details
    entry_time: float
    entry_spread: float
    contracts: int
    entry_cost: float   # total USD committed (both legs)

    # Per-contract entry prices
    yes_entry_price: float
    no_entry_price: float
    yes_platform: str   # where we hold YES
    no_platform: str    # where we hold NO

    # Kalshi/Poly identifiers for the actual positions
    kalshi_ticker: str = ""
    poly_token_yes: str = ""
    poly_token_no: str = ""
    poly_condition_id: str = ""
    poly_fee_rate: float = 0.0

    # Live tracking (updated each scan)
    current_spread: float = 0.0
    best_spread: float = 0.0         # lowest (most compressed) spread seen
    current_yes_bid: float = 0.0
    current_no_bid: float = 0.0
    unrealized_pnl: float = 0.0
    last_update: float = 0.0

    # Exit config
    target_exit_spread: float = 0.0   # exit when spread narrows to this
    stop_loss_spread: float = 0.0     # exit when spread widens past this

    # Status
    status: str = "open"  # open, exiting, closed

    @property
    def spread_compression(self) -> float:
        """How much the spread has compressed since entry (positive = good)."""
        return self.entry_spread - self.current_spread

    @property
    def spread_compression_pct(self) -> float:
        """Compression as % of entry spread."""
        if self.entry_spread > 0:
            return self.spread_compression / self.entry_spread
        return 0.0

    @property
    def hold_time_minutes(self) -> float:
        if self.last_update > 0:
            return (self.last_update - self.entry_time) / 60
        return (time.time() - self.entry_time) / 60

    @property
    def hold_time_seconds(self) -> float:
        if self.last_update > 0:
            return self.last_update - self.entry_time
        return time.time() - self.entry_time


class PositionManager:
    """Manages the lifecycle of open arb positions."""

    def __init__(
        self,
        exit_target_pct: float | None = None,
        stop_loss_pct: float | None = None,
    ):
        import config as _cfg
        self.positions: dict[str, ArbPosition] = {}
        self.closed_positions: list[ArbPosition] = []
        self.exit_target_pct = exit_target_pct if exit_target_pct is not None else _cfg.ARB_EXIT_TARGET_PCT
        self.stop_loss_pct = stop_loss_pct if stop_loss_pct is not None else _cfg.ARB_STOP_LOSS_PCT
        self.stale_exit_seconds = _cfg.ARB_STALE_EXIT_SECONDS
        self.stale_min_compression_pct = _cfg.ARB_STALE_MIN_COMPRESSION_PCT
        self._load()

    # -- Position creation -----------------------------------------------------

    def open_position(
        self,
        opp: SpreadOpportunity,
        contracts: int,
        entry_cost: float,
    ) -> ArbPosition:
        pos_id = f"arb-{int(time.time())}-{len(self.positions)}"

        target = opp.spread_width * (1 - self.exit_target_pct)
        stop = opp.spread_width * (1 + self.stop_loss_pct)

        pos = ArbPosition(
            id=pos_id,
            pair_label=opp.pair.label,
            direction=opp.direction.value,
            entry_time=time.time(),
            entry_spread=opp.spread_width,
            contracts=contracts,
            entry_cost=entry_cost,
            yes_entry_price=opp.cheap_yes_price,
            no_entry_price=opp.expensive_no_price,
            yes_platform=opp.cheap_yes_platform,
            no_platform=opp.expensive_no_platform,
            kalshi_ticker=opp.pair.kalshi_ticker,
            poly_token_yes=opp.pair.poly.token_yes,
            poly_token_no=opp.pair.poly.token_no,
            poly_condition_id=opp.pair.poly.condition_id,
            poly_fee_rate=opp.poly_fee_rate,
            current_spread=opp.spread_width,
            best_spread=opp.spread_width,
            target_exit_spread=target,
            stop_loss_spread=stop,
            status="open",
        )
        self.positions[pos_id] = pos
        self._save()
        logger.info("Opened position %s: %s, %d contracts, spread %.4f",
                     pos_id, opp.pair.label, contracts, opp.spread_width)
        return pos

    def has_open_position(self, kalshi_ticker: str, direction: str) -> bool:
        """Return True when an equivalent position is already open."""
        for pos in self.positions.values():
            if pos.status != "open":
                continue
            if pos.kalshi_ticker == kalshi_ticker and pos.direction == direction:
                return True
        return False

    # -- Spread monitoring -----------------------------------------------------

    def update_position(self, pos: ArbPosition, snapshot: PriceSnapshot):
        """Update a position's current spread from a fresh price snapshot.

        current_spread tracks the cross-platform YES divergence — the same
        metric used at entry (spread_width).  When it shrinks toward zero
        the platforms are converging and we're profiting.
        """
        pos.last_update = time.time()

        k_yes = snapshot.kalshi_yes_mid
        p_yes = snapshot.poly_yes_mid
        if k_yes is None:
            k_yes = snapshot.kalshi_yes_bid or snapshot.kalshi_yes_ask
        if p_yes is None:
            p_yes = snapshot.poly_yes_bid or snapshot.poly_yes_ask

        if k_yes is not None and p_yes is not None:
            if pos.direction == SpreadDirection.KALSHI_HIGHER.value:
                pos.current_spread = k_yes - p_yes
            else:
                pos.current_spread = p_yes - k_yes

        # Trailing stop: ratchet stop-loss tighter as spread compresses
        pos.best_spread = min(pos.best_spread, pos.current_spread)
        if pos.entry_spread > 0:
            compression = 1 - (pos.best_spread / pos.entry_spread)
            if compression >= 0.60:
                new_stop = pos.entry_spread * 0.70
                pos.stop_loss_spread = min(pos.stop_loss_spread, new_stop)
            elif compression >= 0.40:
                pos.stop_loss_spread = min(pos.stop_loss_spread, pos.entry_spread)

        # Track exitable bids for P&L computation
        if pos.direction == SpreadDirection.KALSHI_HIGHER.value:
            if snapshot.poly_yes_bid is not None:
                pos.current_yes_bid = snapshot.poly_yes_bid
            if snapshot.kalshi_no_bid is not None:
                pos.current_no_bid = snapshot.kalshi_no_bid
        else:
            if snapshot.kalshi_yes_bid is not None:
                pos.current_yes_bid = snapshot.kalshi_yes_bid
            if snapshot.poly_no_bid is not None:
                pos.current_no_bid = snapshot.poly_no_bid

        exit_yes = pos.current_yes_bid or pos.yes_entry_price
        exit_no = pos.current_no_bid or pos.no_entry_price
        exit_proceeds = (exit_yes + exit_no) * pos.contracts
        fees = estimate_exit_fees(
            exit_yes, exit_no,
            pos.yes_platform,
            pos.poly_fee_rate,
            contracts=pos.contracts,
        )
        pos.unrealized_pnl = exit_proceeds - pos.entry_cost - fees

    def check_exit_signals(self) -> list[tuple[ArbPosition, str]]:
        """
        Check all open positions for exit signals.
        Returns list of (position, reason) tuples.
        """
        signals = []
        for pos in self.positions.values():
            if pos.status != "open":
                continue

            # Target hit: spread compressed enough AND P&L is non-negative.
            # The P&L gate prevents exiting "profitably" by divergence while
            # actually losing money to bid-ask friction.  Time-stop and
            # stop-loss still fire unconditionally.
            if pos.current_spread <= pos.target_exit_spread and pos.unrealized_pnl >= 0:
                signals.append((pos, "target"))
                logger.info("EXIT SIGNAL [target]: %s spread %.4f <= target %.4f (pnl $%.2f)",
                            pos.pair_label, pos.current_spread, pos.target_exit_spread, pos.unrealized_pnl)

            # Stop loss: spread widened against us
            elif pos.current_spread >= pos.stop_loss_spread:
                signals.append((pos, "stop_loss"))
                logger.warning("EXIT SIGNAL [stop]: %s spread %.4f >= stop %.4f",
                               pos.pair_label, pos.current_spread, pos.stop_loss_spread)

            # Stale trade: enough time has passed without meaningful compression
            # and the position still is not at least breakeven.
            elif (
                self.stale_exit_seconds > 0
                and pos.hold_time_seconds >= self.stale_exit_seconds
                and pos.spread_compression_pct < self.stale_min_compression_pct
                and pos.unrealized_pnl <= 0
            ):
                signals.append((pos, "stale"))
                logger.info(
                    "EXIT SIGNAL [stale]: %s age %.0fs compression %.0f%% < %.0f%% (pnl $%.2f)",
                    pos.pair_label,
                    pos.hold_time_seconds,
                    pos.spread_compression_pct * 100,
                    self.stale_min_compression_pct * 100,
                    pos.unrealized_pnl,
                )

        return signals

    # -- Position closing ------------------------------------------------------

    def close_position(self, pos_id: str, realized_pnl: float = 0.0, reason: str = "manual"):
        pos = self.positions.pop(pos_id, None)
        if pos:
            pos.status = "closed"
            pos.unrealized_pnl = realized_pnl
            self.closed_positions.append(pos)
            self._save()
            logger.info("Closed position %s: P&L $%.2f", pos_id, realized_pnl)
            try:
                log_lifecycle_row({
                    "position_id": pos.id,
                    "pair_label": pos.pair_label,
                    "reason": reason,
                    "contracts": pos.contracts,
                    "entry_spread": round(pos.entry_spread, 6),
                    "exit_spread": round(pos.current_spread, 6),
                    "spread_compression_pct": round(pos.spread_compression_pct, 6),
                    "hold_minutes": round(pos.hold_time_minutes, 3),
                    "realized_pnl": round(realized_pnl, 6),
                    "direction": pos.direction,
                    "yes_platform": pos.yes_platform,
                    "no_platform": pos.no_platform,
                })
            except Exception as e:
                logger.warning("Failed to write lifecycle row for %s: %s", pos.id, e)

    # -- Persistence -----------------------------------------------------------

    def _save(self):
        POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "open": {pid: asdict(p) for pid, p in self.positions.items()},
            "closed_count": len(self.closed_positions),
        }
        with open(POSITIONS_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def _load(self):
        if not POSITIONS_FILE.exists():
            return
        try:
            with open(POSITIONS_FILE) as f:
                data = json.load(f)
            for pid, pdata in data.get("open", {}).items():
                self.positions[pid] = ArbPosition(**{
                    k: v for k, v in pdata.items()
                    if k in ArbPosition.__dataclass_fields__
                })
            logger.info("Loaded %d open positions from disk", len(self.positions))
        except Exception as e:
            logger.warning("Failed to load positions: %s", e)

    # -- Display ---------------------------------------------------------------

    def print_positions(self):
        if not self.positions:
            print("  No open positions.")
            return

        print(f"\n  Open Positions ({len(self.positions)}):")
        print(f"  {'ID':<20} {'Market':<35} {'Contracts':>9} {'Entry Spread':>12} {'Current':>8} {'Compress':>9} {'Unreal P&L':>10} {'Hold':>8}")
        print(f"  {'-'*20} {'-'*35} {'-'*9} {'-'*12} {'-'*8} {'-'*9} {'-'*10} {'-'*8}")

        total_pnl = 0.0
        for pos in self.positions.values():
            compress = f"{pos.spread_compression_pct*100:.0f}%" if pos.entry_spread > 0 else "-"
            hold = f"{pos.hold_time_minutes:.0f}m"
            pnl_str = f"${pos.unrealized_pnl:+.2f}"
            total_pnl += pos.unrealized_pnl

            print(f"  {pos.id:<20} {pos.pair_label[:35]:<35} {pos.contracts:>9} "
                  f"{pos.entry_spread:>12.4f} {pos.current_spread:>8.4f} {compress:>9} "
                  f"{pnl_str:>10} {hold:>8}")

        print(f"\n  Total unrealized P&L: ${total_pnl:+.2f}")
        print(f"  Closed positions: {len(self.closed_positions)}")
