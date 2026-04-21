"""
Polymarket BTC short-duration window discovery.

A ``Window`` represents one BTC up/down market (default 5 minutes) and owns
both outcome token ids.  Callers construct the current window from wall
clock boundaries and then call ``resolve`` against Gamma to attach the
token ids.

Window boundaries are computed in UTC.  Polymarket slugs use the UTC
unix timestamp of the window start, so no timezone gymnastics are needed
here -- unlike Kalshi, which embeds Eastern time in its ticker.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

from . import config

GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class Window:
    start: datetime
    end: datetime
    slug: str
    up_token: Optional[str] = None
    down_token: Optional[str] = None
    condition_id: Optional[str] = None
    end_date_iso: Optional[str] = None
    question: Optional[str] = None
    raw: dict = field(default_factory=dict)

    def seconds_remaining(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max(0.0, (self.end - now).total_seconds())

    def elapsed_s(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.start).total_seconds())

    def has_tokens(self) -> bool:
        return self.up_token is not None and self.down_token is not None

    def label(self) -> str:
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M UTC')}"


def current_window_boundaries(
    minutes: Optional[int] = None,
    now: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    """Return (start, end) of the N-minute window containing ``now``."""
    minutes = minutes or config.SNIPE_WINDOW_MINUTES
    now = now or datetime.now(timezone.utc)
    minute = (now.minute // minutes) * minutes
    start = now.replace(minute=minute, second=0, microsecond=0)
    end = start + timedelta(minutes=minutes)
    return start, end


def build_slug(window_start: datetime) -> str:
    return config.SNIPE_POLY_SLUG_PATTERN.format(ts=int(window_start.timestamp()))


def _parse_json_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    return []


def _extract_tokens(mkt: dict) -> tuple[Optional[str], Optional[str]]:
    """Map Polymarket market outcomes onto (up_token, down_token)."""
    outcomes = _parse_json_list(mkt.get("outcomes", "[]"))
    tokens = _parse_json_list(mkt.get("clobTokenIds", "[]"))

    up_token: Optional[str] = None
    down_token: Optional[str] = None

    for outcome, token in zip(outcomes, tokens):
        label = str(outcome).strip().lower()
        if label in ("up", "yes", "higher"):
            up_token = token
        elif label in ("down", "no", "lower"):
            down_token = token

    return up_token, down_token


async def resolve_window(
    session: aiohttp.ClientSession,
    window_start: datetime,
    window_end: datetime,
) -> Window:
    """Fetch the Polymarket market for ``window_start`` and attach token ids.

    Returns a Window with ``has_tokens() == False`` when the market is not
    yet published on Gamma.  Callers are expected to retry.
    """
    slug = build_slug(window_start)
    w = Window(start=window_start, end=window_end, slug=slug)

    try:
        async with session.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            data = await resp.json()
    except Exception:
        return w

    if not data:
        return w

    mkt = data[0] if isinstance(data, list) else data
    if not isinstance(mkt, dict):
        return w

    up_token, down_token = _extract_tokens(mkt)
    w.up_token = up_token
    w.down_token = down_token
    w.condition_id = mkt.get("conditionId")
    w.end_date_iso = mkt.get("endDate") or mkt.get("endDateIso")
    w.question = mkt.get("question") or mkt.get("slug")
    w.raw = mkt
    return w


async def search_btc_markets(
    session: aiohttp.ClientSession,
    query: Optional[str] = None,
    limit: int = 25,
) -> list[dict]:
    """
    Gamma full-text search for BTC markets.

    Used by the ``probe`` CLI command to verify the slug convention when
    direct slug lookup returns nothing.  Output is a list of raw market
    dicts; callers pick out whichever ones look like short-duration windows.
    """
    query = query or config.SNIPE_GAMMA_SEARCH_QUERY
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(limit),
        "order": "endDate",
        "ascending": "true",
    }
    try:
        async with session.get(
            f"{GAMMA_API}/markets",
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
    except Exception:
        return []

    if not isinstance(data, list):
        return []

    q = query.lower()
    hits = []
    for mkt in data:
        if not isinstance(mkt, dict):
            continue
        blob = " ".join(
            str(mkt.get(k, ""))
            for k in ("slug", "question", "description", "title")
        ).lower()
        if "btc" in blob or "bitcoin" in blob or q in blob:
            hits.append(mkt)
    return hits


async def fetch_market_by_condition_id(
    session: aiohttp.ClientSession,
    condition_id: str,
) -> Optional[dict]:
    """Fetch a single market by its Polymarket condition id, for post-hoc settlement lookup."""
    try:
        async with session.get(
            f"{GAMMA_API}/markets",
            params={"condition_ids": condition_id},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            data = await resp.json()
    except Exception:
        return None

    if isinstance(data, list) and data:
        return data[0]
    return None
