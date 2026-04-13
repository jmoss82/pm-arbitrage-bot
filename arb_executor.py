"""
Arb executor -- handles entry and exit of spread positions on both platforms.

Entry: buy YES on the cheap platform, buy NO on the expensive platform.
Exit:  sell YES where we hold it, sell NO where we hold it.

Both entry and exit place orders on both platforms near-simultaneously.
Supports dry-run mode for paper trading.
"""
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from kalshi_client import KalshiClient, Order as KalshiOrder
from polymarket_client import PolymarketClient
from arb_scanner import SpreadOpportunity, SpreadDirection, _kalshi_book_to_prices
from position_manager import PositionManager, ArbPosition
import config
from trade_logger import log_execution

logger = logging.getLogger(__name__)

# Polymarket's CLOB has a known server-side balance cache bug where
# instantly-matched buys leave the cache stale, causing 100% sells to
# fail with "not enough balance / allowance".  Selling a slightly
# reduced share count (e.g. 98%) reliably passes validation.  The
# residual dust settles automatically at market resolution.
# See: https://github.com/Polymarket/py-clob-client/issues/287
_POLY_SELL_SIZE_FACTOR = config.ARB_POLY_SELL_SIZE_FACTOR


def _poly_sell_size(contracts: int) -> float:
    """Return a Polymarket-safe sell size: reduced by the sell factor and
    truncated to 3 decimal places (floor) to avoid floating-point dust."""
    raw = contracts * _POLY_SELL_SIZE_FACTOR
    return math.floor(raw * 1000) / 1000


@dataclass
class TradeResult:
    action: str  # "entry" or "exit"
    timestamp: float
    dry_run: bool
    contracts: int = 0
    total_cost_usd: float = 0.0

    poly_success: bool = False
    poly_error: str | None = None
    kalshi_success: bool = False
    kalshi_error: str | None = None
    poly_order_id: str | None = None
    kalshi_order_id: str | None = None
    poly_status: str | None = None
    kalshi_status: str | None = None
    poly_partial: bool = False
    kalshi_partial: bool = False

    @property
    def both_filled(self) -> bool:
        return self.poly_success and self.kalshi_success

    @property
    def one_leg_only(self) -> bool:
        return self.poly_success != self.kalshi_success

    def summary(self) -> str:
        mode = "[DRY RUN] " if self.dry_run else ""
        if self.both_filled:
            return f"{mode}{self.action.upper()} OK: {self.contracts} contracts, ${self.total_cost_usd:.2f}"
        elif self.one_leg_only:
            filled = "Poly" if self.poly_success else "Kalshi"
            failed = "Kalshi" if self.poly_success else "Poly"
            err = self.kalshi_error if self.poly_success else self.poly_error
            return f"{mode}{self.action.upper()} PARTIAL: {filled} OK, {failed} FAILED: {err}"
        elif self.poly_partial or self.kalshi_partial:
            return (
                f"{mode}{self.action.upper()} PARTIAL: "
                f"Poly={self.poly_status or 'partial'} Kalshi={self.kalshi_status or 'partial'}"
            )
        else:
            return f"{mode}{self.action.upper()} FAILED: Poly={self.poly_error} Kalshi={self.kalshi_error}"


