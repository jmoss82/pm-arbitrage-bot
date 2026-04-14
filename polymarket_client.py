"""
Polymarket client wrapper — unifies CLOB order execution, Gamma market lookup,
and Data API queries behind a single interface for the arbitrage engine.
"""
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp
import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    OrderArgs,
    BalanceAllowanceParams,
    AssetType,
    OrderType,
)

import config

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"


def _mask_value(value: str | None, prefix: int = 6, suffix: int = 4) -> str:
    if not value:
        return "(missing)"
    if len(value) <= prefix + suffix:
        return value
    return f"{value[:prefix]}...{value[-suffix:]}"


@dataclass
class PolymarketMarket:
    condition_id: str = ""
    question_id: str = ""
    question: str = ""
    slug: str = ""
    token_yes: str = ""
    token_no: str = ""
    price_yes: float | None = None
    price_no: float | None = None
    volume: float | None = None
    liquidity: float | None = None
    active: bool = False
    closed: bool = False
    accepting_orders: bool = False
    neg_risk: bool = False
    end_date: str | None = None
    category: str | None = None
    _raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_gamma(cls, d: dict) -> "PolymarketMarket":
        clob_ids = json.loads(d.get("clobTokenIds", "[]"))
        outcome_prices = json.loads(d.get("outcomePrices", "[]"))
        return cls(
            condition_id=d.get("conditionId", ""),
            question_id=d.get("questionID", ""),
            question=d.get("question", ""),
            slug=d.get("slug", ""),
            token_yes=clob_ids[0] if len(clob_ids) > 0 else "",
            token_no=clob_ids[1] if len(clob_ids) > 1 else "",
            price_yes=float(outcome_prices[0]) if len(outcome_prices) > 0 else None,
            price_no=float(outcome_prices[1]) if len(outcome_prices) > 1 else None,
            volume=d.get("volumeNum"),
            liquidity=d.get("liquidityNum"),
            active=bool(d.get("active")),
            closed=bool(d.get("closed")),
            accepting_orders=bool(d.get("acceptingOrders")),
            neg_risk=bool(d.get("negRisk")),
            end_date=d.get("endDate"),
            category=d.get("groupItemTitle") or d.get("category"),
            _raw=d,
        )


