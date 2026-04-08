"""
Live spread monitor -- tracks both sides of a matched market across platforms
and logs price snapshots to CSV for post-match analysis.

Usage:
  python spread_monitor.py --kalshi-event EVENT_TICKER --poly-slug SLUG [--interval 5]
  python spread_monitor.py --search "royals guardians"

Tracks both outcomes (e.g. KC and CLE) independently on both platforms.
"""
import argparse
import asyncio
import csv
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import aiohttp

from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient
from arb_scanner import _kalshi_book_to_prices

GAMMA_API = "https://gamma-api.polymarket.com"

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def out(msg=""):
    print(msg, flush=True)


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _get_kalshi_prices(kalshi: KalshiClient, ticker: str) -> dict:
    """Get bid/ask/mid for a Kalshi market ticker."""
    try:
        ob = kalshi.get_orderbook(ticker)
        prices = _kalshi_book_to_prices(ob)
        bid = prices.get("yes_bid")
        ask = prices.get("yes_ask")
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2
        else:
            mid = bid or ask
        return {"bid": bid, "ask": ask, "mid": mid}
    except Exception as e:
        logger.warning("Kalshi %s: %s", ticker, e)
        return {"bid": None, "ask": None, "mid": None}


def _get_poly_prices(poly: PolymarketClient, token: str) -> dict:
    """Get bid/ask/mid for a Polymarket token."""
    try:
        bid, ask = poly.get_best_prices(token)
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2
        else:
            mid = bid or ask
        return {"bid": bid, "ask": ask, "mid": mid}
    except Exception as e:
        logger.warning("Poly %s: %s", token[:12], e)
        return {"bid": None, "ask": None, "mid": None}


async def search_markets(query: str):
    out(f"\n  Searching for '{query}'...\n")
    kalshi = KalshiClient()
    out("  -- Kalshi events --")
    events = kalshi.get_events(status="open")
    q_lower = query.lower()
    matches = [e for e in events if q_lower in (e.title or "").lower()]
    if not matches:
        tokens = q_lower.split()
        matches = [e for e in events if all(t in (e.title or "").lower() for t in tokens)]
    for e in matches[:10]:
        out(f"    Event: {e.event_ticker:<30} {e.title}")
        mkts = kalshi.get_markets(event_ticker=e.event_ticker)
        for m in mkts[:5]:
            out(f"      Market: {m.ticker:<35} {m.title or '-'}")

    out("\n  -- Polymarket --")
    async with aiohttp.ClientSession() as session:
        poly_results = await PolymarketClient.search_markets(session, query)
    for m in poly_results[:10]:
        out(f"    Slug: {m.slug or '-':<40} {m.question[:50]}")
        out(f"      token_yes: {m.token_yes}")
        out(f"      condition: {m.condition_id}")

    if not matches and not poly_results:
        out("  No matches found. Try broader terms.")


async def discover_pair(kalshi_event: str, poly_slug: str):
    """
    Auto-discover tickers/tokens for both sides of an event.
    Returns: (side_a_label, side_b_label, kalshi_tickers, poly_tokens)
    """
    kalshi = KalshiClient()

    # Kalshi: get sub-markets for event
    out(f"  Kalshi event: {kalshi_event}")
    mkts = kalshi.get_markets(event_ticker=kalshi_event)
    kalshi_tickers = {}
    for m in mkts:
        # Ticker suffix is usually the side abbreviation
        suffix = m.ticker.split("-")[-1]
        kalshi_tickers[suffix] = m.ticker
        out(f"    {suffix}: {m.ticker}")

    # Polymarket: get tokens from Gamma API
    out(f"  Poly slug: {poly_slug}")
    poly_tokens = {}
    async with aiohttp.ClientSession() as session:
        params = {"slug": poly_slug}
        async with session.get(f"{GAMMA_API}/markets", params=params) as resp:
            data = await resp.json()
        if data:
            mkt = data[0]
            outcomes = mkt.get("outcomes", "[]")
            tokens = mkt.get("clobTokenIds", "[]")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            for outcome, token in zip(outcomes, tokens):
                poly_tokens[outcome] = token
                out(f"    {outcome}: {token[:30]}...")

    return kalshi_tickers, poly_tokens


