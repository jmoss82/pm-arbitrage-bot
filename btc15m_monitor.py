"""
BTC 15-minute spread monitor that auto-discovers the current window on both
Kalshi and Polymarket, tracks spreads, and rotates to the next window on expiry.

Usage:
  python btc15m_monitor.py [--interval 5]
"""
import argparse
import asyncio
import csv
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

from arb_scanner import _kalshi_book_to_prices, estimate_entry_exit_fees_simple
from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient

GAMMA_API = "https://gamma-api.polymarket.com"
EASTERN = ZoneInfo("America/New_York")

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def out(msg=""):
    print(msg, flush=True)


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _window_boundaries(dt_utc=None):
    """Return (start, end) of the 15-minute window containing dt_utc."""
    if dt_utc is None:
        dt_utc = datetime.now(timezone.utc)
    minute = (dt_utc.minute // 15) * 15
    start = dt_utc.replace(minute=minute, second=0, microsecond=0)
    end = start + timedelta(minutes=15)
    return start, end


def _window_label(start_utc):
    edt = start_utc.astimezone(EASTERN)
    end_edt = edt + timedelta(minutes=15)
    return f"{edt.strftime('%I:%M')}-{end_edt.strftime('%I:%M %p')}"


class WindowState:
    """Tracks identifiers for the current 15-minute window on both platforms."""

    def __init__(self):
        self.window_start = None
        self.window_end = None
        self.kalshi_event = None
        self.kalshi_ticker = None
        self.poly_slug = None
        self.poly_up_token = None
        self.poly_down_token = None

    def _reset_market_state(self):
        """Clear venue-specific identifiers when rolling to a new window."""
        self.kalshi_event = None
        self.kalshi_ticker = None
        self.poly_slug = None
        self.poly_up_token = None
        self.poly_down_token = None

    def _try_kalshi(self, kalshi: KalshiClient, window_end_utc):
        """Construct the expected Kalshi ticker and verify it has an orderbook."""
        end_edt = window_end_utc.astimezone(EASTERN)
        event_ticker = f"KXBTC15M-{end_edt.strftime('%y%b%d%H%M').upper()}"
        mm = end_edt.strftime("%M")
        market_ticker = f"{event_ticker}-{mm}"

        try:
            ob = kalshi.get_orderbook(market_ticker)
            prices = _kalshi_book_to_prices(ob)
            has_data = prices.get("yes_bid") is not None or prices.get("yes_ask") is not None
            if has_data:
                self.kalshi_event = event_ticker
                self.kalshi_ticker = market_ticker
            else:
                self.kalshi_event = event_ticker
                self.kalshi_ticker = market_ticker
        except Exception:
            self.kalshi_event = event_ticker
            self.kalshi_ticker = market_ticker

    async def _try_polymarket(self):
        """Look up the current Polymarket window and store token ids if found."""
        if self.window_start is None:
            return

        self.poly_slug = f"btc-updown-15m-{int(self.window_start.timestamp())}"
        self.poly_up_token = None
        self.poly_down_token = None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{GAMMA_API}/markets", params={"slug": self.poly_slug}
                ) as resp:
                    data = await resp.json()
        except Exception as e:
            out(f"  ERROR finding Poly window: {e}")
            return

        if not data:
            return

        mkt = data[0]
        outcomes = mkt.get("outcomes", "[]")
        tokens = mkt.get("clobTokenIds", "[]")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(tokens, str):
            tokens = json.loads(tokens)

        for outcome, token in zip(outcomes, tokens):
            label = outcome.lower()
            if label == "up":
                self.poly_up_token = token
            elif label == "down":
                self.poly_down_token = token

    async def refresh(self, kalshi: KalshiClient):
        """Detect the current window and find matching markets on both platforms."""
        now = datetime.now(timezone.utc)
        start, end = _window_boundaries(now)

        if start == self.window_start:
            if self.kalshi_ticker is None:
                self._try_kalshi(kalshi, end)
            if self.poly_up_token is None or self.poly_down_token is None:
                await self._try_polymarket()
            return True

        self.window_start = start
        self.window_end = end
        label = _window_label(start)
        out(f"\n  === New window: {label} ===")

        self._reset_market_state()
        self._try_kalshi(kalshi, end)

        if self.kalshi_ticker:
            out(f"  Kalshi:  {self.kalshi_ticker}")
        else:
            out("  WARNING: Kalshi not available yet -- will retry each poll")

        await self._try_polymarket()
        if self.poly_up_token and self.poly_down_token:
            out(f"  Poly:    {self.poly_slug}")
        else:
            out(f"  WARNING: Poly not available yet for {self.poly_slug} -- will retry each poll")

        return True


def _get_mid(bid, ask):
    """Compute midpoint, handling None values."""
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return bid if bid is not None else ask


