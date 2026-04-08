"""
Spread scanner -- detects cross-platform pricing divergences worth trading.

Strategy: convergence trading, not hold-to-resolution.
  - Enter when the spread between platforms is wide enough to cover round-trip costs.
  - Exit when the spread narrows. Profit = spread compression minus fees.
  - Never need the market to resolve. Never hold a guaranteed loser to zero.

Round-trip cost model:
  Entry:  buy at ask on both platforms (pay the spread on each book)
  Exit:   sell at bid on both platforms (pay the spread again)
  Fees:   Polymarket ~2% on each trade's profit, Kalshi ~7% on each trade's profit

  The spread needs to compress by more than the total round-trip friction.
"""
import logging
import time
from dataclasses import dataclass
from enum import Enum

from kalshi_client import KalshiClient, Orderbook
from polymarket_client import PolymarketClient
from market_matcher import MarketPair

logger = logging.getLogger(__name__)


class SpreadDirection(Enum):
    """Which platform is pricing YES higher."""
    POLY_HIGHER = "poly_higher"   # Poly YES > Kalshi YES -> buy YES on Kalshi, buy NO on Poly
    KALSHI_HIGHER = "kalshi_higher"  # Kalshi YES > Poly YES -> buy YES on Poly, buy NO on Kalshi
    NONE = "none"


@dataclass
class PriceSnapshot:
    """Point-in-time prices for a market pair."""
    pair: MarketPair
    timestamp: float

    # Kalshi (all decimal 0-1)
    kalshi_yes_bid: float | None = None
    kalshi_yes_ask: float | None = None
    kalshi_no_bid: float | None = None
    kalshi_no_ask: float | None = None
    kalshi_yes_bid_qty: int | None = None
    kalshi_yes_ask_qty: int | None = None
    kalshi_no_bid_qty: int | None = None
    kalshi_no_ask_qty: int | None = None

    # Polymarket (already decimal)
    poly_yes_bid: float | None = None
    poly_yes_ask: float | None = None
    poly_no_bid: float | None = None
    poly_no_ask: float | None = None

    @property
    def kalshi_yes_mid(self) -> float | None:
        if self.kalshi_yes_bid is not None and self.kalshi_yes_ask is not None:
            return (self.kalshi_yes_bid + self.kalshi_yes_ask) / 2
        return self.kalshi_yes_bid or self.kalshi_yes_ask

    @property
    def poly_yes_mid(self) -> float | None:
        if self.poly_yes_bid is not None and self.poly_yes_ask is not None:
            return (self.poly_yes_bid + self.poly_yes_ask) / 2
        return self.poly_yes_bid or self.poly_yes_ask

    @property
    def mid_spread(self) -> float | None:
        """Midpoint spread: how far apart the two platforms are at mid."""
        if self.kalshi_yes_mid is not None and self.poly_yes_mid is not None:
            return abs(self.kalshi_yes_mid - self.poly_yes_mid)
        return None


@dataclass
class SpreadOpportunity:
    """A detected spread worth entering."""
    pair: MarketPair
    direction: SpreadDirection
    snapshot: PriceSnapshot

    # The spread as we'd actually capture it (entry prices, not mids)
    spread_width: float         # raw price gap between platforms
    entry_cost_per_contract: float  # total cost to enter both legs
    est_exit_proceeds: float    # what we'd get if spread fully closes
    est_round_trip_fees: float  # entry + exit fees
    net_edge: float             # profit if spread fully closes, after fees

    # What we'd pay on each platform to enter
    cheap_yes_price: float      # buy YES here (lower platform)
    expensive_no_price: float   # buy NO here (higher platform), = 1 - expensive_yes_bid
    cheap_yes_platform: str
    expensive_no_platform: str
    poly_total: float | None = None
    kalshi_total: float | None = None
    kalshi_leg_available_qty: int | None = None

    def __repr__(self):
        return (
            f"SpreadOpportunity({self.pair.label!r}, spread={self.spread_width:.4f}, "
            f"net_edge={self.net_edge:.4f})"
        )


# -- Fee model for round-trip trades ------------------------------------------

POLY_FEE_RATE = 0.02
KALSHI_FEE_RATE = 0.07


def estimate_round_trip_fees(
    entry_yes_price: float,
    entry_no_price: float,
    exit_yes_price: float,
    exit_no_price: float,
    yes_platform: str,
) -> float:
    """
    Estimate fees for a full round trip (enter + exit), no resolution.

    Each platform charges a fee on the profit of each individual trade.
    If you buy at 0.40 and sell at 0.48, your profit is 0.08 and the
    fee applies to that 0.08.
    If you buy at 0.60 and sell at 0.55, you lost money -- no fee.
    """
    yes_rate = POLY_FEE_RATE if yes_platform == "polymarket" else KALSHI_FEE_RATE
    no_rate = KALSHI_FEE_RATE if yes_platform == "polymarket" else POLY_FEE_RATE

    # YES leg profit (buy low, sell higher after convergence)
    yes_profit = max(0, exit_yes_price - entry_yes_price)
    yes_fee = yes_rate * yes_profit

    # NO leg profit (buy low, sell higher after convergence)
    no_profit = max(0, exit_no_price - entry_no_price)
    no_fee = no_rate * no_profit

    return yes_fee + no_fee


