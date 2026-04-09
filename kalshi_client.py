"""
Kalshi Trade API v2 client — Python port of the Rust kalshi-mm-bot client.

Handles RSA-PSS request signing, paginated endpoints, and retry logic.
"""
import time
import base64
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlencode

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import config

logger = logging.getLogger(__name__)


def _load_private_key(pem_text: str) -> rsa.RSAPrivateKey:
    return serialization.load_pem_private_key(pem_text.encode(), password=None)


def _sign_request(method: str, path: str, api_key_id: str, pem_text: str) -> dict[str, str]:
    """
    Produce Kalshi auth headers.

    Signing message = f"{timestamp_ms}{METHOD}{path_without_query}"
    Signature = RSA-PSS(SHA-256) → base64.
    """
    timestamp_ms = int(time.time() * 1000)
    path_no_query = path.split("?")[0]
    message = f"{timestamp_ms}{method.upper()}{path_no_query}"

    private_key = _load_private_key(pem_text)
    signature = private_key.sign(
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )

    return {
        "kalshi-access-key": api_key_id,
        "kalshi-access-signature": base64.b64encode(signature).decode(),
        "kalshi-access-timestamp": str(timestamp_ms),
    }


# ── Response types (plain dataclasses for easy attribute access) ──────────

@dataclass
class Market:
    ticker: str = ""
    status: str = ""
    title: str | None = None
    subtitle: str | None = None
    event_ticker: str | None = None
    series_ticker: str | None = None
    close_time: str | None = None
    volume: int | None = None
    volume_24h: int | None = None
    open_interest: int | None = None
    yes_bid: int | None = None
    yes_ask: int | None = None
    last_price: int | None = None
    category: str | None = None
    _raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Market":
        return cls(
            ticker=d.get("ticker", ""),
            status=d.get("status", ""),
            title=d.get("title"),
            subtitle=d.get("subtitle"),
            event_ticker=d.get("event_ticker"),
            series_ticker=d.get("series_ticker"),
            close_time=d.get("close_time"),
            volume=d.get("volume"),
            volume_24h=d.get("volume_24h"),
            open_interest=d.get("open_interest"),
            yes_bid=d.get("yes_bid"),
            yes_ask=d.get("yes_ask"),
            last_price=d.get("last_price"),
            category=d.get("category"),
            _raw=d,
        )


@dataclass
class Event:
    event_ticker: str = ""
    series_ticker: str | None = None
    title: str | None = None
    category: str | None = None
    status: str | None = None
    mutually_exclusive: bool | None = None
    markets: list[Market] = field(default_factory=list)
    _raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        markets = [Market.from_dict(m) for m in d.get("markets", [])]
        return cls(
            event_ticker=d.get("event_ticker", ""),
            series_ticker=d.get("series_ticker"),
            title=d.get("title"),
            category=d.get("category"),
            status=d.get("status"),
            mutually_exclusive=d.get("mutually_exclusive"),
            markets=markets,
            _raw=d,
        )


@dataclass
class OrderbookLevel:
    price: int
    quantity: int


@dataclass
class Orderbook:
    yes: list[OrderbookLevel] = field(default_factory=list)
    no: list[OrderbookLevel] = field(default_factory=list)


@dataclass
class Order:
    order_id: str = ""
    ticker: str = ""
    side: str = ""
    action: str = ""
    order_type: str = ""
    status: str = ""
    yes_price: int | None = None
    no_price: int | None = None
    fill_count: int = 0
    remaining_count: int = 0
    initial_count: int = 0
    client_order_id: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        return cls(
            order_id=d.get("order_id", ""),
            ticker=d.get("ticker", ""),
            side=d.get("side", ""),
            action=d.get("action", ""),
            order_type=d.get("type", ""),
            status=d.get("status", ""),
            yes_price=d.get("yes_price"),
            no_price=d.get("no_price"),
            fill_count=d.get("fill_count", 0),
            remaining_count=d.get("remaining_count", 0),
            initial_count=d.get("initial_count", 0),
            client_order_id=d.get("client_order_id"),
        )


@dataclass
class Position:
    ticker: str = ""
    position: int = 0
    market_exposure: int | None = None
    average_price: int | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(
            ticker=d.get("ticker", ""),
            position=d.get("position", 0),
            market_exposure=d.get("market_exposure"),
            average_price=d.get("average_price"),
        )


# ── Client ────────────────────────────────────────────────────────────────