def _compute_executable_edge(k_yes_bid, k_yes_ask, p_yes_bid, p_yes_ask):
    """
    Estimate executable convergence edge using visible buy/sell prices, not mids.

    Direction 1:
      Kalshi YES bid > Polymarket YES ask
      => buy YES on Polymarket, buy DOWN on Kalshi

    Direction 2:
      Polymarket YES bid > Kalshi YES ask
      => buy YES on Kalshi, buy DOWN on Polymarket
    """
    best = {
        "direction": "",
        "gross_edge": None,
        "net_edge": None,
        "entry_cost": None,
        "fees": None,
    }

    if p_yes_ask is not None and k_yes_bid is not None and k_yes_bid > p_yes_ask:
        gross = k_yes_bid - p_yes_ask
        fees = estimate_entry_exit_fees_simple(
            p_yes_ask,
            1.0 - k_yes_bid,
            "polymarket",
            0.0,
            exit_yes_price=(k_yes_bid + p_yes_ask) / 2,
        )
        best = {
            "direction": "poly_up / kalshi_down",
            "gross_edge": gross,
            "net_edge": gross - fees,
            "entry_cost": p_yes_ask + (1.0 - k_yes_bid),
            "fees": fees,
        }

    if k_yes_ask is not None and p_yes_bid is not None and p_yes_bid > k_yes_ask:
        gross = p_yes_bid - k_yes_ask
        fees = estimate_entry_exit_fees_simple(
            k_yes_ask,
            1.0 - p_yes_bid,
            "kalshi",
            0.0,
            exit_yes_price=(k_yes_ask + p_yes_bid) / 2,
        )
        candidate = {
            "direction": "kalshi_up / poly_down",
            "gross_edge": gross,
            "net_edge": gross - fees,
            "entry_cost": k_yes_ask + (1.0 - p_yes_bid),
            "fees": fees,
        }
        if best["net_edge"] is None or candidate["net_edge"] > best["net_edge"]:
            best = candidate

    return best