class PolymarketClient:
    QUOTE_RETRIES = 2
    QUOTE_RETRY_DELAY_SECONDS = 0.15

    def __init__(self, derive_keys: bool = True):
        self.clob = self._init_clob(derive_keys)
        self._fee_rate_cache_bps: dict[str, int] = {}

    def _init_clob(self, derive_keys: bool) -> ClobClient:
        explicit_creds_available = all(
            [
                config.POLY_API_KEY,
                config.POLY_API_SECRET,
                config.POLY_API_PASSPHRASE,
            ]
        )

        if explicit_creds_available:
            logger.info("Using configured Polymarket API credentials.")
            creds = ApiCreds(
                api_key=config.POLY_API_KEY,
                api_secret=config.POLY_API_SECRET,
                api_passphrase=config.POLY_API_PASSPHRASE,
            )
        elif derive_keys and config.POLY_PRIVATE_KEY:
            logger.info("Deriving Polymarket API credentials (IP-bound)...")
            l1_client = ClobClient(
                host=config.CLOB_HOST,
                chain_id=config.CHAIN_ID,
                key=config.POLY_PRIVATE_KEY,
                signature_type=2,
            )
            raw_creds = l1_client.derive_api_key()

            if isinstance(raw_creds, dict):
                api_key = raw_creds.get("apiKey") or raw_creds.get("api_key")
                api_secret = raw_creds.get("secret") or raw_creds.get("api_secret")
                api_passphrase = raw_creds.get("passphrase") or raw_creds.get("api_passphrase")
            else:
                api_key = raw_creds.api_key
                api_secret = raw_creds.api_secret
                api_passphrase = raw_creds.api_passphrase

            logger.info("Polymarket API key derived: %s...", api_key[:16])
            creds = ApiCreds(api_key, api_secret, api_passphrase)
        else:
            logger.info("Using configured Polymarket API credentials.")
            creds = ApiCreds(
                api_key=config.POLY_API_KEY,
                api_secret=config.POLY_API_SECRET,
                api_passphrase=config.POLY_API_PASSPHRASE,
            )

        client = ClobClient(
            config.CLOB_HOST,
            key=config.POLY_PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            signature_type=2,
            funder=config.POLY_FUNDER,
            creds=creds,
        )
        return client

    # ── Market Discovery (Gamma API — async) ─────────────────────────────

    @staticmethod
    async def fetch_active_markets(
        session: aiohttp.ClientSession,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PolymarketMarket]:
        """Fetch active, non-closed markets from the Gamma API."""
        params = {
            "limit": limit,
            "offset": offset,
            "active": "true",
            "closed": "false",
        }
        async with session.get(f"{GAMMA_API}/markets", params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return [PolymarketMarket.from_gamma(m) for m in data if m.get("enableOrderBook")]

    @staticmethod
    async def fetch_market_by_slug(session: aiohttp.ClientSession, slug: str) -> PolymarketMarket | None:
        async with session.get(f"{GAMMA_API}/markets", params={"slug": slug}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return PolymarketMarket.from_gamma(data[0]) if data else None

    @staticmethod
    async def fetch_market_by_id(session: aiohttp.ClientSession, condition_id: str) -> PolymarketMarket | None:
        async with session.get(f"{GAMMA_API}/markets", params={"id": condition_id}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return PolymarketMarket.from_gamma(data[0]) if data else None

    @staticmethod
    async def search_markets(session: aiohttp.ClientSession, query: str, limit: int = 50) -> list[PolymarketMarket]:
        """
        Search Gamma API. Returns markets whose question matches the query.
        Gamma doesn't have a native text search, so we fetch by volume and filter.
        """
        all_markets: list[PolymarketMarket] = []
        q_lower = query.lower()

        for offset in range(0, 500, 100):
            batch = await PolymarketClient.fetch_active_markets(session, limit=100, offset=offset)
            if not batch:
                break
            for m in batch:
                if q_lower in m.question.lower():
                    all_markets.append(m)
                    if len(all_markets) >= limit:
                        return all_markets
        return all_markets

    @staticmethod
    async def fetch_all_active_markets(session: aiohttp.ClientSession, max_pages: int = 10) -> list[PolymarketMarket]:
        """Fetch up to max_pages * 100 active markets sorted by volume."""
        all_markets: list[PolymarketMarket] = []
        for page in range(max_pages):
            batch = await PolymarketClient.fetch_active_markets(session, limit=100, offset=page * 100)
            if not batch:
                break
            all_markets.extend(batch)
            logger.debug("Fetched page %d: %d markets (total %d)", page, len(batch), len(all_markets))
        return all_markets

    # ── Order Book (CLOB — sync, via py_clob_client) ─────────────────────

    def get_orderbook(self, token_id: str) -> dict:
        """Fetch CLOB order book for a given token_id. Returns raw dict."""
        return self.clob.get_order_book(token_id)

    def get_midpoint(self, token_id: str) -> float | None:
        """Get midpoint price for a token. Returns decimal (0-1)."""
        try:
            mp = self.clob.get_midpoint(token_id)
            if isinstance(mp, dict):
                return float(mp.get("mid", 0)) or None
            return float(mp) if mp else None
        except Exception:
            return None

    def get_best_prices(
        self,
        token_id: str,
        allow_midpoint_fallback: bool = True,
    ) -> tuple[float | None, float | None]:
        """
        Return (best_bid, best_ask) for a token.

        Many Polymarket markets (especially sports) have sparse standing order
        books with real depth only at extreme prices.  Market makers fill
        orders just-in-time instead of posting visible quotes.

        Strategy: read the raw order book first.  If the standing book is
        too sparse (spread > 20%) or entirely empty, use the CLOB midpoint
        as a fallback reference.  When ``allow_midpoint_fallback`` is False
        the midpoint is still used as a last resort to avoid returning None
        (which would silently block spread detection), but raw book prices
        are always preferred.
        """
        last_error = None
        for attempt in range(self.QUOTE_RETRIES + 1):
            try:
                book = self.get_orderbook(token_id)
                bids = book.bids if hasattr(book, "bids") else book.get("bids", [])
                asks = book.asks if hasattr(book, "asks") else book.get("asks", [])

                best_bid = None
                best_ask = None

                if bids:
                    bid_prices = [
                        float(level.price if hasattr(level, "price") else level["price"])
                        for level in bids
                    ]
                    best_bid = max(bid_prices) if bid_prices else None
                if asks:
                    ask_prices = [
                        float(level.price if hasattr(level, "price") else level["price"])
                        for level in asks
                    ]
                    best_ask = min(ask_prices) if ask_prices else None

                if best_bid is not None and best_ask is not None:
                    book_spread = best_ask - best_bid
                    if allow_midpoint_fallback and book_spread > 0.20:
                        mid = self.get_midpoint(token_id)
                        if mid is not None:
                            best_bid = mid - 0.005
                            best_ask = mid + 0.005
                    return best_bid, best_ask

                # Book is partially or fully empty — use midpoint as
                # last-resort fallback regardless of allow_midpoint_fallback
                # so that spread detection still has a reference price.
                if best_bid is None or best_ask is None:
                    mid = self.get_midpoint(token_id)
                    if mid is not None:
                        if best_bid is None:
                            best_bid = mid - 0.005
                        if best_ask is None:
                            best_ask = mid + 0.005

                return best_bid, best_ask
            except Exception as e:
                last_error = e
                if attempt < self.QUOTE_RETRIES:
                    time.sleep(self.QUOTE_RETRY_DELAY_SECONDS * (attempt + 1))
                else:
                    logger.warning("Failed to get prices for %s: %s", token_id[:12], e)
        return None, None

    def get_market_quotes(
        self,
        token_yes: str,
        token_no: str,
        allow_midpoint_fallback: bool = True,
    ) -> dict[str, float | None]:
        """Return best bid/ask quotes for both outcome tokens."""
        yes_bid, yes_ask = self.get_best_prices(
            token_yes,
            allow_midpoint_fallback=allow_midpoint_fallback,
        )
        no_bid, no_ask = self.get_best_prices(
            token_no,
            allow_midpoint_fallback=allow_midpoint_fallback,
        )
        return {
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
        }

    # ── Order Execution (CLOB — sync) ────────────────────────────────────

    def get_fee_rate_bps(self, token_id: str) -> int:
        """Return the token's taker fee rate in basis points."""
        cached = self._fee_rate_cache_bps.get(token_id)
        if cached is not None:
            return cached

        raw = None
        try:
            if hasattr(self.clob, "get_fee_rate_bps"):
                raw = self.clob.get_fee_rate_bps(token_id)
            elif hasattr(self.clob, "get_fee_rate"):
                raw = self.clob.get_fee_rate(token_id)
        except Exception as e:
            logger.debug("SDK fee lookup failed for %s: %s", token_id[:12], e)

        if raw is None:
            resp = requests.get(
                f"{config.CLOB_HOST}/fee-rate",
                params={"token_id": token_id},
                timeout=5,
            )
            resp.raise_for_status()
            raw = resp.json()

        if isinstance(raw, dict):
            value = (
                raw.get("fee_rate_bps")
                or raw.get("feeRateBps")
                or raw.get("fee_rate")
                or raw.get("feeRate")
            )
        else:
            value = raw

        bps = int(round(float(value)))
        self._fee_rate_cache_bps[token_id] = bps
        return bps

    def get_fee_rate(self, token_id: str) -> float:
        return self.get_fee_rate_bps(token_id) / 10_000.0

    def buy(self, token_id: str, price: float, size: float) -> dict:
        return self.clob.create_and_post_order(
            OrderArgs(token_id=token_id, price=price, size=size, side="BUY")
        )

    def sell(self, token_id: str, price: float, size: float) -> dict:
        return self.clob.create_and_post_order(
            OrderArgs(token_id=token_id, price=price, size=size, side="SELL")
        )

    def refresh_conditional_allowance(self, token_id: str):
        """Nudge the CLOB's cached conditional-token allowance for this token.

        Polymarket's CLOB has a known server-side balance cache bug where
        instantly-matched buys don't update the cache fast enough, causing
        subsequent sells at full size to fail with 'not enough balance /
        allowance'.  Calling update_balance_allowance(CONDITIONAL) before a
        sell forces a cache refresh and significantly improves reliability.
        See: https://github.com/Polymarket/py-clob-client/issues/287
        """
        try:
            self.clob.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
            )
        except Exception as e:
            logger.debug("update_balance_allowance(CONDITIONAL) for %s: %s", token_id[:12], e)

    def cancel(self, order_id: str):
        return self.clob.cancel(order_id)

    def get_order(self, order_id: str) -> dict:
        return self.clob.get_order(order_id)

    def get_order_avg_fill_price(self, order_id: str) -> float | None:
        """Return the average fill price for a filled order, or None."""
        order = self.get_order(order_id) or {}
        avg = order.get("average_price") or order.get("averagePrice")
        if avg is not None:
            try:
                return float(avg)
            except (TypeError, ValueError):
                pass
        return None

    def get_order_fill_state(self, order_id: str) -> tuple[bool, bool, str]:
        """Return (filled, partial, normalized_status) for an order."""
        order = self.get_order(order_id) or {}
        status = str(order.get("status", "")).lower()
        size_matched = order.get("size_matched") or order.get("matchedSize") or order.get("filled_size")
        original_size = order.get("original_size") or order.get("size")

        try:
            matched = float(size_matched) if size_matched is not None else 0.0
        except (TypeError, ValueError):
            matched = 0.0
        try:
            total = float(original_size) if original_size is not None else 0.0
        except (TypeError, ValueError):
            total = 0.0

        if status in {"filled", "matched", "executed", "complete", "completed"}:
            return True, False, status
        if status in {"partially_filled", "partial", "partially matched"}:
            return False, True, status
        if total > 0 and matched >= total:
            return True, False, status or "filled"
        if matched > 0:
            return False, True, status or "partial"
        return False, False, status or "posted"

    # ── Balance / Positions ──────────────────────────────────────────────

    def get_usdc_balance_details(self) -> tuple[float | None, dict]:
        """Return parsed USDC balance plus safe diagnostics about the query."""
        details = {
            "funder": _mask_value(config.POLY_FUNDER),
            "host": config.CLOB_HOST,
            "raw_type": None,
            "raw_keys": [],
        }
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal = self.clob.get_balance_allowance(params)
            details["raw_type"] = type(bal).__name__
            if isinstance(bal, dict):
                details["raw_keys"] = sorted(str(k) for k in bal.keys())
                return float(bal.get("balance", "0")) / 1e6, details
            return None, details
        except Exception as e:
            details["error"] = str(e)
            logger.warning("Failed to get USDC balance for funder %s: %s", details["funder"], e)
            return None, details

    def get_usdc_balance(self) -> float | None:
        """Return USDC balance in human units."""
        balance, _ = self.get_usdc_balance_details()
        return balance

    @staticmethod
    async def fetch_positions(session: aiohttp.ClientSession, wallet: str) -> list[dict]:
        """Fetch current positions from the Data API."""
        url = f"{DATA_API}/positions"
        params = {"user": wallet, "limit": 500, "sizeThreshold": 0}
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data if isinstance(data, list) else data.get("positions", [])
