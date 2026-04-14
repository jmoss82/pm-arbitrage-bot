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
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from enum import Enum

import config
from kalshi_client import KalshiClient, Orderbook
from polymarket_client import PolymarketClient
from market_matcher import MarketPair

logger = logging.getLogger(__name__)
_SNAPSHOT_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="arb-scan")


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
    poly_yes_fee_rate: float | None = None
    poly_no_fee_rate: float | None = None

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
    def poly_no_mid(self) -> float | None:
        if self.poly_no_bid is not None and self.poly_no_ask is not None:
            return (self.poly_no_bid + self.poly_no_ask) / 2
        return self.poly_no_bid or self.poly_no_ask

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

    spread_width: float         # cross-platform YES divergence (executable)
    entry_cost_per_contract: float  # total cost to enter both legs
    est_round_trip_fees: float  # estimated fees if spread fully closes
    net_edge: float             # divergence minus fees

    # What we'd pay on each platform to enter
    cheap_yes_price: float      # buy YES here (lower platform)
    expensive_no_price: float   # buy NO here (higher platform)
    cheap_yes_platform: str
    expensive_no_platform: str
    poly_total: float | None = None
    kalshi_total: float | None = None
    kalshi_leg_available_qty: int | None = None
    poly_fee_rate: float = 0.0

    def __repr__(self):
        return (
            f"SpreadOpportunity({self.pair.label!r}, spread={self.spread_width:.4f}, "
            f"net_edge={self.net_edge:.4f})"
        )


# -- Fee model for round-trip trades ------------------------------------------

KALSHI_TAKER_FEE_RATE = 0.07


def _clamp_price(price: float) -> float:
    return max(0.0001, min(0.9999, price))


def _round_poly_fee(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))