async def monitor(
    kalshi_event: str,
    poly_slug: str,
    interval: int = 5,
    output_dir: str = "data",
):
    out(f"\n  Discovering market identifiers...")
    kalshi_tickers, poly_tokens = await discover_pair(kalshi_event, poly_slug)
    out()

    if len(kalshi_tickers) < 2 or len(poly_tokens) < 2:
        out("  ERROR: Need at least 2 sides on each platform.")
        out(f"  Kalshi: {list(kalshi_tickers.keys())}")
        out(f"  Poly:   {list(poly_tokens.keys())}")
        return

    # Match sides by order (first Kalshi ticker = first Poly outcome, etc.)
    k_keys = list(kalshi_tickers.keys())
    p_keys = list(poly_tokens.keys())

    # Use short labels for display
    side_a = k_keys[0]
    side_b = k_keys[1]
    side_a_full = p_keys[0]
    side_b_full = p_keys[1]

    # Match Kalshi suffix (e.g. "CLE", "KC") to Poly outcome (e.g. "Cleveland Guardians")
    # Strategy: for each Kalshi suffix, check if it matches the start of any word
    # in the Poly outcome. If one side matches, assign the other by elimination.
    def _match_side(suffix, outcomes):
        s = suffix.lower()
        for outcome in outcomes:
            words = outcome.lower().split()
            # Direct substring match (e.g. "cle" in "cleveland")
            if any(w.startswith(s) for w in words):
                return outcome
            # Initials match (e.g. "kc" matches "Kansas City")
            if len(s) >= 2:
                initials = "".join(w[0] for w in words if w)
                if initials.startswith(s):
                    return outcome
        return None

    match_a = _match_side(side_a, p_keys)
    match_b = _match_side(side_b, p_keys)

    if match_a and match_b:
        side_a_full = match_a
        side_b_full = match_b
    elif match_a:
        side_a_full = match_a
        side_b_full = [k for k in p_keys if k != match_a][0]
    elif match_b:
        side_b_full = match_b
        side_a_full = [k for k in p_keys if k != match_b][0]

    out(f"  Side A: {side_a} (Kalshi) / {side_a_full} (Poly)")
    out(f"  Side B: {side_b} (Kalshi) / {side_b_full} (Poly)")
    out()

    out("  Initializing clients...")
    kalshi = KalshiClient()
    poly = PolymarketClient()
    out("  Ready.")
    out()

    # CSV setup
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    safe_name = kalshi_event.replace("/", "_").replace("\\", "_")
    csv_file = out_path / f"spread_{safe_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    fieldnames = [
        "timestamp", "elapsed_s",
        f"k_{side_a}", f"p_{side_a}", f"spread_{side_a}",
        f"k_{side_b}", f"p_{side_b}", f"spread_{side_b}",
        "k_total", "p_total",
        "max_spread", "spread_side",
    ]

    with open(csv_file, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=fieldnames).writeheader()

    out(f"  Logging to: {csv_file}")
    out(f"  Interval: {interval}s")
    out()

    # Header
    a5 = side_a[:5].center(5)
    b5 = side_b[:5].center(5)
    out(f"  {'Time':<10}  {'Kalshi':^13}  {'Poly':^13}  {'Spread':^11}  {'Total':^11}")
    out(f"  {'':10}  {a5:>5}  {b5:>5}   {a5:>5}  {b5:>5}   {a5:>5} {b5:>5}   {'K':>5} {'P':>5}")
    out(f"  {'-'*10}  {'-'*5}  {'-'*5}   {'-'*5}  {'-'*5}   {'-'*5} {'-'*5}   {'-'*5} {'-'*5}")

    start_time = time.time()
    snap_count = 0
    max_spread_seen = 0.0

    try:
        while True:
            snap_count += 1
            elapsed = time.time() - start_time
            ts = _ts()

            # Fetch all four prices
            ka = _get_kalshi_prices(kalshi, kalshi_tickers[side_a])
            kb = _get_kalshi_prices(kalshi, kalshi_tickers[side_b])
            pa = _get_poly_prices(poly, poly_tokens[side_a_full])
            pb = _get_poly_prices(poly, poly_tokens[side_b_full])

            # Midpoints
            ka_mid = ka["mid"]
            kb_mid = kb["mid"]
            pa_mid = pa["mid"]
            pb_mid = pb["mid"]

            # Spreads per side
            spread_a = abs(ka_mid - pa_mid) if ka_mid and pa_mid else None
            spread_b = abs(kb_mid - pb_mid) if kb_mid and pb_mid else None

            # Totals (should be ~$1.00-1.02)
            k_total = (ka_mid or 0) + (kb_mid or 0) if ka_mid and kb_mid else None
            p_total = (pa_mid or 0) + (pb_mid or 0) if pa_mid and pb_mid else None

            # Which side has the bigger spread?
            if spread_a is not None and spread_b is not None:
                max_spread = max(spread_a, spread_b)
                spread_side = side_a if spread_a >= spread_b else side_b
            elif spread_a is not None:
                max_spread = spread_a
                spread_side = side_a
            elif spread_b is not None:
                max_spread = spread_b
                spread_side = side_b
            else:
                max_spread = 0
                spread_side = ""

            max_spread_seen = max(max_spread_seen, max_spread)

            # CSV row
            row = {
                "timestamp": datetime.now().isoformat(),
                "elapsed_s": f"{elapsed:.1f}",
                f"k_{side_a}": f"{ka_mid:.4f}" if ka_mid else "",
                f"p_{side_a}": f"{pa_mid:.4f}" if pa_mid else "",
                f"spread_{side_a}": f"{spread_a:.4f}" if spread_a else "",
                f"k_{side_b}": f"{kb_mid:.4f}" if kb_mid else "",
                f"p_{side_b}": f"{pb_mid:.4f}" if pb_mid else "",
                f"spread_{side_b}": f"{spread_b:.4f}" if spread_b else "",
                "k_total": f"{k_total:.4f}" if k_total else "",
                "p_total": f"{p_total:.4f}" if p_total else "",
                "max_spread": f"{max_spread:.4f}",
                "spread_side": spread_side,
            }

            with open(csv_file, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=fieldnames).writerow(row)

            # Console output
            def _c(v):
                return f"{v*100:5.1f}" if v is not None else "    -"

            marker = ""
            if max_spread >= 0.05:
                marker = " << ENTRY"
            elif max_spread >= 0.03:
                marker = " < close"

            out(f"  {ts:<10}  {_c(ka_mid)}  {_c(kb_mid)}   {_c(pa_mid)}  {_c(pb_mid)}   "
                f"{_c(spread_a)} {_c(spread_b)}   {_c(k_total)} {_c(p_total)}{marker}")

            await asyncio.sleep(interval)

    except KeyboardInterrupt:
        elapsed_min = (time.time() - start_time) / 60
        out(f"\n\n  Stopped after {snap_count} snapshots ({elapsed_min:.1f} min)")
        out(f"  Max spread seen: {max_spread_seen*100:.1f} cents")
        out(f"  Data saved: {csv_file}")


def main():
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="Live spread monitor for a matched market pair")
    parser.add_argument("--search", type=str, help="Search for markets on both platforms")
    parser.add_argument("--kalshi-event", type=str, help="Kalshi event ticker")
    parser.add_argument("--poly-slug", type=str, help="Polymarket event slug")
    parser.add_argument("--interval", type=int, default=10, help="Seconds between polls (default: 10)")

    args = parser.parse_args()

    if args.search:
        asyncio.run(search_markets(args.search))
    elif args.kalshi_event and args.poly_slug:
        asyncio.run(monitor(args.kalshi_event, args.poly_slug, args.interval))
    else:
        out("Usage:")
        out('  Search:  python spread_monitor.py --search "royals guardians"')
        out("  Monitor: python spread_monitor.py --kalshi-event EVENT --poly-slug SLUG")
        parser.print_help()


if __name__ == "__main__":
    main()
