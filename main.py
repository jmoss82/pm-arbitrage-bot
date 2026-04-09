"""
Prediction Market Arbitrage -- Polymarket x Kalshi spread trading engine.

Strategy: convergence trading. Enter when cross-platform spreads are wide,
exit when they compress. Never hold to resolution.

Modes:
  discover  -- Show active markets on both platforms
  match     -- Show matched market pairs (no price checks)
  scan      -- One-shot spread scan across matched pairs
  monitor   -- Continuous loop: scan for entries, watch open positions for exits
  execute   -- One-shot scan + enter any found opportunities
  positions -- Show open arb positions and unrealized P&L
  status    -- Show account balances on both platforms
"""
import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import aiohttp

import config
from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient, PolymarketMarket
from market_matcher import match_markets, print_pairs, MarketPair
from arb_scanner import scan_all_pairs, print_opportunities, fetch_snapshot, detect_spread
from arb_executor import ArbExecutor
from position_manager import PositionManager
from trade_logger import log_signal

logger = logging.getLogger("arb")


def setup_logging():
    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
    kalshi_cfg = config.kalshi_config_summary()
    logger.info(
        "Kalshi config: env=%s base_url=%s api_key=%s private_key=%s source=%s",
        kalshi_cfg["env"],
        kalshi_cfg["base_url"],
        "set" if kalshi_cfg["has_api_key"] else "missing",
        "set" if kalshi_cfg["has_private_key"] else "missing",
        kalshi_cfg["private_key_source"],
    )