def estimate_entry_exit_fees_simple(spread_width: float, yes_platform: str) -> float:
    """
    Quick estimate: if the spread fully closes, each leg gains ~half the spread.
    Conservative: assume both legs are profitable and both get taxed.
    """
    yes_rate = POLY_FEE_RATE if yes_platform == "polymarket" else KALSHI_FEE_RATE
    no_rate = KALSHI_FEE_RATE if yes_platform == "polymarket" else POLY_FEE_RATE
    avg_rate = (yes_rate + no_rate) / 2
    return avg_rate * spread_width


# -- Price fetching (unchanged from before) ------------------------------------

def _kalshi_book_to_prices(ob: Orderbook) -> dict:
    """Convert Kalshi orderbook to best bid/ask in decimal (0-1)."""
    result = {}
    if ob.yes:
        result["yes_bid"] = ob.yes[-1].price / 100.0
        result["yes_bid_qty"] = ob.yes[-1].quantity
    if ob.no:
        result["no_bid"] = ob.no[-1].price / 100.0
        result["no_bid_qty"] = ob.no[-1].quantity
        result["yes_ask"] = (100 - ob.no[-1].price) / 100.0
        result["yes_ask_qty"] = ob.no[-1].quantity
    if ob.yes:
        result["no_ask"] = (100 - ob.yes[-1].price) / 100.0
        result["no_ask_qty"] = ob.yes[-1].quantity
    return result


def fetch_snapshot(
    pair: MarketPair,
    kalshi: KalshiClient,
    poly: PolymarketClient,
) -> PriceSnapshot:
    snap = PriceSnapshot(pair=pair, timestamp=time.time())

    try:
        ob = kalshi.get_orderbook(pair.kalshi.ticker)
        prices = _kalshi_book_to_prices(ob)
        snap.kalshi_yes_bid = prices.get("yes_bid")
        snap.kalshi_yes_ask = prices.get("yes_ask")
        snap.kalshi_no_bid = prices.get("no_bid")
        snap.kalshi_no_ask = prices.get("no_ask")
        snap.kalshi_yes_bid_qty = prices.get("yes_bid_qty")
        snap.kalshi_yes_ask_qty = prices.get("yes_ask_qty")
        snap.kalshi_no_bid_qty = prices.get("no_bid_qty")
        snap.kalshi_no_ask_qty = prices.get("no_ask_qty")
    except Exception as e:
        logger.warning("Kalshi book fetch failed for %s: %s", pair.kalshi.ticker, e)

    try:
        poly_yes_bid, poly_yes_ask = poly.get_best_prices(pair.poly.token_yes)
        snap.poly_yes_bid = poly_yes_bid
        snap.poly_yes_ask = poly_yes_ask
        if poly_yes_bid is not None:
            snap.poly_no_ask = 1.0 - poly_yes_bid
        if poly_yes_ask is not None:
            snap.poly_no_bid = 1.0 - poly_yes_ask
    except Exception as e:
        logger.warning("Poly price fetch failed for %s: %s", pair.poly.question[:30], e)

    return snap


# -- Spread detection ----------------------------------------------------------