def _round_kalshi_fee(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def polymarket_order_fee(price: float, contracts: int, fee_rate: float) -> float:
    price = _clamp_price(price)
    raw = contracts * fee_rate * price * (1.0 - price)
    return _round_poly_fee(raw)


def kalshi_order_fee(price: float, contracts: int) -> float:
    price = _clamp_price(price)
    raw = KALSHI_TAKER_FEE_RATE * contracts * price * (1.0 - price)
    return _round_kalshi_fee(raw)


def estimate_entry_fees(
    entry_yes_price: float,
    entry_no_price: float,
    yes_platform: str,
    poly_fee_rate: float,
    contracts: int = 1,
) -> float:
    if yes_platform == "polymarket":
        yes_fee = polymarket_order_fee(entry_yes_price, contracts, poly_fee_rate)
        no_fee = kalshi_order_fee(entry_no_price, contracts)
    else:
        yes_fee = kalshi_order_fee(entry_yes_price, contracts)
        no_fee = polymarket_order_fee(entry_no_price, contracts, poly_fee_rate)
    return yes_fee + no_fee


def estimate_exit_fees(
    exit_yes_price: float,
    exit_no_price: float,
    yes_platform: str,
    poly_fee_rate: float,
    contracts: int = 1,
) -> float:
    if yes_platform == "polymarket":
        yes_fee = polymarket_order_fee(exit_yes_price, contracts, poly_fee_rate)
        no_fee = kalshi_order_fee(exit_no_price, contracts)
    else:
        yes_fee = kalshi_order_fee(exit_yes_price, contracts)
        no_fee = polymarket_order_fee(exit_no_price, contracts, poly_fee_rate)
    return yes_fee + no_fee


def estimate_round_trip_fees(
    entry_yes_price: float,
    entry_no_price: float,
    exit_yes_price: float,
    exit_no_price: float,
    yes_platform: str,
    poly_fee_rate: float,
    contracts: int = 1,
) -> float:
    return estimate_entry_fees(
        entry_yes_price,
        entry_no_price,
        yes_platform,
        poly_fee_rate,
        contracts=contracts,
    ) + estimate_exit_fees(
        exit_yes_price,
        exit_no_price,
        yes_platform,
        poly_fee_rate,
        contracts=contracts,
    )


def estimate_entry_exit_fees_simple(
    entry_yes_price: float,
    entry_no_price: float,
    yes_platform: str,
    poly_fee_rate: float,
    exit_yes_price: float | None = None,
) -> float:
    if exit_yes_price is None:
        exit_yes_price = 0.5
    exit_yes_price = _clamp_price(exit_yes_price)
    exit_no_price = _clamp_price(1.0 - exit_yes_price)
    return estimate_round_trip_fees(
        entry_yes_price,
        entry_no_price,
        exit_yes_price,
        exit_no_price,
        yes_platform,
        poly_fee_rate,
    ) + config.ARB_ESTIMATED_ROUND_TRIP_SLIPPAGE


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

    def _fetch_kalshi_book() -> dict:
        ob = kalshi.get_orderbook(pair.kalshi.ticker)
        return _kalshi_book_to_prices(ob)

    def _fetch_poly_quotes() -> dict[str, float | None]:
        return poly.get_market_quotes(
            pair.poly.token_yes,
            pair.poly.token_no,
            allow_midpoint_fallback=False,
        )

    started = time.perf_counter()
    kalshi_future = _SNAPSHOT_EXECUTOR.submit(_fetch_kalshi_book)
    poly_future = _SNAPSHOT_EXECUTOR.submit(_fetch_poly_quotes)

    try:
        prices = kalshi_future.result()
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
        quotes = poly_future.result()
        snap.poly_yes_bid = quotes.get("yes_bid")
        snap.poly_yes_ask = quotes.get("yes_ask")
        snap.poly_no_bid = quotes.get("no_bid")
        snap.poly_no_ask = quotes.get("no_ask")
        try:
            snap.poly_yes_fee_rate = poly.get_fee_rate(pair.poly.token_yes)
        except Exception as e:
            logger.warning("Poly fee lookup failed for YES token %s: %s", pair.poly.token_yes[:12], e)
        try:
            snap.poly_no_fee_rate = poly.get_fee_rate(pair.poly.token_no)
        except Exception as e:
            logger.warning("Poly fee lookup failed for NO token %s: %s", pair.poly.token_no[:12], e)
    except Exception as e:
        logger.warning("Poly price fetch failed for %s: %s", pair.poly.question[:30], e)

    logger.debug(
        "Snapshot latency for %s: %.0fms",
        pair.kalshi.ticker,
        (time.perf_counter() - started) * 1000,
    )

    return snap


# -- Spread detection ----------------------------------------------------------

def _compute_divergence(snapshot: PriceSnapshot) -> tuple[float | None, float | None]:
    """Return (kalshi_yes_ref, poly_yes_ref) using the best available prices.

    Prefer executable prices (bid/ask crossing the platforms), fall back
    to midpoints when one side of the book is missing.
    """
    k_yes = snapshot.kalshi_yes_mid
    p_yes = snapshot.poly_yes_mid

    if k_yes is None and snapshot.kalshi_yes_bid is not None:
        k_yes = snapshot.kalshi_yes_bid
    if k_yes is None and snapshot.kalshi_yes_ask is not None:
        k_yes = snapshot.kalshi_yes_ask

    if p_yes is None and snapshot.poly_yes_bid is not None:
        p_yes = snapshot.poly_yes_bid
    if p_yes is None and snapshot.poly_yes_ask is not None:
        p_yes = snapshot.poly_yes_ask

    return k_yes, p_yes


def detect_spread(snapshot: PriceSnapshot) -> SpreadOpportunity | None:
    """
    Detect a tradeable cross-platform divergence.

    The spread is the gap between how the two platforms price the same
    YES outcome.  When the divergence is large enough to cover round-trip
    fees, we buy YES where it's cheap and NO where it's expensive, then
    exit when prices converge.
    """
    best: SpreadOpportunity | None = None

    k_yes_ref, p_yes_ref = _compute_divergence(snapshot)
    if k_yes_ref is None or p_yes_ref is None:
        return None

    # Direction 1: Kalshi YES is higher -> buy YES on Poly (cheap), buy NO on Kalshi
    # Divergence = kalshi_yes - poly_yes (positive when Kalshi prices Up higher)
    if k_yes_ref > p_yes_ref:
        divergence = k_yes_ref - p_yes_ref

        entry_yes = snapshot.poly_yes_ask if snapshot.poly_yes_ask is not None else p_yes_ref
        entry_no = snapshot.kalshi_no_ask if snapshot.kalshi_no_ask is not None else (1.0 - k_yes_ref)
        entry_cost = entry_yes + entry_no

        poly_fee_rate = snapshot.poly_yes_fee_rate or 0.0
        est_exit_yes = _clamp_price((k_yes_ref + p_yes_ref) / 2)
        fees = estimate_entry_exit_fees_simple(
            entry_yes,
            entry_no,
            "polymarket",
            poly_fee_rate,
            exit_yes_price=est_exit_yes,
        )
        net = divergence - fees
        entry_fees = estimate_entry_fees(entry_yes, entry_no, "polymarket", poly_fee_rate)

        if divergence > 0:
            opp = SpreadOpportunity(
                pair=snapshot.pair,
                direction=SpreadDirection.KALSHI_HIGHER,
                snapshot=snapshot,
                spread_width=divergence,
                entry_cost_per_contract=entry_cost + entry_fees,
                est_round_trip_fees=fees,
                net_edge=net,
                cheap_yes_price=entry_yes,
                expensive_no_price=entry_no,
                cheap_yes_platform="polymarket",
                expensive_no_platform="kalshi",
                poly_total=(snapshot.poly_yes_mid + snapshot.poly_no_mid)
                if snapshot.poly_yes_mid is not None and snapshot.poly_no_mid is not None
                else None,
                kalshi_total=(snapshot.kalshi_yes_mid + (snapshot.kalshi_no_bid + snapshot.kalshi_no_ask) / 2)
                if snapshot.kalshi_yes_mid is not None and snapshot.kalshi_no_bid is not None and snapshot.kalshi_no_ask is not None
                else None,
                kalshi_leg_available_qty=snapshot.kalshi_no_ask_qty,
                poly_fee_rate=poly_fee_rate,
            )
            if best is None or opp.net_edge > best.net_edge:
                best = opp

    # Direction 2: Poly YES is higher -> buy YES on Kalshi (cheap), buy NO on Poly
    # Divergence = poly_yes - kalshi_yes (positive when Poly prices Up higher)
    if p_yes_ref > k_yes_ref:
        divergence = p_yes_ref - k_yes_ref

        entry_yes = snapshot.kalshi_yes_ask if snapshot.kalshi_yes_ask is not None else k_yes_ref
        entry_no = snapshot.poly_no_ask if snapshot.poly_no_ask is not None else (1.0 - p_yes_ref)
        entry_cost = entry_yes + entry_no

        poly_fee_rate = snapshot.poly_no_fee_rate or 0.0
        est_exit_yes = _clamp_price((k_yes_ref + p_yes_ref) / 2)
        fees = estimate_entry_exit_fees_simple(
            entry_yes,
            entry_no,
            "kalshi",
            poly_fee_rate,
            exit_yes_price=est_exit_yes,
        )
        net = divergence - fees
        entry_fees = estimate_entry_fees(entry_yes, entry_no, "kalshi", poly_fee_rate)

        if divergence > 0:
            opp = SpreadOpportunity(
                pair=snapshot.pair,
                direction=SpreadDirection.POLY_HIGHER,
                snapshot=snapshot,
                spread_width=divergence,
                entry_cost_per_contract=entry_cost + entry_fees,
                est_round_trip_fees=fees,
                net_edge=net,
                cheap_yes_price=entry_yes,
                expensive_no_price=entry_no,
                cheap_yes_platform="kalshi",
                expensive_no_platform="polymarket",
                poly_total=(snapshot.poly_yes_mid + snapshot.poly_no_mid)
                if snapshot.poly_yes_mid is not None and snapshot.poly_no_mid is not None
                else None,
                kalshi_total=(snapshot.kalshi_yes_mid + (snapshot.kalshi_no_bid + snapshot.kalshi_no_ask) / 2)
                if snapshot.kalshi_yes_mid is not None and snapshot.kalshi_no_bid is not None and snapshot.kalshi_no_ask is not None
                else None,
                kalshi_leg_available_qty=snapshot.kalshi_yes_ask_qty,
                poly_fee_rate=poly_fee_rate,
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