async def monitor(interval: int = 5, output_dir: str = "data"):
    out("  BTC 15-Minute Spread Monitor")
    out("  " + "=" * 40)
    out()

    out("  Initializing clients...")
    kalshi = KalshiClient()
    poly = PolymarketClient()
    out("  Ready.")
    out()

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    csv_file = out_path / f"btc15m_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    fieldnames = [
        "timestamp", "window", "elapsed_s",
        "k_up", "k_down", "k_total",
        "p_up", "p_down", "p_total",
        "k_overround", "p_overround",
        "spread_up", "spread_down",
        "max_spread", "spread_side",
        "gross_edge", "net_edge", "entry_cost", "est_fees", "entry_direction",
    ]

    with open(csv_file, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=fieldnames).writeheader()

    out(f"  Logging to: {csv_file}")
    out(f"  Interval: {interval}s")

    ws = WindowState()
    start_time = time.time()
    snap_count = 0
    max_spread_ever = 0.0
    max_gross_edge_ever = 0.0
    header_printed_for_window = None

    try:
        while True:
            ok = await ws.refresh(kalshi)
            if not ok:
                out(f"  {_ts()} Waiting for next window...")
                await asyncio.sleep(interval)
                continue

            if ws.window_start != header_printed_for_window:
                header_printed_for_window = ws.window_start
                remaining = (ws.window_end - datetime.now(timezone.utc)).total_seconds()
                out(f"  ~{remaining:.0f}s remaining in window")
                out()
                out(
                    f"  {'Time':<10}  {'K.Up':>6} {'K.Dn':>6} {'K.Tot':>6}  |  "
                    f"{'P.Up':>6} {'P.Dn':>6} {'P.Tot':>6}  |  "
                    f"{'Sp.Up':>6} {'Sp.Dn':>6}  {'Max':>6}  |  "
                    f"{'Exec':>6} {'Net':>6}"
                )
                out(
                    f"  {'-'*10}  {'-'*6} {'-'*6} {'-'*6}  |  "
                    f"{'-'*6} {'-'*6} {'-'*6}  |  "
                    f"{'-'*6} {'-'*6}  {'-'*6}  |  "
                    f"{'-'*6} {'-'*6}"
                )

            snap_count += 1
            elapsed = time.time() - start_time
            ts = _ts()

            k_mid_up = k_mid_down = None
            k_yes_bid = k_yes_ask = None
            k_no_bid = k_no_ask = None
            if ws.kalshi_ticker:
                try:
                    ob = kalshi.get_orderbook(ws.kalshi_ticker)
                    prices = _kalshi_book_to_prices(ob)

                    k_yes_bid = prices.get("yes_bid")
                    k_yes_ask = prices.get("yes_ask")
                    k_no_bid = prices.get("no_bid")
                    k_no_ask = prices.get("no_ask")

                    k_mid_up = _get_mid(k_yes_bid, k_yes_ask)
                    k_mid_down = _get_mid(k_no_bid, k_no_ask)

                    if k_mid_up is not None and k_mid_down is None:
                        k_mid_down = 1.0 - k_mid_up
                    elif k_mid_down is not None and k_mid_up is None:
                        k_mid_up = 1.0 - k_mid_down
                except Exception as e:
                    logger.warning("Kalshi: %s", e)

            p_mid_up = p_mid_down = None
            p_up_bid = p_up_ask = None
            p_down_bid = p_down_ask = None

            if ws.poly_up_token:
                try:
                    p_up_bid, p_up_ask = poly.get_best_prices(ws.poly_up_token)
                    p_mid_up = _get_mid(p_up_bid, p_up_ask)
                except Exception as e:
                    logger.warning("Poly Up: %s", e)

            if ws.poly_down_token:
                try:
                    p_down_bid, p_down_ask = poly.get_best_prices(ws.poly_down_token)
                    p_mid_down = _get_mid(p_down_bid, p_down_ask)
                except Exception as e:
                    logger.warning("Poly Down: %s", e)

            k_total = (k_mid_up + k_mid_down) if k_mid_up is not None and k_mid_down is not None else None
            p_total = (p_mid_up + p_mid_down) if p_mid_up is not None and p_mid_down is not None else None
            k_overround = (k_total - 1.0) if k_total is not None else None
            p_overround = (p_total - 1.0) if p_total is not None else None

            spread_up = abs(k_mid_up - p_mid_up) if k_mid_up is not None and p_mid_up is not None else None
            spread_down = abs(k_mid_down - p_mid_down) if k_mid_down is not None and p_mid_down is not None else None

            if spread_up is not None and spread_down is not None:
                max_spread = max(spread_up, spread_down)
                spread_side = "Up" if spread_up >= spread_down else "Down"
            elif spread_up is not None:
                max_spread = spread_up
                spread_side = "Up"
            elif spread_down is not None:
                max_spread = spread_down
                spread_side = "Down"
            else:
                max_spread = 0.0
                spread_side = ""

            executable = _compute_executable_edge(k_yes_bid, k_yes_ask, p_up_bid, p_up_ask)
            executable_edge = executable["gross_edge"] or 0.0
            executable_net = executable["net_edge"] or 0.0

            max_spread_ever = max(max_spread_ever, max_spread)
            max_gross_edge_ever = max(max_gross_edge_ever, executable_edge)

            row = {
                "timestamp": datetime.now().isoformat(),
                "window": _window_label(ws.window_start),
                "elapsed_s": f"{elapsed:.1f}",
                "k_up": f"{k_mid_up:.4f}" if k_mid_up is not None else "",
                "k_down": f"{k_mid_down:.4f}" if k_mid_down is not None else "",
                "k_total": f"{k_total:.4f}" if k_total is not None else "",
                "p_up": f"{p_mid_up:.4f}" if p_mid_up is not None else "",
                "p_down": f"{p_mid_down:.4f}" if p_mid_down is not None else "",
                "p_total": f"{p_total:.4f}" if p_total is not None else "",
                "k_overround": f"{k_overround:.4f}" if k_overround is not None else "",
                "p_overround": f"{p_overround:.4f}" if p_overround is not None else "",
                "spread_up": f"{spread_up:.4f}" if spread_up is not None else "",
                "spread_down": f"{spread_down:.4f}" if spread_down is not None else "",
                "max_spread": f"{max_spread:.4f}",
                "spread_side": spread_side,
                "gross_edge": f"{executable['gross_edge']:.4f}" if executable["gross_edge"] is not None else "",
                "net_edge": f"{executable['net_edge']:.4f}" if executable["net_edge"] is not None else "",
                "entry_cost": f"{executable['entry_cost']:.4f}" if executable["entry_cost"] is not None else "",
                "est_fees": f"{executable['fees']:.4f}" if executable["fees"] is not None else "",
                "entry_direction": executable["direction"],
            }

            with open(csv_file, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=fieldnames).writerow(row)

            def _c(v):
                return f"{v*100:5.1f}" if v is not None else "    -"

            marker = ""
            if executable_edge >= 0.05 and executable_net > 0:
                marker = " << ENTRY"
            elif executable_edge >= 0.03 and executable_net > 0:
                marker = " < close"

            out(
                f"  {ts:<10}  {_c(k_mid_up)} {_c(k_mid_down)} {_c(k_total)}  |  "
                f"{_c(p_mid_up)} {_c(p_mid_down)} {_c(p_total)}  |  "
                f"{_c(spread_up)} {_c(spread_down)}  {_c(max_spread)}  |  "
                f"{_c(executable_edge)} {_c(executable_net)}{marker}"
            )

            await asyncio.sleep(interval)

    except KeyboardInterrupt:
        elapsed_min = (time.time() - start_time) / 60
        out(f"\n\n  Stopped after {snap_count} snapshots ({elapsed_min:.1f} min)")
        out(f"  Max midpoint spread seen: {max_spread_ever*100:.1f} cents")
        out(f"  Max executable edge seen: {max_gross_edge_ever*100:.1f} cents")
        out(f"  Data saved: {csv_file}")


def main():
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="BTC 15-minute spread monitor")
    parser.add_argument("--interval", type=int, default=5, help="Seconds between polls (default: 5)")
    args = parser.parse_args()

    asyncio.run(monitor(interval=args.interval))


if __name__ == "__main__":
    main()