def _header(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def _is_btc15_pair_label(label: str) -> bool:
    text = (label or "").lower()
    return ("btc" in text or "bitcoin" in text) and ("15" in text or "minute" in text)


def _seconds_into_15m_window(now_ts: float | None = None) -> int:
    dt = datetime.fromtimestamp(now_ts or time.time(), tz=timezone.utc)
    return (dt.minute % 15) * 60 + dt.second


def _entry_timing_allowed(pair_label: str) -> tuple[bool, str]:
    if not config.ARB_BTC15_TIME_GATING:
        return True, "timing-gate-disabled"
    if not _is_btc15_pair_label(pair_label):
        return True, "non-btc-pair"
    sec = _seconds_into_15m_window()
    if sec < config.ARB_ENTRY_COOLDOWN_SECONDS_AFTER_ROLLOVER:
        return False, f"cooldown-after-rollover({sec}s)"
    if sec < config.ARB_ENTRY_MIN_SECONDS_IN_WINDOW:
        return False, f"too-early({sec}s)"
    if sec > config.ARB_ENTRY_MAX_SECONDS_IN_WINDOW:
        return False, f"too-late({sec}s)"
    return True, "ok"


def _opportunity_passes_quality_filters(opp) -> tuple[bool, str]:
    if opp.kalshi_leg_available_qty is not None and opp.kalshi_leg_available_qty < config.ARB_MIN_KALSHI_LEVEL_QTY:
        return False, f"kalshi-qty<{config.ARB_MIN_KALSHI_LEVEL_QTY}"
    if opp.poly_total is not None and abs(opp.poly_total - 1.0) > config.ARB_MAX_POLY_OVERROUND:
        return False, f"poly-total-outside({opp.poly_total:.3f})"
    return True, "ok"


def _print_runtime_mode_banner(mode_label: str):
    live_requested = config.live_mode_requested()
    live_armed = config.live_mode_armed()
    print(f"\n  Mode: {mode_label}")
    print(f"  Dry run: {config.ARB_DRY_RUN}")
    print(f"  Live enabled: {config.ARB_ENABLE_LIVE}")
    print(f"  Live requested: {live_requested}")
    print(f"  Live armed: {live_armed}")
    if live_requested and live_armed:
        print("  !!! LIVE MODE ARMED !!!")


def _fmt_px(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "-"


def _btc_diag_line(snap, opp) -> str:
    mid_spread = snap.mid_spread
    parts = [
        f"K y {_fmt_px(snap.kalshi_yes_bid)}/{_fmt_px(snap.kalshi_yes_ask)}",
        f"P y {_fmt_px(snap.poly_yes_bid)}/{_fmt_px(snap.poly_yes_ask)}",
        f"mid {_fmt_px(mid_spread)}",
    ]
    if opp:
        parts.extend([
            f"dir {opp.direction.value}",
            f"gross {opp.spread_width:.3f}",
            f"net {opp.net_edge:.3f}",
            f"k_qty {opp.kalshi_leg_available_qty if opp.kalshi_leg_available_qty is not None else '-'}",
        ])
    else:
        parts.append("gross -")
        parts.append("net -")
    return " | ".join(parts)


def _position_status_line(pos) -> str:
    return (
        f"{pos.pair_label} | dir {pos.direction} | spread {pos.current_spread:.3f} "
        f"| target {pos.target_exit_spread:.3f} | stop {pos.stop_loss_spread:.3f} "
        f"| pnl ${pos.unrealized_pnl:+.2f}"
    )


def _preflight_or_raise(executor: ArbExecutor, mode_label: str):
    ok, issues = executor.preflight_check()
    if ok:
        return
    details = "; ".join(issues) if issues else "unknown preflight failure"
    raise RuntimeError(f"{mode_label} preflight failed: {details}")


# -- Shared helpers ------------------------------------------------------------
EDT = timezone(timedelta(hours=-4))


def _btc15_window_boundaries(dt_utc: datetime | None = None) -> tuple[datetime, datetime]:
    if dt_utc is None:
        dt_utc = datetime.now(timezone.utc)
    minute = (dt_utc.minute // 15) * 15
    start = dt_utc.replace(minute=minute, second=0, microsecond=0)
    end = start + timedelta(minutes=15)
    return start, end


def _btc15_market_ids(dt_utc: datetime | None = None) -> dict:
    start, end = _btc15_window_boundaries(dt_utc)
    end_edt = end.astimezone(EDT)
    event_ticker = f"KXBTC15M-{end_edt.strftime('%y%b%d%H%M').upper()}"
    mm = end_edt.strftime("%M")
    market_ticker = f"{event_ticker}-{mm}"
    poly_slug = f"btc-updown-15m-{int(start.timestamp())}"
    return {
        "window_start": start,
        "window_end": end,
        "window_key": start.isoformat(),
        "window_label": f"{start.astimezone(EDT).strftime('%I:%M')}-{end.astimezone(EDT).strftime('%I:%M %p')}",
        "kalshi_event_ticker": event_ticker,
        "kalshi_ticker": market_ticker,
        "poly_slug": poly_slug,
    }


async def _fetch_btc15_pair(
    kalshi: KalshiClient,
    session: aiohttp.ClientSession,
) -> tuple[MarketPair | None, dict, str]:
    ids = _btc15_market_ids()

    try:
        kalshi_market = kalshi.get_market(ids["kalshi_ticker"])
    except Exception as e:
        return None, ids, f"kalshi unavailable ({e})"

    poly_market: dict | None = None
    try:
        async with session.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": ids["poly_slug"]},
        ) as resp:
            data = await resp.json()
            if data:
                poly_market = data[0]
    except Exception as e:
        return None, ids, f"polymarket unavailable ({e})"

    if not poly_market:
        return None, ids, "polymarket slug not listed yet"

    outcomes = poly_market.get("outcomes", "[]")
    tokens = poly_market.get("clobTokenIds", "[]")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(tokens, str):
        tokens = json.loads(tokens)

    token_yes = ""
    token_no = ""
    for outcome, token in zip(outcomes, tokens):
        label = str(outcome).lower()
        if label == "up":
            token_yes = token
        elif label == "down":
            token_no = token

    if not token_yes or not token_no:
        return None, ids, "polymarket up/down tokens unavailable"

    poly = PolymarketMarket(
        condition_id=poly_market.get("conditionId", ""),
        question=poly_market.get("question", f"BTC 15m {ids['window_label']}"),
        slug=ids["poly_slug"],
        token_yes=token_yes,
        token_no=token_no,
        active=bool(poly_market.get("active", True)),
        accepting_orders=bool(poly_market.get("acceptingOrders", True)),
    )
    pair = MarketPair(
        kalshi_event=None,
        kalshi_market=kalshi_market,
        poly=poly,
        match_score=100.0,
        match_method="btc15m",
        notes=ids["window_label"],
    )
    return pair, ids, ""


async def _fetch_and_match(kalshi: KalshiClient, min_score: float = 65.0) -> list:
    print("  Fetching Kalshi events...")
    k_events = kalshi.get_events(status="open")
    print(f"  {len(k_events)} Kalshi events")

    print("  Fetching Polymarket markets...")
    async with aiohttp.ClientSession() as session:
        p_markets = await PolymarketClient.fetch_all_active_markets(session, max_pages=5)
    print(f"  {len(p_markets)} Polymarket markets")

    print("  Matching...")
    pairs = match_markets(
        kalshi_markets=[], poly_markets=p_markets, min_score=min_score,
        kalshi_client=kalshi, kalshi_events=k_events,
    )
    return pairs


# -- Mode: discover ------------------------------------------------------------

async def cmd_discover(args):
    _header("Market Discovery")
    kalshi = KalshiClient()

    print("\n  Fetching Kalshi events...")
    k_events = kalshi.get_events(status="open")
    print(f"  Found {len(k_events)} open Kalshi events")
    for e in k_events[:20]:
        cat = f"[{e.category}]" if e.category else ""
        print(f"    {cat:<25} {(e.title or e.event_ticker)[:60]}")
    if len(k_events) > 20:
        print(f"    ... and {len(k_events) - 20} more")

    print("\n  Fetching Polymarket markets...")
    async with aiohttp.ClientSession() as session:
        p_markets = await PolymarketClient.fetch_all_active_markets(session, max_pages=5)
    print(f"  Found {len(p_markets)} active Polymarket markets")

    for m in p_markets[:20]:
        price = f"{m.price_yes:.2f}" if m.price_yes else "-"
        print(f"    {m.question[:70]:<70} yes={price}")
    if len(p_markets) > 20:
        print(f"    ... and {len(p_markets) - 20} more")


# -- Mode: match ---------------------------------------------------------------

async def cmd_match(args):
    _header("Cross-Platform Market Matching")
    kalshi = KalshiClient()

    print("\n  Fetching events/markets from both platforms...")
    k_events = kalshi.get_events(status="open")
    async with aiohttp.ClientSession() as session:
        p_markets = await PolymarketClient.fetch_all_active_markets(session, max_pages=5)

    print(f"  Kalshi: {len(k_events)} events")
    print(f"  Polymarket: {len(p_markets)} markets")

    min_score = args.min_score if hasattr(args, "min_score") else 65.0
    print(f"\n  Matching (min_score={min_score})...")
    pairs = match_markets(
        kalshi_markets=[], poly_markets=p_markets, min_score=min_score,
        kalshi_client=kalshi, kalshi_events=k_events,
    )
    print(f"  Found {len(pairs)} matched pairs")
    print_pairs(pairs)


# -- Mode: scan ----------------------------------------------------------------

async def cmd_scan(args):
    _header("Spread Scan")
    kalshi = KalshiClient()
    poly = PolymarketClient()

    if config.ARB_BTC15_ONLY:
        print("\n  BTC-only mode enabled (single 15-minute pair).")
        async with aiohttp.ClientSession() as session:
            pair, ids, reason = await _fetch_btc15_pair(kalshi, session)
        if not pair:
            print(f"  No active BTC pair available yet: {reason}")
            return

        print(f"  Window: {ids['window_label']}")
        print(f"  Kalshi: {ids['kalshi_ticker']}")
        print(f"  Poly:   {ids['poly_slug']}")
        snap = fetch_snapshot(pair, kalshi, poly)
        opp = detect_spread(snap)
        opps = [opp] if (opp and opp.net_edge >= config.ARB_MIN_EDGE) else []
        print(f"\n  Found {len(opps)} spreads above {config.ARB_MIN_EDGE:.2%} net edge")
        print_opportunities(opps)
        return

    print("\n  Fetching and matching markets...")
    pairs = await _fetch_and_match(kalshi)
    tradeable = [p for p in pairs if p.kalshi_market]
    print(f"  {len(pairs)} matched pairs ({len(tradeable)} tradeable)")
    print_pairs(pairs[:10])  # show top 10

    if not tradeable:
        print("\n  No tradeable matched pairs.")
        return

    print(f"\n  Scanning {len(tradeable)} pairs for spreads...")
    opps = scan_all_pairs(tradeable, kalshi, poly, min_spread=config.ARB_MIN_EDGE)
    print(f"\n  Found {len(opps)} spreads above {config.ARB_MIN_EDGE:.2%} net edge")
    print_opportunities(opps)

    # Show near-misses
    all_opps = scan_all_pairs(tradeable, kalshi, poly, min_spread=-0.10)
    near = [o for o in all_opps if o.spread_width > 0 and o.net_edge < config.ARB_MIN_EDGE]
    if near:
        print(f"\n  Near-misses ({len(near)} with spread but eaten by fees):")
        print_opportunities(near[:10])


# -- Mode: monitor -------------------------------------------------------------

async def cmd_monitor(args):
    _header("Spread Monitor")
    kalshi = KalshiClient()
    poly = PolymarketClient()
    pos_mgr = PositionManager()
    interval = config.ARB_SCAN_INTERVAL

    print(f"\n  Scan interval: {interval}s")
    print(f"  Min edge: {config.ARB_MIN_EDGE:.2%}")
    _print_runtime_mode_banner("monitor")
    print(f"  Exit target: spread compresses {pos_mgr.exit_target_pct*100:.0f}%")
    print(f"  Stop loss: spread widens {pos_mgr.stop_loss_pct*100:.0f}%")
    print(f"  Open positions: {len(pos_mgr.positions)}")
    print(f"  Press Ctrl+C to stop\n")

    tradeable: list[MarketPair] = []
    btc_pair: MarketPair | None = None
    btc_window_key = ""
    session = aiohttp.ClientSession()
    try:
        if config.ARB_BTC15_ONLY:
            print("  BTC-only mode enabled (single 15-minute pair).\n")
        else:
            pairs = await _fetch_and_match(kalshi)
            tradeable = [p for p in pairs if p.kalshi_market]
            print(f"  Monitoring {len(tradeable)} tradeable pairs\n")
            if not tradeable and not pos_mgr.positions:
                print("  No pairs and no open positions. Exiting.")
                return

        executor = ArbExecutor(kalshi, poly, pos_mgr) if not args.scan_only else None
        if executor:
            _preflight_or_raise(executor, "monitor")
        scan_count = 0
        entry_streaks: dict[str, int] = {}

        while True:
            scan_count += 1
            ts = time.strftime("%H:%M:%S")

            # 1. Check open positions for exit signals
            if pos_mgr.positions:
                for pos in list(pos_mgr.positions.values()):
                    if pos.status != "open":
                        continue
                    # Build a minimal pair to fetch prices
                    try:
                        _update_position_prices(pos, kalshi, poly)
                    except Exception as e:
                        logger.warning("Failed to update position %s: %s", pos.id, e)

                exit_signals = pos_mgr.check_exit_signals()
                for pos, reason in exit_signals:
                    print(f"  [{ts}] EXIT SIGNAL [{reason}]: {pos.pair_label}")
                    print(f"    Spread: {pos.entry_spread:.4f} -> {pos.current_spread:.4f} "
                          f"({pos.spread_compression_pct*100:.0f}% compression)")
                    if executor:
                        result = executor.exit(pos, reason)
                        print(f"    > {result.summary()}")
                if (not exit_signals) and scan_count % 6 == 1:
                    for pos in pos_mgr.positions.values():
                        if pos.status == "open":
                            print(f"  [{ts}] Tracking exit: {_position_status_line(pos)}")

            # 2. Scan for new entry opportunities
            if executor and executor.emergency_stop:
                if scan_count % 12 == 1:
                    print(f"  [{ts}] ENTRY HALTED: {executor.emergency_reason}")
                await asyncio.sleep(interval)
                continue

            if config.ARB_BTC15_ONLY:
                ids = _btc15_market_ids()
                if ids["window_key"] != btc_window_key or btc_pair is None or scan_count % 8 == 1:
                    btc_pair, ids, reason = await _fetch_btc15_pair(kalshi, session)
                    if ids["window_key"] != btc_window_key:
                        btc_window_key = ids["window_key"]
                        print(f"  [{ts}] Active BTC window: {ids['window_label']}")
                        print(f"      Kalshi: {ids['kalshi_ticker']}")
                        print(f"      Poly:   {ids['poly_slug']}")
                    if not btc_pair and scan_count % 5 == 1:
                        print(f"  [{ts}] Waiting for BTC pair listing: {reason}")

                if btc_pair:
                    snap = fetch_snapshot(btc_pair, kalshi, poly)
                    single = detect_spread(snap)
                    opps = [single] if (single and single.net_edge >= config.ARB_MIN_EDGE) else []
                else:
                    opps = []
            elif tradeable:
                opps = scan_all_pairs(tradeable, kalshi, poly, min_spread=config.ARB_MIN_EDGE)
            else:
                opps = []

            if opps:
                keys_this_scan = set()
                accepted: list = []
                for opp in opps:
                    key = f"{opp.pair.kalshi_ticker}:{opp.direction.value}"
                    keys_this_scan.add(key)
                    timing_ok, timing_reason = _entry_timing_allowed(opp.pair.label)
                    quality_ok, quality_reason = _opportunity_passes_quality_filters(opp)
                    already_open = pos_mgr.has_open_position(opp.pair.kalshi_ticker, opp.direction.value)
                    if not (timing_ok and quality_ok) or already_open:
                        entry_streaks[key] = 0
                        log_signal({
                            "pair": opp.pair.label,
                            "ticker": opp.pair.kalshi_ticker,
                            "direction": opp.direction.value,
                            "spread_width": round(opp.spread_width, 6),
                            "net_edge": round(opp.net_edge, 6),
                            "accepted": False,
                            "reason": (
                                "matching-position-open"
                                if already_open
                                else timing_reason if not timing_ok else quality_reason
                            ),
                        })
                        continue

                    entry_streaks[key] = entry_streaks.get(key, 0) + 1
                    if entry_streaks[key] < config.ARB_MIN_EDGE_PERSIST_SCANS:
                        log_signal({
                            "pair": opp.pair.label,
                            "ticker": opp.pair.kalshi_ticker,
                            "direction": opp.direction.value,
                            "spread_width": round(opp.spread_width, 6),
                            "net_edge": round(opp.net_edge, 6),
                            "accepted": False,
                            "reason": f"edge-not-persistent({entry_streaks[key]}/{config.ARB_MIN_EDGE_PERSIST_SCANS})",
                        })
                        continue

                    accepted.append(opp)
                    log_signal({
                        "pair": opp.pair.label,
                        "ticker": opp.pair.kalshi_ticker,
                        "direction": opp.direction.value,
                        "spread_width": round(opp.spread_width, 6),
                        "net_edge": round(opp.net_edge, 6),
                        "accepted": True,
                        "reason": "passed-filters",
                    })

                for stale_key in list(entry_streaks):
                    if stale_key not in keys_this_scan:
                        entry_streaks.pop(stale_key, None)

                if accepted:
                    print(f"  [{ts}] Scan #{scan_count}: {len(accepted)} spread opportunities!")
                    print_opportunities(accepted)

                    if executor and not args.scan_only:
                        for opp in accepted:
                            result = executor.enter(opp)
                            print(f"    > {result.summary()}")
                elif scan_count % 12 == 1:
                    open_count = len(pos_mgr.positions)
                    pair_count = 1 if config.ARB_BTC15_ONLY else len(tradeable)
                    print(f"  [{ts}] Scan #{scan_count}: no new spreads "
                          f"({pair_count} pair, {open_count} open positions)")
                    if config.ARB_BTC15_ONLY and btc_pair:
                        print(f"      {_btc_diag_line(snap, single)}")
            elif scan_count % 12 == 1:
                open_count = len(pos_mgr.positions)
                pair_count = 1 if config.ARB_BTC15_ONLY else len(tradeable)
                print(f"  [{ts}] Scan #{scan_count}: no new spreads "
                      f"({pair_count} pair, {open_count} open positions)")
                if config.ARB_BTC15_ONLY and btc_pair:
                    print(f"      {_btc_diag_line(snap, single)}")

            await asyncio.sleep(interval)

            # Refresh markets every 50 scans (non-BTC-only mode)
            if (not config.ARB_BTC15_ONLY) and scan_count % 50 == 0:
                pairs = await _fetch_and_match(kalshi)
                tradeable = [p for p in pairs if p.kalshi_market]
                print(f"  [{time.strftime('%H:%M:%S')}] Refreshed: {len(tradeable)} tradeable pairs")

    except KeyboardInterrupt:
        print("\n\n  Stopped.")
        if 'executor' in locals() and executor:
            executor.print_ledger()
        pos_mgr.print_positions()
    finally:
        await session.close()


def _update_position_prices(pos, kalshi: KalshiClient, poly: PolymarketClient):
    """Fetch current prices and update a position's spread tracking."""
    from arb_scanner import PriceSnapshot, SpreadDirection, _kalshi_book_to_prices, estimate_round_trip_fees

    snap = PriceSnapshot(pair=None, timestamp=time.time())

    try:
        ob = kalshi.get_orderbook(pos.kalshi_ticker)
        prices = _kalshi_book_to_prices(ob)
        snap.kalshi_yes_bid = prices.get("yes_bid")
        snap.kalshi_yes_ask = prices.get("yes_ask")
        snap.kalshi_no_bid = prices.get("no_bid")
        snap.kalshi_no_ask = prices.get("no_ask")
    except Exception:
        pass

    try:
        poly_bid, poly_ask = poly.get_best_prices(pos.poly_token_yes)
        snap.poly_yes_bid = poly_bid
        snap.poly_yes_ask = poly_ask
        if poly_bid is not None:
            snap.poly_no_ask = 1.0 - poly_bid
        if poly_ask is not None:
            snap.poly_no_bid = 1.0 - poly_ask
    except Exception:
        pass

    pos.last_update = time.time()

    k_mid = snap.kalshi_yes_mid
    p_mid = snap.poly_yes_mid

    if pos.direction == SpreadDirection.KALSHI_HIGHER.value:
        pos.current_yes_bid = snap.poly_yes_bid or 0
        pos.current_no_bid = snap.kalshi_no_bid or 0
        if k_mid is not None and p_mid is not None:
            pos.current_spread = max(0, k_mid - p_mid)
    else:
        pos.current_yes_bid = snap.kalshi_yes_bid or 0
        pos.current_no_bid = snap.poly_no_bid or 0
        if k_mid is not None and p_mid is not None:
            pos.current_spread = max(0, p_mid - k_mid)

    exit_yes = pos.current_yes_bid or pos.yes_entry_price
    exit_no = pos.current_no_bid or pos.no_entry_price
    exit_proceeds = (exit_yes + exit_no) * pos.contracts
    fees = estimate_round_trip_fees(
        pos.yes_entry_price, pos.no_entry_price,
        exit_yes, exit_no,
        pos.yes_platform,
    ) * pos.contracts
    pos.unrealized_pnl = exit_proceeds - pos.entry_cost - fees


# -- Mode: execute -------------------------------------------------------------

async def cmd_execute(args):
    dry_label = "DRY RUN" if config.ARB_DRY_RUN else "LIVE"
    _header(f"Spread Execute ({dry_label})")
    kalshi = KalshiClient()
    poly = PolymarketClient()
    pos_mgr = PositionManager()
    _print_runtime_mode_banner("execute")

    if config.ARB_BTC15_ONLY:
        print("\n  BTC-only mode enabled (single 15-minute pair).")
        async with aiohttp.ClientSession() as session:
            pair, ids, reason = await _fetch_btc15_pair(kalshi, session)
        if not pair:
            print(f"  No active BTC pair available yet: {reason}")
            return
        print(f"  Window: {ids['window_label']}")
        print(f"  Kalshi: {ids['kalshi_ticker']}")
        print(f"  Poly:   {ids['poly_slug']}")
        snap = fetch_snapshot(pair, kalshi, poly)
        single = detect_spread(snap)
        opps = [single] if (single and single.net_edge >= config.ARB_MIN_EDGE) else []
    else:
        print("\n  Fetching and matching markets...")
        pairs = await _fetch_and_match(kalshi)
        tradeable = [p for p in pairs if p.kalshi_market]
        print(f"  {len(tradeable)} tradeable pairs")
        opps = scan_all_pairs(tradeable, kalshi, poly, min_spread=config.ARB_MIN_EDGE)

    print(f"  {len(opps)} opportunities above {config.ARB_MIN_EDGE:.2%} edge")

    if not opps:
        print("  Nothing to execute.")
        return

    executor = ArbExecutor(kalshi, poly, pos_mgr)
    _preflight_or_raise(executor, "execute")
    for opp in opps:
        timing_ok, timing_reason = _entry_timing_allowed(opp.pair.label)
        quality_ok, quality_reason = _opportunity_passes_quality_filters(opp)
        if not (timing_ok and quality_ok):
            reason = timing_reason if not timing_ok else quality_reason
            print(f"\n  Skipping {opp.pair.label}: {reason}")
            log_signal({
                "pair": opp.pair.label,
                "ticker": opp.pair.kalshi_ticker,
                "direction": opp.direction.value,
                "spread_width": round(opp.spread_width, 6),
                "net_edge": round(opp.net_edge, 6),
                "accepted": False,
                "reason": reason,
            })
            continue

        log_signal({
            "pair": opp.pair.label,
            "ticker": opp.pair.kalshi_ticker,
            "direction": opp.direction.value,
            "spread_width": round(opp.spread_width, 6),
            "net_edge": round(opp.net_edge, 6),
            "accepted": True,
            "reason": "passed-filters",
        })
        print_opportunities([opp])
        result = executor.enter(opp)
        print(f"\n  > {result.summary()}")

    executor.print_ledger()
    pos_mgr.print_positions()


# -- Mode: positions -----------------------------------------------------------

async def cmd_positions(args):
    _header("Open Arb Positions")
    pos_mgr = PositionManager()

    if not pos_mgr.positions:
        print("\n  No open positions.")
        return

    # Refresh prices
    kalshi = KalshiClient()
    poly = PolymarketClient()

    print("\n  Refreshing prices...")
    for pos in pos_mgr.positions.values():
        try:
            _update_position_prices(pos, kalshi, poly)
        except Exception as e:
            logger.warning("Failed to update %s: %s", pos.id, e)

    pos_mgr.print_positions()

    # Check for exit signals
    signals = pos_mgr.check_exit_signals()
    if signals:
        print(f"\n  Exit signals pending ({len(signals)}):")
        for pos, reason in signals:
            print(f"    {pos.pair_label}: {reason} "
                  f"(spread {pos.entry_spread:.4f} -> {pos.current_spread:.4f})")


# -- Mode: status --------------------------------------------------------------

async def cmd_status(args):
    _header("Account Status")

    print("\n  -- Kalshi --")
    try:
        kalshi = KalshiClient()
        bal = kalshi.get_balance()
        cents = bal.get("balance", 0)
        print(f"  Balance: ${cents / 100:.2f}")
        positions = kalshi.get_positions()
        if positions:
            print(f"  Positions ({len(positions)}):")
            for p in positions:
                print(f"    {p.ticker}: {p.position} contracts (avg {p.average_price}c)")
        else:
            print("  No open positions")
    except Exception as e:
        print(f"  Error connecting to Kalshi: {e}")

    print("\n  -- Polymarket --")
    try:
        poly = PolymarketClient()
        usdc = poly.get_usdc_balance()
        print(f"  USDC Balance: ${usdc:.2f}" if usdc is not None else "  Balance: unavailable")
        async with aiohttp.ClientSession() as session:
            positions = await PolymarketClient.fetch_positions(session, config.POLY_FUNDER)
        active = [p for p in positions if float(p.get("size", 0)) > 0.01]
        if active:
            print(f"  Positions ({len(active)}):")
            for p in active:
                title = p.get("title", "?")[:40]
                outcome = p.get("outcome", "?")
                size = float(p.get("size", 0))
                print(f"    {title} [{outcome}]: {size:.2f} shares")
        else:
            print("  No open positions")
    except Exception as e:
        print(f"  Error connecting to Polymarket: {e}")

    # Also show arb positions
    pos_mgr = PositionManager()
    if pos_mgr.positions:
        print("\n  -- Arb Positions --")
        pos_mgr.print_positions()


# -- CLI -----------------------------------------------------------------------

def main():
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Prediction Market Arbitrage - Polymarket x Kalshi"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("discover", help="Show active markets on both platforms")

    match_p = sub.add_parser("match", help="Show matched market pairs")
    match_p.add_argument("--min-score", type=float, default=55.0, help="Minimum match score (0-100)")

    sub.add_parser("scan", help="One-shot spread scan")

    mon_p = sub.add_parser("monitor", help="Continuous monitoring (entries + exits)")
    mon_p.add_argument("--scan-only", action="store_true", help="Monitor without executing")

    sub.add_parser("execute", help="Scan and enter spread opportunities")
    sub.add_parser("positions", help="Show open arb positions with live P&L")
    sub.add_parser("status", help="Show account balances and positions")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "discover": cmd_discover,
        "match": cmd_match,
        "scan": cmd_scan,
        "monitor": cmd_monitor,
        "execute": cmd_execute,
        "positions": cmd_positions,
        "status": cmd_status,
    }

    asyncio.run(commands[args.command](args))


if __name__ == "__main__":
    main()