@dataclass
class Ledger:
    entries: list[TradeResult] = field(default_factory=list)
    exits: list[TradeResult] = field(default_factory=list)
    daily_spent: float = 0.0
    _daily_reset_date: str = ""

    def reset_daily_if_needed(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_reset_date:
            self._daily_reset_date = today
            self.daily_spent = 0.0


class ArbExecutor:
    def __init__(
        self,
        kalshi: KalshiClient,
        poly: PolymarketClient,
        position_mgr: PositionManager,
        dry_run: bool | None = None,
        max_position_usd: float | None = None,
        max_daily_spend: float | None = None,
    ):
        self.kalshi = kalshi
        self.poly = poly
        self.positions = position_mgr
        self.dry_run = dry_run if dry_run is not None else config.ARB_DRY_RUN
        self.max_position_usd = max_position_usd or config.ARB_MAX_POSITION_USD
        self.max_daily_spend = max_daily_spend or config.ARB_MAX_DAILY_SPEND
        self.ledger = Ledger()
        self.poly_limit_offset = config.ARB_POLY_LIMIT_OFFSET
        self.kalshi_limit_offset_cents = config.ARB_KALSHI_LIMIT_OFFSET_CENTS
        self.allow_partials = config.ARB_ALLOW_PARTIAL_FILLS
        self.entry_marketable = config.ARB_ENTRY_MARKETABLE
        self.poly_entry_aggr = config.ARB_POLY_ENTRY_AGGRESSION
        self.kalshi_entry_aggr_c = config.ARB_KALSHI_ENTRY_AGGRESSION_CENTS
        self.exit_limit_only = config.ARB_EXIT_LIMIT_ONLY
        self.poly_exit_passive = config.ARB_POLY_EXIT_PASSIVE_OFFSET
        self.kalshi_exit_passive_c = config.ARB_KALSHI_EXIT_PASSIVE_OFFSET_CENTS
        self.emergency_stop = False
        self.emergency_reason: str | None = None
        self._stop_loss_cooldowns: dict[str, float] = {}

    def entry_block_reason(self, kalshi_ticker: str, direction: str) -> str | None:
        cooldown_key = f"{kalshi_ticker}:{direction}"
        cooldown_until = self._stop_loss_cooldowns.get(cooldown_key, 0)
        remaining = cooldown_until - time.time()
        if remaining > 0:
            return f"post-exit cooldown ({max(1, math.ceil(remaining))}s remaining)"
        return None

    def preflight_check(self) -> tuple[bool, list[str]]:
        """
        Validate safety preconditions before live trading.
        Dry-run always passes.
        """
        issues: list[str] = []
        if self.dry_run:
            return True, issues

        if not config.live_mode_requested():
            issues.append("live mode requested but ARB_ENABLE_LIVE is false")
        if not config.live_mode_armed():
            issues.append("live mode not armed: set ARB_DRY_RUN=false and ARB_ENABLE_LIVE=true")
        if not config.ARB_REQUIRE_BALANCE_CHECK:
            return len(issues) == 0, issues

        try:
            bal = self.kalshi.get_balance()
            kalshi_usd = float(bal.get("balance", 0)) / 100.0
            logger.info("Kalshi preflight: balance=$%.2f", kalshi_usd)
            if kalshi_usd < config.ARB_MIN_KALSHI_BALANCE_USD:
                issues.append(
                    f"kalshi balance ${kalshi_usd:.2f} < ${config.ARB_MIN_KALSHI_BALANCE_USD:.2f}"
                )
        except Exception as e:
            issues.append(f"kalshi balance check failed: {e}")

        try:
            poly_usdc, poly_details = self.poly.get_usdc_balance_details()
            logger.info(
                "Polymarket preflight: funder=%s host=%s raw_type=%s raw_keys=%s balance=%s",
                poly_details.get("funder"),
                poly_details.get("host"),
                poly_details.get("raw_type"),
                poly_details.get("raw_keys"),
                "unavailable" if poly_usdc is None else f"${poly_usdc:.2f}",
            )
            if poly_usdc is None:
                extra = f" (funder {poly_details.get('funder')})"
                if poly_details.get("error"):
                    extra = f"{extra}: {poly_details.get('error')}"
                issues.append(f"polymarket balance unavailable{extra}")
            elif poly_usdc < config.ARB_MIN_POLY_BALANCE_USD:
                issues.append(
                    f"polymarket balance ${poly_usdc:.2f} < ${config.ARB_MIN_POLY_BALANCE_USD:.2f} "
                    f"(funder {poly_details.get('funder')})"
                )
        except Exception as e:
            issues.append(f"polymarket balance check failed: {e}")

        return len(issues) == 0, issues

    # -- Sizing ----------------------------------------------------------------

    def _compute_entry_size(self, opp: SpreadOpportunity) -> int:
        cost_per = opp.entry_cost_per_contract
        if cost_per <= 0:
            return 0

        max_by_pos = int(self.max_position_usd / cost_per)

        self.ledger.reset_daily_if_needed()
        remaining = self.max_daily_spend - self.ledger.daily_spent
        max_by_daily = int(remaining / cost_per)

        contracts = min(max_by_pos, max_by_daily)

        # Platform minimums
        poly_price = opp.cheap_yes_price if opp.cheap_yes_platform == "polymarket" else opp.expensive_no_price
        if poly_price > 0 and contracts * poly_price < 1.0:
            contracts = max(contracts, math.ceil(1.0 / poly_price))
        contracts = max(contracts, 5)

        total = contracts * cost_per
        if total > remaining or total > self.max_position_usd:
            logger.warning("Size after minimums ($%.2f) exceeds limits", total)
            return 0

        return contracts

    # -- Entry -----------------------------------------------------------------

    def enter(self, opp: SpreadOpportunity) -> TradeResult:
        """Open a new spread position by buying both legs."""
        if self.emergency_stop:
            return TradeResult(
                action="entry",
                timestamp=time.time(),
                dry_run=self.dry_run,
                poly_error=self.emergency_reason or "emergency stop active",
                kalshi_error=self.emergency_reason or "emergency stop active",
            )

        if self.positions.has_open_position(opp.pair.kalshi_ticker, opp.direction.value):
            return TradeResult(
                action="entry",
                timestamp=time.time(),
                dry_run=self.dry_run,
                poly_error="matching position already open",
                kalshi_error="matching position already open",
            )

        if len(self.positions.positions) >= config.ARB_MAX_OPEN_POSITIONS:
            return TradeResult(
                action="entry",
                timestamp=time.time(),
                dry_run=self.dry_run,
                poly_error=f"max open positions reached ({config.ARB_MAX_OPEN_POSITIONS})",
                kalshi_error=f"max open positions reached ({config.ARB_MAX_OPEN_POSITIONS})",
            )

        msg = self.entry_block_reason(opp.pair.kalshi_ticker, opp.direction.value)
        if msg:
            logger.info("ENTER blocked: %s — %s", opp.pair.label, msg)
            return TradeResult(
                action="entry",
                timestamp=time.time(),
                dry_run=self.dry_run,
                poly_error=msg,
                kalshi_error=msg,
            )

        contracts = self._compute_entry_size(opp)
        result = TradeResult(
            action="entry", timestamp=time.time(), dry_run=self.dry_run,
            contracts=contracts, total_cost_usd=contracts * opp.entry_cost_per_contract,
        )

        if contracts == 0:
            result.poly_error = result.kalshi_error = "sizing returned 0"
            self._log_trade_result("entry", result, {"pair": opp.pair.label})
            return result

        signal_age = time.time() - opp.snapshot.timestamp
        if signal_age > config.ARB_MAX_SIGNAL_AGE_SECONDS:
            result.poly_error = f"stale signal ({signal_age:.2f}s)"
            result.kalshi_error = result.poly_error
            self._log_trade_result("entry", result, {
                "pair": opp.pair.label,
                "signal_age_s": round(signal_age, 3),
            })
            return result

        label = opp.pair.label
        logger.info("ENTER: %s | %s | %d contracts | spread %.4f",
                     label, opp.direction.value, contracts, opp.spread_width)

        if not self.dry_run:
            ok, issues = self.preflight_check()
            if not ok:
                msg = "; ".join(issues)
                result.poly_error = msg
                result.kalshi_error = msg
                self._log_trade_result("entry", result, {"pair": label, "direction": opp.direction.value})
                return result

        if self.dry_run:
            print(f"  [DRY RUN] ENTER {contracts} contracts on {label}")
            print(f"    Buy YES @ {opp.cheap_yes_price:.4f} on {opp.cheap_yes_platform}")
            print(f"    Buy NO  @ {opp.expensive_no_price:.4f} on {opp.expensive_no_platform}")
            print(f"    Spread: {opp.spread_width:.4f} | Cost: ${result.total_cost_usd:.2f}")
            result.poly_success = result.kalshi_success = True
            self.ledger.daily_spent += result.total_cost_usd
            self.positions.open_position(opp, contracts, result.total_cost_usd)
            self.ledger.entries.append(result)
            self._log_trade_result("entry", result, {
                "pair": label,
                "direction": opp.direction.value,
                "spread": opp.spread_width,
                "net_edge": opp.net_edge,
                "mode": "dry_run",
            })
            return result

        # Determine order params for each platform
        poly_token, poly_price, kalshi_side, kalshi_price_cents = self._entry_params(opp)
        poly_price = self._entry_poly_price(opp, poly_price)
        kalshi_price_cents = self._entry_kalshi_price(opp, kalshi_side, kalshi_price_cents)

        # Place Polymarket leg
        try:
            poly_result = self._place_poly_with_reprice(
                side="buy",
                token_id=poly_token,
                start_price=poly_price,
                size=float(contracts),
            )
            if isinstance(poly_result, dict) and poly_result.get("success") is False:
                result.poly_error = poly_result.get("errorMsg") or str(poly_result)
                result.poly_status = str(poly_result.get("status", "failed"))
            else:
                if isinstance(poly_result, dict):
                    result.poly_order_id = (
                        poly_result.get("orderID")
                        or poly_result.get("id")
                        or (poly_result.get("order") or {}).get("id")
                    )
                    result.poly_status = str(poly_result.get("status", "posted"))
                (
                    result.poly_success,
                    result.poly_partial,
                    result.poly_status,
                    result.poly_error,
                ) = self._wait_for_poly_entry_fill(result.poly_order_id, result.poly_status)
        except Exception as e:
            result.poly_error = str(e)
            logger.error("Poly entry failed: %s", e)

        # Place Kalshi leg
        try:
            order = self.kalshi.create_order(
                ticker=opp.pair.kalshi_ticker,
                side=kalshi_side,
                action="buy",
                count=contracts,
                yes_price=kalshi_price_cents if kalshi_side == "yes" else None,
                no_price=kalshi_price_cents if kalshi_side == "no" else None,
                client_order_id=f"arb-e-{uuid.uuid4().hex[:8]}",
            )
            result.kalshi_order_id = order.order_id
            (
                result.kalshi_success,
                result.kalshi_partial,
                result.kalshi_status,
            ) = self._wait_for_kalshi_entry_fill(order.order_id, order.status)
            if not result.kalshi_success:
                result.kalshi_error = (
                    f"partial fill ({result.kalshi_status})"
                    if result.kalshi_partial
                    else f"unfilled ({result.kalshi_status})"
                )
        except Exception as e:
            result.kalshi_error = str(e)
            logger.error("Kalshi entry failed: %s", e)

        if result.both_filled:
            self.ledger.daily_spent += result.total_cost_usd
            self.positions.open_position(opp, contracts, result.total_cost_usd)
        elif result.one_leg_only or result.poly_partial or result.kalshi_partial:
            self._warn_partial("ENTRY", label, result)
            if not self.allow_partials:
                self._handle_entry_partial(opp, contracts, result)

        self.ledger.entries.append(result)
        self._log_trade_result("entry", result, {
            "pair": label,
            "direction": opp.direction.value,
            "spread": opp.spread_width,
            "net_edge": opp.net_edge,
            "mode": "live",
            "poly_price": poly_price,
            "kalshi_price_cents": kalshi_price_cents,
        })
        return result

    # -- Exit ------------------------------------------------------------------

    def exit(self, pos: ArbPosition, reason: str = "manual") -> TradeResult:
        """Close an open spread position by selling both legs."""
        if pos.status == "stuck_exit":
            return TradeResult(
                action="exit",
                timestamp=time.time(),
                dry_run=self.dry_run,
                contracts=pos.contracts,
                poly_error="position stuck — manual intervention required",
                kalshi_error="position stuck — manual intervention required",
            )

        result = TradeResult(
            action="exit", timestamp=time.time(), dry_run=self.dry_run,
            contracts=pos.contracts,
        )

        label = pos.pair_label
        logger.info("EXIT [%s]: %s | %d contracts | spread %.4f -> %.4f",
                     reason, label, pos.contracts, pos.entry_spread, pos.current_spread)

        if self.dry_run:
            print(f"  [DRY RUN] EXIT {pos.contracts} contracts on {label} (reason: {reason})")
            print(f"    Entry spread: {pos.entry_spread:.4f} -> Current: {pos.current_spread:.4f}")
            print(f"    Compression: {pos.spread_compression_pct*100:.0f}%")
            print(f"    Unrealized P&L: ${pos.unrealized_pnl:+.2f}")
            result.poly_success = result.kalshi_success = True
            self.positions.close_position(pos.id, pos.unrealized_pnl, reason=reason)
            self.ledger.exits.append(result)
            self._log_trade_result("exit", result, {
                "pair": label,
                "reason": reason,
                "mode": "dry_run",
                "entry_spread": pos.entry_spread,
                "current_spread": pos.current_spread,
            })
            return result

        # Sell both legs
        poly_token, kalshi_side = self._exit_params(pos)

        # Sell on Polymarket with escalation if passive exits do not fill.
        try:
            poly_success, poly_meta = self._exit_poly_with_escalation(poly_token, pos.contracts)
            result.poly_success = poly_success
            result.poly_order_id = poly_meta.get("order_id")
            result.poly_status = poly_meta.get("status")
            result.poly_error = poly_meta.get("error")
        except Exception as e:
            result.poly_error = str(e)
            logger.error("Poly exit failed: %s", e)

        # Sell on Kalshi with escalation if passive exits do not fill.
        try:
            kalshi_success, kalshi_meta = self._exit_kalshi_with_escalation(pos.kalshi_ticker, kalshi_side, pos.contracts)
            result.kalshi_success = kalshi_success
            result.kalshi_order_id = kalshi_meta.get("order_id")
            result.kalshi_status = kalshi_meta.get("status")
            result.kalshi_error = kalshi_meta.get("error")
        except Exception as e:
            result.kalshi_error = str(e)
            logger.error("Kalshi exit failed: %s", e)

        if result.both_filled:
            self.positions.close_position(pos.id, pos.unrealized_pnl, reason=reason)
            cooldown_key = f"{pos.kalshi_ticker}:{pos.direction}"
            cooldown_sec = config.ARB_EXIT_COOLDOWN_SECONDS
            self._stop_loss_cooldowns[cooldown_key] = time.time() + cooldown_sec
            logger.info("Post-exit cooldown: %s blocked for %ds (reason: %s)", cooldown_key, cooldown_sec, reason)
        elif result.one_leg_only:
            self._warn_partial("EXIT", label, result)
            if not self.allow_partials:
                self._handle_exit_partial(pos, result)

        self.ledger.exits.append(result)
        self._log_trade_result("exit", result, {
            "pair": label,
            "reason": reason,
            "mode": "live",
            "entry_spread": pos.entry_spread,
            "current_spread": pos.current_spread,
            "unrealized_pnl": pos.unrealized_pnl,
        })
        return result

    # -- Helpers ---------------------------------------------------------------

    def _entry_params(self, opp: SpreadOpportunity):
        """Determine per-platform order params for entry."""
        if opp.direction == SpreadDirection.KALSHI_HIGHER:
            # Buy YES on Poly, buy NO on Kalshi
            poly_token = opp.pair.poly.token_yes
            poly_price = opp.cheap_yes_price
            kalshi_side = "no"
            kalshi_price_cents = int(round(opp.expensive_no_price * 100))
        else:
            # Buy YES on Kalshi, buy NO on Poly
            poly_token = opp.pair.poly.token_no
            poly_price = opp.expensive_no_price
            kalshi_side = "yes"
            kalshi_price_cents = int(round(opp.cheap_yes_price * 100))
        return poly_token, poly_price, kalshi_side, kalshi_price_cents

    def _exit_params(self, pos: ArbPosition):
        """Determine per-platform order params for exit."""
        if pos.direction == SpreadDirection.KALSHI_HIGHER.value:
            # We hold YES on Poly, NO on Kalshi -> sell YES on Poly, sell NO on Kalshi
            poly_token = pos.poly_token_yes
            kalshi_side = "no"
        else:
            # We hold YES on Kalshi, NO on Poly -> sell NO on Poly, sell YES on Kalshi
            poly_token = pos.poly_token_no
            kalshi_side = "yes"
        return poly_token, kalshi_side

    def _entry_poly_price(self, opp: SpreadOpportunity, fallback_price: float) -> float:
        if not self.entry_marketable:
            return max(0.01, min(0.99, fallback_price + self.poly_limit_offset))
        ask_ref = opp.snapshot.poly_yes_ask
        if opp.direction == SpreadDirection.POLY_HIGHER:
            ask_ref = opp.snapshot.poly_no_ask
        base = ask_ref if ask_ref is not None else fallback_price
        return max(0.01, min(0.99, base + self.poly_entry_aggr))

    def _entry_kalshi_price(self, opp: SpreadOpportunity, kalshi_side: str, fallback_cents: int) -> int:
        if not self.entry_marketable:
            return max(1, min(99, int(fallback_cents + self.kalshi_limit_offset_cents)))
        if kalshi_side == "yes":
            ask_ref = opp.snapshot.kalshi_yes_ask
        else:
            ask_ref = opp.snapshot.kalshi_no_ask
        if ask_ref is None:
            return max(1, min(99, int(fallback_cents + self.kalshi_entry_aggr_c)))
        return max(1, min(99, int(round(ask_ref * 100)) + self.kalshi_entry_aggr_c))

    def _exit_poly_limit_price(self, bid: float | None, ask: float | None) -> float:
        if self.exit_limit_only:
            base = bid if bid is not None else (ask if ask is not None else 0.01)
            return max(0.01, min(0.99, base + self.poly_exit_passive))
        # Start at the bid on the first attempt; the escalation loop in
        # _exit_poly_with_escalation subtracts attempt*0.01 on retries.
        base = bid if bid is not None else (ask if ask is not None else 0.01)
        return max(0.01, min(0.99, base))

    def _exit_kalshi_limit_prices(self, ticker: str, kalshi_side: str) -> tuple[int | None, int | None]:
        yes_price = None
        no_price = None
        try:
            ob = self.kalshi.get_orderbook(ticker)
            prices = _kalshi_book_to_prices(ob)
            if self.exit_limit_only:
                offset = self.kalshi_exit_passive_c
            else:
                # Start at bid; escalation loop handles below-bid repricing
                offset = 0
            if kalshi_side == "yes":
                bid_ref = prices.get("yes_bid")
                if bid_ref is not None:
                    px = int(round(bid_ref * 100))
                    yes_price = max(1, min(99, px + offset))
            else:
                bid_ref = prices.get("no_bid")
                if bid_ref is not None:
                    px = int(round(bid_ref * 100))
                    no_price = max(1, min(99, px + offset))
        except Exception:
            pass
        return yes_price, no_price

    def _exit_poly_with_escalation(self, token_id: str, contracts: int) -> tuple[bool, dict]:
        attempts = max(0, config.ARB_EXIT_REPRICE_ATTEMPTS)
        last_meta = {"error": "poly exit not attempted", "status": "failed", "order_id": None}

        self.poly.refresh_conditional_allowance(token_id)

        # Try the full amount first, then progressively smaller sizes.
        # Taker fees on entry mean we may have received fewer shares than
        # the nominal count, and the CLOB's balance cache can be stale on
        # top of that.  Starting at 1.0 avoids leaving residual positions
        # when the full sell actually goes through.
        size_factors = [1.0, _POLY_SELL_SIZE_FACTOR, 0.93, 0.90, 0.85]

        for size_factor in size_factors:
            sell_size = math.floor(contracts * size_factor * 1000) / 1000
            if sell_size <= 0:
                continue

            for attempt in range(attempts + 1):
                bid, ask = self.poly.get_best_prices(token_id)
                if attempt == attempts:
                    sell_price = 0.01
                else:
                    sell_price = self._exit_poly_limit_price(bid=bid, ask=ask)
                    sell_price = max(0.01, min(0.99, sell_price - (attempt * 0.01)))

                try:
                    poly_result = self.poly.sell(token_id, sell_price, sell_size)
                except Exception as e:
                    err_str = str(e)
                    if "not enough balance" in err_str:
                        logger.warning("Poly sell at %.0f%% (%s shares) balance error, reducing size", size_factor * 100, sell_size)
                        last_meta = {"error": err_str, "status": "failed", "order_id": None}
                        break  # try next smaller size
                    raise

                if isinstance(poly_result, dict) and poly_result.get("success") is False:
                    err_msg = poly_result.get("errorMsg") or str(poly_result)
                    if "not enough balance" in err_msg:
                        logger.warning("Poly sell at %.0f%% (%s shares) balance error, reducing size", size_factor * 100, sell_size)
                        last_meta = {"error": err_msg, "status": "failed", "order_id": None}
                        break  # try next smaller size
                    last_meta = {
                        "error": err_msg,
                        "status": str(poly_result.get("status", "failed")),
                        "order_id": None,
                    }
                    continue

                order_id = None
                status = "posted"
                if isinstance(poly_result, dict):
                    order_id = (
                        poly_result.get("orderID")
                        or poly_result.get("id")
                        or (poly_result.get("order") or {}).get("id")
                    )
                    status = str(poly_result.get("status", "posted"))

                if not order_id:
                    return True, {"order_id": None, "status": status, "error": None}

                filled, latest_status = self._wait_for_poly_fill(order_id)
                if filled:
                    return True, {"order_id": order_id, "status": latest_status, "error": None}

                try:
                    self.poly.cancel(order_id)
                except Exception:
                    pass

                last_meta = {
                    "error": f"unfilled after attempt {attempt + 1}",
                    "status": latest_status,
                    "order_id": order_id,
                }
            else:
                continue  # inner loop finished without break — no balance error
            continue  # broke out of inner loop — try next size factor

        return False, last_meta

    def _exit_kalshi_with_escalation(self, ticker: str, side: str, contracts: int) -> tuple[bool, dict]:
        attempts = max(0, config.ARB_EXIT_REPRICE_ATTEMPTS)
        last_meta = {"error": "kalshi exit not attempted", "status": "failed", "order_id": None}
        for attempt in range(attempts + 1):
            yes_price, no_price = self._exit_kalshi_limit_prices(ticker, side)
            if attempt == attempts:
                if side == "yes":
                    yes_price, no_price = 1, None
                else:
                    yes_price, no_price = None, 1
            elif attempt > 0:
                # Escalate: reduce price by 1c per retry attempt
                if side == "yes" and yes_price is not None:
                    yes_price = max(1, yes_price - attempt)
                elif side == "no" and no_price is not None:
                    no_price = max(1, no_price - attempt)
            order = self.kalshi.create_order(
                ticker=ticker,
                side=side,
                action="sell",
                count=contracts,
                yes_price=yes_price,
                no_price=no_price,
                client_order_id=f"arb-x-{uuid.uuid4().hex[:8]}",
            )
            if self._kalshi_order_is_filled(order.status):
                return True, {"order_id": order.order_id, "status": order.status, "error": None}

            filled, latest_status = self._wait_for_kalshi_fill(order.order_id)
            if filled:
                return True, {"order_id": order.order_id, "status": latest_status, "error": None}

            try:
                self.kalshi.cancel_order(order.order_id)
            except Exception:
                pass

            last_meta = {
                "error": f"unfilled after attempt {attempt + 1}",
                "status": latest_status,
                "order_id": order.order_id,
            }

        return False, last_meta

    def _wait_for_poly_fill(self, order_id: str) -> tuple[bool, str]:
        deadline = time.time() + max(1, config.ARB_EXIT_FILL_TIMEOUT_SECONDS)
        last_status = "posted"
        while time.time() < deadline:
            try:
                order = self.poly.get_order(order_id)
                last_status = str((order or {}).get("status", last_status)).lower()
                if last_status in {"filled", "matched", "executed"}:
                    return True, last_status
            except Exception:
                pass
            time.sleep(0.4)
        return False, last_status

    def _wait_for_poly_entry_fill(
        self,
        order_id: str | None,
        initial_status: str | None,
    ) -> tuple[bool, bool, str, str | None]:
        status = str(initial_status or "posted").lower()
        if status in {"filled", "matched", "executed", "complete", "completed"}:
            return True, False, status, None
        if not order_id:
            return False, False, status, f"missing order id ({status})"

        deadline = time.time() + max(1, config.ARB_ORDER_TIMEOUT_SECONDS)
        partial = False
        while time.time() < deadline:
            try:
                filled, is_partial, latest_status = self.poly.get_order_fill_state(order_id)
                status = latest_status
                partial = partial or is_partial
                if filled:
                    return True, False, latest_status, None
            except Exception:
                pass
            time.sleep(0.4)

        try:
            self.poly.cancel(order_id)
        except Exception:
            pass

        if partial:
            return False, True, status, f"partial fill ({status})"
        return False, False, status, f"unfilled ({status})"

    def _wait_for_kalshi_fill(self, order_id: str) -> tuple[bool, str]:
        deadline = time.time() + max(1, config.ARB_EXIT_FILL_TIMEOUT_SECONDS)
        last_status = "posted"
        while time.time() < deadline:
            try:
                order = self.kalshi.get_order(order_id)
                if order:
                    last_status = order.status
                    if self._kalshi_order_is_filled(last_status):
                        return True, last_status
            except Exception:
                pass
            time.sleep(0.4)
        return False, last_status

    def _wait_for_kalshi_entry_fill(
        self,
        order_id: str,
        initial_status: str | None,
    ) -> tuple[bool, bool, str]:
        status = (initial_status or "").lower()
        if self._kalshi_order_is_filled(status):
            return True, False, status

        deadline = time.time() + max(1, config.ARB_ORDER_TIMEOUT_SECONDS)
        partial = False
        while time.time() < deadline:
            try:
                order = self.kalshi.get_order(order_id)
                if order:
                    status = (order.status or "").lower()
                    if self._kalshi_order_is_filled(status):
                        return True, False, status
                    partial = partial or (order.fill_count or 0) > 0
            except Exception:
                pass
            time.sleep(0.4)

        try:
            self.kalshi.cancel_order(order_id)
        except Exception:
            pass
        return False, partial, status or "posted"

    def _kalshi_order_is_filled(self, status: str | None) -> bool:
        text = (status or "").lower()
        return text in {"filled", "executed", "completed"}

    def _log_trade_result(self, action: str, result: TradeResult, context: dict):
        try:
            log_execution({
                "action": action,
                "dry_run": result.dry_run,
                "contracts": result.contracts,
                "total_cost_usd": result.total_cost_usd,
                "poly_success": result.poly_success,
                "poly_error": result.poly_error,
                "poly_order_id": result.poly_order_id,
                "poly_status": result.poly_status,
                "poly_partial": result.poly_partial,
                "kalshi_success": result.kalshi_success,
                "kalshi_error": result.kalshi_error,
                "kalshi_order_id": result.kalshi_order_id,
                "kalshi_status": result.kalshi_status,
                "kalshi_partial": result.kalshi_partial,
                "context": context,
            })
        except Exception as e:
            logger.warning("Failed to write execution log: %s", e)

    def _place_poly_with_reprice(
        self,
        side: str,
        token_id: str,
        start_price: float,
        size: float,
    ) -> dict:
        """
        Place a Polymarket order with optional repricing attempts.
        """
        attempts = max(0, config.ARB_ORDER_REPRICE_ATTEMPTS)
        price = start_price
        last_result: dict = {}
        for attempt in range(attempts + 1):
            if side == "buy":
                last_result = self.poly.buy(token_id, price, size)
            else:
                last_result = self.poly.sell(token_id, price, size)

            if not (isinstance(last_result, dict) and last_result.get("success") is False):
                return last_result

            if attempt >= attempts:
                return last_result

            step = 0.005
            if side == "buy":
                price = max(0.01, min(0.99, price + step))
            else:
                price = max(0.01, min(0.99, price - step))
            time.sleep(min(1, config.ARB_ORDER_TIMEOUT_SECONDS))
        return last_result

    def _warn_partial(self, action: str, label: str, result: TradeResult):
        logger.warning("PARTIAL %s on %s - manual intervention may be needed", action, label)
        print(f"  *** WARNING: Partial {action} on {label} ***")
        print(f"      Poly: {'OK' if result.poly_success else result.poly_error}")
        print(f"      Kalshi: {'OK' if result.kalshi_success else result.kalshi_error}")

    def _trip_emergency_stop(self, reason: str):
        self.emergency_stop = True
        self.emergency_reason = reason
        logger.error("EMERGENCY STOP: %s", reason)
        print(f"  *** EMERGENCY STOP: {reason} ***")
        print("      New entries are disabled until the process is restarted.")

    def _handle_entry_partial(self, opp: SpreadOpportunity, contracts: int, result: TradeResult):
        logger.warning("Partials disabled; attempting emergency flatten for entry on %s", opp.pair.label)
        if result.poly_success and not result.kalshi_success:
            poly_token, _, _, _ = self._entry_params(opp)
            self._emergency_flatten_poly(poly_token, contracts)
        elif result.kalshi_success and not result.poly_success:
            _, _, kalshi_side, _ = self._entry_params(opp)
            self._emergency_flatten_kalshi(opp.pair.kalshi_ticker, kalshi_side, contracts)
        self._trip_emergency_stop(f"partial entry on {opp.pair.label}")

    def _handle_exit_partial(self, pos: ArbPosition, result: TradeResult):
        logger.warning("Partials disabled; attempting emergency flatten for exit on %s", pos.pair_label)
        poly_token, kalshi_side = self._exit_params(pos)
        if result.poly_success and not result.kalshi_success:
            self._emergency_flatten_kalshi(pos.kalshi_ticker, kalshi_side, pos.contracts)
        elif result.kalshi_success and not result.poly_success:
            self._emergency_flatten_poly(poly_token, pos.contracts)
        pos.status = "stuck_exit"
        self._trip_emergency_stop(f"partial exit on {pos.pair_label}")

    def _emergency_flatten_poly(self, token_id: str, contracts: int):
        self.poly.refresh_conditional_allowance(token_id)
        # Try progressively smaller sizes.  The CLOB's cached balance can
        # be significantly stale after instant-match buys, and taker fees
        # mean we received fewer shares than the nominal contract count.
        for factor in (0.95, 0.90, 0.85, 0.80):
            sell_size = math.floor(contracts * factor * 1000) / 1000
            if sell_size <= 0:
                continue
            try:
                self.poly.sell(token_id, 0.01, sell_size)
                logger.warning("Emergency Poly flatten submitted: token=%s size=%s price=0.01", token_id, sell_size)
                print(f"      Emergency Poly flatten submitted @ $0.01 ({sell_size} shares)")
                return
            except Exception as e:
                logger.warning("Emergency Poly flatten at %.0f%% (%s shares) failed: %s", factor * 100, sell_size, e)
        logger.error("Emergency Poly flatten exhausted all size attempts")
        print("      Emergency Poly flatten failed: exhausted all size attempts")

    def _emergency_flatten_kalshi(self, ticker: str, side: str, contracts: int):
        try:
            yes_price = no_price = None
            if side == "yes":
                yes_price = 1
            else:
                no_price = 1
            self.kalshi.create_order(
                ticker=ticker,
                side=side,
                action="sell",
                count=contracts,
                yes_price=yes_price,
                no_price=no_price,
                client_order_id=f"arb-emerg-{uuid.uuid4().hex[:8]}",
            )
            logger.warning("Emergency Kalshi flatten submitted: ticker=%s side=%s count=%s price=1c", ticker, side, contracts)
            print(f"      Emergency Kalshi flatten submitted on {ticker} ({side}) @ 1c")
        except Exception as e:
            logger.error("Emergency Kalshi flatten failed: %s", e)
            print(f"      Emergency Kalshi flatten failed: {e}")

    def print_ledger(self):
        l = self.ledger
        entries_ok = sum(1 for t in l.entries if t.both_filled)
        exits_ok = sum(1 for t in l.exits if t.both_filled)
        print(f"\n  Session Ledger")
        print(f"  --------------------------------")
        print(f"  Entries:  {entries_ok}/{len(l.entries)} successful")
        print(f"  Exits:    {exits_ok}/{len(l.exits)} successful")
        print(f"  Daily spent: ${l.daily_spent:.2f} / ${self.max_daily_spend:.2f}")
        print(f"  Open positions: {len(self.positions.positions)}")