def detect_spread(snapshot: PriceSnapshot) -> SpreadOpportunity | None:
    """
    Detect a tradeable spread between the two platforms.

    The spread exists when one platform prices YES significantly higher
    than the other. We enter by buying YES on the cheap side and NO
    on the expensive side, then exit both when prices converge.
    """
    best: SpreadOpportunity | None = None

    # Direction 1: Kalshi YES is higher -> buy YES on Poly (cheap), buy NO on Kalshi
    if (snapshot.poly_yes_ask is not None and snapshot.kalshi_yes_bid is not None
            and snapshot.kalshi_yes_bid > snapshot.poly_yes_ask):
        spread = snapshot.kalshi_yes_bid - snapshot.poly_yes_ask
        entry_yes = snapshot.poly_yes_ask               # buy YES on Poly
        entry_no = 1.0 - snapshot.kalshi_yes_bid         # buy NO on Kalshi (= sell YES at their bid)
        entry_cost = entry_yes + entry_no

        # If spread fully closes, both positions gain. Estimate exit at midpoint convergence.
        # YES we bought at entry_yes, could sell at ~entry_yes + spread/2
        # NO we bought at entry_no, could sell at ~entry_no + spread/2
        # Simplified: full convergence means exit proceeds = entry_cost + spread
        est_exit = entry_cost + spread
        fees = estimate_entry_exit_fees_simple(spread, "polymarket")
        net = spread - fees

        opp = SpreadOpportunity(
            pair=snapshot.pair,
            direction=SpreadDirection.KALSHI_HIGHER,
            snapshot=snapshot,
            spread_width=spread,
            entry_cost_per_contract=entry_cost,
            est_exit_proceeds=est_exit,
            est_round_trip_fees=fees,
            net_edge=net,
            cheap_yes_price=entry_yes,
            expensive_no_price=entry_no,
            cheap_yes_platform="polymarket",
            expensive_no_platform="kalshi",
            poly_total=(snapshot.poly_yes_mid + (snapshot.poly_no_bid + snapshot.poly_no_ask) / 2)
            if snapshot.poly_yes_mid is not None and snapshot.poly_no_bid is not None and snapshot.poly_no_ask is not None
            else None,
            kalshi_total=(snapshot.kalshi_yes_mid + (snapshot.kalshi_no_bid + snapshot.kalshi_no_ask) / 2)
            if snapshot.kalshi_yes_mid is not None and snapshot.kalshi_no_bid is not None and snapshot.kalshi_no_ask is not None
            else None,
            kalshi_leg_available_qty=snapshot.kalshi_no_ask_qty,
        )
        if best is None or opp.net_edge > best.net_edge:
            best = opp

    # Direction 2: Poly YES is higher -> buy YES on Kalshi (cheap), buy NO on Poly
    if (snapshot.kalshi_yes_ask is not None and snapshot.poly_yes_bid is not None
            and snapshot.poly_yes_bid > snapshot.kalshi_yes_ask):
        spread = snapshot.poly_yes_bid - snapshot.kalshi_yes_ask
        entry_yes = snapshot.kalshi_yes_ask               # buy YES on Kalshi
        entry_no = 1.0 - snapshot.poly_yes_bid             # buy NO on Poly
        entry_cost = entry_yes + entry_no

        est_exit = entry_cost + spread
        fees = estimate_entry_exit_fees_simple(spread, "kalshi")
        net = spread - fees

        opp = SpreadOpportunity(
            pair=snapshot.pair,
            direction=SpreadDirection.POLY_HIGHER,
            snapshot=snapshot,
            spread_width=spread,
            entry_cost_per_contract=entry_cost,
            est_exit_proceeds=est_exit,
            est_round_trip_fees=fees,
            net_edge=net,
            cheap_yes_price=entry_yes,
            expensive_no_price=entry_no,
            cheap_yes_platform="kalshi",
            expensive_no_platform="polymarket",
            poly_total=(snapshot.poly_yes_mid + (snapshot.poly_no_bid + snapshot.poly_no_ask) / 2)
            if snapshot.poly_yes_mid is not None and snapshot.poly_no_bid is not None and snapshot.poly_no_ask is not None
            else None,
            kalshi_total=(snapshot.kalshi_yes_mid + (snapshot.kalshi_no_bid + snapshot.kalshi_no_ask) / 2)
            if snapshot.kalshi_yes_mid is not None and snapshot.kalshi_no_bid is not None and snapshot.kalshi_no_ask is not None
            else None,
            kalshi_leg_available_qty=snapshot.kalshi_yes_ask_qty,
        )
        if best is None or opp.net_edge > best.net_edge:
            best = opp

    return best


def scan_all_pairs(
    pairs: list[MarketPair],
    kalshi: KalshiClient,
    poly: PolymarketClient,
    min_spread: float = 0.0,
) -> list[SpreadOpportunity]:
    """Scan all pairs for spread opportunities. Returns sorted by net_edge desc."""
    opportunities: list[SpreadOpportunity] = []

    for pair in pairs:
        if not pair.kalshi or not pair.poly.token_yes:
            continue
        try:
            snap = fetch_snapshot(pair, kalshi, poly)
            opp = detect_spread(snap)
            if opp and opp.net_edge >= min_spread:
                opportunities.append(opp)
        except Exception as e:
            logger.warning("Error scanning %s: %s", pair.label, e)

    opportunities.sort(key=lambda o: -o.net_edge)
    return opportunities


def print_opportunities(opps: list[SpreadOpportunity]):
    if not opps:
        print("  No spread opportunities detected.")
        return

    for i, o in enumerate(opps, 1):
        status = "TRADEABLE" if o.net_edge > 0 else "too thin (fees)"
        print(f"\n  [{i}] {o.pair.label}")
        print(f"      Direction:    {o.direction.value}")
        print(f"      Spread:       {o.spread_width:.4f}  ({o.spread_width*100:.2f}%)")
        print(f"      Buy YES @     {o.cheap_yes_price:.4f} on {o.cheap_yes_platform}")
        print(f"      Buy NO  @     {o.expensive_no_price:.4f} on {o.expensive_no_platform}")
        print(f"      Entry cost:   {o.entry_cost_per_contract:.4f}")
        print(f"      Est. RT fees: {o.est_round_trip_fees:.4f}")
        print(f"      Net edge:     {o.net_edge:.4f}  ({o.net_edge*100:.2f}%) <- {status}")