class KalshiClient:
    MAX_RETRIES = 3

    def __init__(
        self,
        base_url: str | None = None,
        api_key_id: str | None = None,
        private_key_pem: str | None = None,
    ):
        self.base_url = (base_url or config.KALSHI_BASE_URL).rstrip("/")
        self.api_key_id = api_key_id or config.KALSHI_API_KEY_ID
        self.private_key_pem = private_key_pem or config.resolve_kalshi_pem()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "prediction-market-arb/0.1"

    # ── Low-level request ────────────────────────────────────────────────

    def _request(self, method: str, path: str, params: dict | None = None, json_body: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})

        signing_path = urlparse(url).path
        auth_headers = _sign_request(method, signing_path, self.api_key_id, self.private_key_pem)

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = self.session.request(
                    method, url, headers=auth_headers, json=json_body, timeout=15,
                )
                if resp.status_code >= 500 and attempt + 1 < self.MAX_RETRIES:
                    wait = 0.1 * (2 ** attempt)
                    logger.warning("Kalshi %s %s → %s, retry in %.1fs", method, path, resp.status_code, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.ConnectionError as e:
                if attempt + 1 < self.MAX_RETRIES:
                    wait = 0.1 * (2 ** attempt)
                    logger.warning("Kalshi connection error: %s, retry in %.1fs", e, wait)
                    time.sleep(wait)
                    continue
                raise

    # ── Paginated helper ─────────────────────────────────────────────────

    def _paginate(self, method: str, path: str, result_key: str,
                   params: dict | None = None, max_pages: int = 20) -> list[dict]:
        params = dict(params or {})
        all_items = []
        page = 0
        while page < max_pages:
            page += 1
            data = self._request(method, path, params=params)
            items = data.get(result_key, [])
            all_items.extend(items)
            cursor = data.get("cursor")
            if not cursor or not items:
                break
            params["cursor"] = cursor
        if page >= max_pages:
            logger.info("Pagination capped at %d pages (%d items)", max_pages, len(all_items))
        return all_items

    # ── Public / Market Data ─────────────────────────────────────────────

    def get_markets(self, series_ticker: str | None = None, status: str | None = None,
                    event_ticker: str | None = None, limit: int | None = None) -> list[Market]:
        params = {}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if event_ticker:
            params["event_ticker"] = event_ticker
        if limit:
            params["limit"] = str(limit)
        raw = self._paginate("GET", "/markets", "markets", params)
        return [Market.from_dict(m) for m in raw]

    def get_events(self, series_ticker: str | None = None, status: str | None = None) -> list[Event]:
        params = {}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        raw = self._paginate("GET", "/events", "events", params)
        return [Event.from_dict(e) for e in raw]

    def get_event(self, event_ticker: str) -> Event | None:
        data = self._request("GET", f"/events/{event_ticker}")
        evt = data.get("event")
        return Event.from_dict(evt) if evt else None

    def get_market(self, ticker: str) -> Market | None:
        data = self._request("GET", f"/markets/{ticker}")
        mkt = data.get("market")
        return Market.from_dict(mkt) if mkt else None

    def get_orderbook(self, ticker: str) -> Orderbook:
        data = self._request("GET", f"/markets/{ticker}/orderbook")

        # Kalshi may return the book under "orderbook" (integer cents)
        # or "orderbook_fp" (dollar-string format with yes_dollars/no_dollars).
        ob = data.get("orderbook")
        ob_fp = data.get("orderbook_fp")

        if ob and (ob.get("yes") or ob.get("no")):
            return Orderbook(
                yes=[OrderbookLevel(price=lv[0], quantity=lv[1]) for lv in (ob.get("yes") or [])],
                no=[OrderbookLevel(price=lv[0], quantity=lv[1]) for lv in (ob.get("no") or [])],
            )

        if ob_fp:
            yes_raw = ob_fp.get("yes") or ob_fp.get("yes_dollars") or []
            no_raw = ob_fp.get("no") or ob_fp.get("no_dollars") or []
            return Orderbook(
                yes=[OrderbookLevel(price=round(float(lv[0]) * 100), quantity=int(float(lv[1])))
                     for lv in yes_raw],
                no=[OrderbookLevel(price=round(float(lv[0]) * 100), quantity=int(float(lv[1])))
                    for lv in no_raw],
            )

        return Orderbook()

    # ── Portfolio ────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        return self._request("GET", "/portfolio/balance")

    def get_positions(self, ticker: str | None = None) -> list[Position]:
        params = {}
        if ticker:
            params["ticker"] = ticker
        raw = self._paginate("GET", "/portfolio/positions", "market_positions", params)
        return [Position.from_dict(p) for p in raw]

    def get_orders(self, ticker: str | None = None, status: str = "resting") -> list[Order]:
        params = {"status": status}
        if ticker:
            params["ticker"] = ticker
        raw = self._paginate("GET", "/portfolio/orders", "orders", params)
        return [Order.from_dict(o) for o in raw]

    def get_order(self, order_id: str) -> Order | None:
        data = self._request("GET", f"/portfolio/orders/{order_id}")
        order = data.get("order")
        return Order.from_dict(order) if order else None

    # ── Order Management ─────────────────────────────────────────────────

    def create_order(
        self,
        ticker: str,
        side: str,
        action: str = "buy",
        count: int = 1,
        yes_price: int | None = None,
        no_price: int | None = None,
        client_order_id: str | None = None,
        time_in_force: str = "good_till_canceled",
    ) -> Order:
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
            "time_in_force": time_in_force,
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        if client_order_id:
            body["client_order_id"] = client_order_id

        data = self._request("POST", "/portfolio/orders", json_body=body)
        return Order.from_dict(data.get("order", {}))

    def cancel_order(self, order_id: str) -> Order:
        data = self._request("DELETE", f"/portfolio/orders/{order_id}")
        return Order.from_dict(data.get("order", {}))

    def cancel_all_orders(self) -> int:
        orders = self.get_orders()
        cancelled = 0
        for o in orders:
            try:
                self.cancel_order(o.order_id)
                cancelled += 1
            except Exception as e:
                logger.warning("Failed to cancel %s: %s", o.order_id, e)
        return cancelled

    # ── Convenience ──────────────────────────────────────────────────────

    def search_markets(self, query: str, status: str = "open") -> list[Market]:
        """Search markets using Kalshi's text search if available, else filter locally."""
        all_markets = self.get_markets(status=status)
        q_lower = query.lower()
        return [m for m in all_markets if m.title and q_lower in m.title.lower()]
