"""
Passive fair-value shadow model for BTC 5-minute markets.

This module does not place orders.  It estimates a simple probability that
BTC finishes above/below the window's Price to Beat, compares that fair value
to the current Polymarket asks, and tracks hypothetical entries for research.
"""
from __future__ import annotations

import csv
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import config
from .loop import LoopContext, WindowAccumulator
from .reference_price import ReferencePriceFeed, ReferenceSnapshot


FV_FIELDS = [
    "ts_iso",
    "window_slug",
    "seconds_remaining",
    "side",
    "ask",
    "model_prob",
    "edge",
    "p_up",
    "p_down",
    "distance_usd",
    "expected_move_usd",
    "sigma_usd_per_sqrt_s",
    "resolved_side",
    "result",
    "paper_pnl_usd",
]


CALIBRATION_FIELDS = [
    "ts_iso",
    "window_slug",
    "kind",
    "seconds_remaining",
    "distance_usd",
    "expected_move_usd",
    "sigma_usd_per_sqrt_s",
    "p_up",
    "p_down",
    "up_ask",
    "up_ask_size",
    "up_edge",
    "down_ask",
    "down_ask_size",
    "down_edge",
    "total_mid",
    "leader_side",
    "resolved_side",
    "final_up_mid",
    "final_down_mid",
]


@dataclass(frozen=True)
class FairValueEstimate:
    p_up: float
    p_down: float
    distance_usd: float
    expected_move_usd: float
    sigma_usd_per_sqrt_s: float


@dataclass
class ShadowSignal:
    ts_utc: datetime
    window_slug: str
    side: str
    ask: float
    model_prob: float
    edge: float
    p_up: float
    p_down: float
    distance_usd: float
    expected_move_usd: float
    sigma_usd_per_sqrt_s: float
    seconds_remaining: float


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _fmt_opt(value: object, spec: str) -> str:
    if value is None:
        return ""
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return ""


def _fmt_signed(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:+.4f}"


class FairValueModel:
    """Distance/time/volatility model for binary UP/DOWN fair value."""

    def __init__(
        self,
        *,
        lookback_s: float,
        fallback_sigma: float,
        min_expected_move_usd: float,
    ) -> None:
        self._lookback_s = lookback_s
        self._fallback_sigma = fallback_sigma
        self._min_expected_move_usd = min_expected_move_usd
        self._prices: deque[tuple[datetime, int, float]] = deque()

    def observe(self, ref: ReferenceSnapshot, at: datetime) -> None:
        if ref.current_price is None:
            return
        tick_key = ref.last_tick_ts_ms or int(at.timestamp() * 1000)
        if self._prices and self._prices[-1][1] == tick_key:
            return
        self._prices.append((at, tick_key, ref.current_price))
        cutoff_s = max(self._lookback_s * 2.0, self._lookback_s + 30.0)
        while self._prices and (at - self._prices[0][0]).total_seconds() > cutoff_s:
            self._prices.popleft()

    def estimate(self, ref: ReferenceSnapshot, seconds_remaining: float) -> Optional[FairValueEstimate]:
        distance = ref.distance_usd()
        if distance is None:
            return None

        sigma = self._sigma_usd_per_sqrt_s()
        effective_seconds = max(seconds_remaining, 0.1)
        expected_move = max(
            self._min_expected_move_usd,
            sigma * math.sqrt(effective_seconds),
        )
        z = distance / expected_move
        p_up = min(0.999, max(0.001, normal_cdf(z)))
        return FairValueEstimate(
            p_up=p_up,
            p_down=1.0 - p_up,
            distance_usd=distance,
            expected_move_usd=expected_move,
            sigma_usd_per_sqrt_s=sigma,
        )

    def _sigma_usd_per_sqrt_s(self) -> float:
        if len(self._prices) < 2:
            return self._fallback_sigma
        now = self._prices[-1][0]
        recent = [
            row for row in self._prices
            if (now - row[0]).total_seconds() <= self._lookback_s
        ]
        if len(recent) < 2:
            return self._fallback_sigma

        samples: list[float] = []
        for (t0, _, p0), (t1, _, p1) in zip(recent, recent[1:]):
            dt = max((t1 - t0).total_seconds(), 0.001)
            samples.append((p1 - p0) / math.sqrt(dt))
        if not samples:
            return self._fallback_sigma

        rms = math.sqrt(sum(x * x for x in samples) / len(samples))
        return max(self._fallback_sigma, rms)


class FairValueShadowTracker:
    """Tick/window handlers that track hypothetical FV entries."""

    def __init__(
        self,
        *,
        ref_feed: ReferencePriceFeed,
        csv_path: Path,
        out: Callable[[str], None],
        calibration_path: Optional[Path] = None,
    ) -> None:
        self._ref_feed = ref_feed
        self._csv_path = csv_path
        self._calibration_path = calibration_path
        self._out = out
        self._model = FairValueModel(
            lookback_s=config.SNIPE_FV_VOL_LOOKBACK_S,
            fallback_sigma=config.SNIPE_FV_FALLBACK_VOL_USD_PER_SQRT_S,
            min_expected_move_usd=config.SNIPE_FV_MIN_EXPECTED_MOVE_USD,
        )
        self._windows_seen: set[str] = set()
        self._signals_by_window: dict[str, ShadowSignal] = {}
        self._settled_windows: set[str] = set()
        self._signals = 0
        self._wins = 0
        self._losses = 0
        self._paper_pnl = 0.0
        self._edge_sum = 0.0
        self._entry_price_sum = 0.0
        self._side_counts = {"up": 0, "down": 0}
        self._price_buckets = {
            "<0.70": 0,
            "0.70-0.85": 0,
            "0.85-0.95": 0,
            ">=0.95": 0,
        }
        self._last_summary_at = 0.0
        self._calibration_rows = 0
        self._calibrated_windows: set[str] = set()
        self._last_calibration_at: dict[str, float] = {}
        self._init_csv()
        if self._calibration_path is not None:
            self._init_calibration_csv()

    async def on_tick(self, ctx: LoopContext) -> None:
        if not config.SNIPE_FV_SHADOW_ENABLED:
            return
        self._windows_seen.add(ctx.window.slug)

        ref = self._ref_feed.snapshot()
        now = ctx.tick.ts_utc
        self._model.observe(ref, now)

        if ctx.window.slug in self._signals_by_window:
            return
        if not ref.is_usable(config.SNIPE_REF_STALE_S, now=now):
            return
        if ref.window_slug and ref.window_slug != ctx.window.slug:
            return
        if not (
            config.SNIPE_FV_MIN_SECONDS_REMAINING
            <= ctx.tick.seconds_remaining
            <= config.SNIPE_FV_MAX_SECONDS_REMAINING
        ):
            return

        estimate = self._model.estimate(ref, ctx.tick.seconds_remaining)
        if estimate is None:
            return

        self._maybe_log_calibration(ctx, estimate)

        signal = self._best_signal(ctx, estimate)
        if signal is None:
            self._maybe_print_summary()
            return

        self._signals_by_window[ctx.window.slug] = signal
        self._signals += 1
        self._edge_sum += signal.edge
        self._entry_price_sum += signal.ask
        self._side_counts[signal.side] += 1
        self._price_buckets[self._price_bucket(signal.ask)] += 1
        self._append_signal(signal)
        self._out(
            "  [fv] signal "
            f"{signal.side.upper()} edge={signal.edge:+.3f} "
            f"ask={signal.ask:.2f} fair={signal.model_prob:.2f} "
            f"t-{signal.seconds_remaining:.1f}s d={signal.distance_usd:+.2f}"
        )
        self._maybe_print_summary(force=True)

    async def on_window_end(self, acc: WindowAccumulator) -> None:
        if not config.SNIPE_FV_SHADOW_ENABLED:
            return
        self._windows_seen.add(acc.window.slug)
        if acc.window.slug in self._settled_windows:
            return
        if datetime.now(timezone.utc) < acc.window.end:
            self._maybe_print_summary(force=True)
            return
        if acc.last_tick is None or acc.last_tick.leader_side not in ("up", "down"):
            return

        resolved = acc.last_tick.leader_side
        self._log_calibration_close(acc, resolved)

        signal = self._signals_by_window.get(acc.window.slug)
        if signal is None:
            self._maybe_print_summary(force=True)
            return

        self._settled_windows.add(acc.window.slug)
        won = signal.side == resolved
        if won:
            self._wins += 1
        else:
            self._losses += 1
        shares = config.SNIPE_POSITION_USD / signal.ask
        pnl = (shares if won else 0.0) - config.SNIPE_POSITION_USD
        self._paper_pnl += pnl
        self._append_resolution(signal, resolved, "win" if won else "loss", pnl)
        self._out(
            "  [fv] settle "
            f"{signal.side.upper()}->{resolved.upper()} "
            f"{'WIN' if won else 'LOSS'} pnl={pnl:+.2f}"
        )
        self.print_summary(force=True)

    def print_summary(self, *, force: bool = False) -> None:
        if not force and self._signals == 0 and self._calibration_rows == 0:
            return
        settled = self._wins + self._losses
        win_rate = (self._wins / settled * 100.0) if settled else 0.0
        avg_edge = self._edge_sum / self._signals if self._signals else 0.0
        avg_entry = self._entry_price_sum / self._signals if self._signals else 0.0
        calib_part = (
            f" calib_rows={self._calibration_rows}"
            if self._calibration_path is not None
            else ""
        )
        self._out(
            "  [fv] summary "
            f"windows={len(self._windows_seen)} signals={self._signals} settled={settled} "
            f"wins={self._wins} losses={self._losses} "
            f"win_rate={win_rate:.1f}% avg_entry={avg_entry:.3f} "
            f"avg_edge={avg_edge:+.3f} paper_pnl={self._paper_pnl:+.2f}{calib_part}"
        )

    def _best_signal(
        self,
        ctx: LoopContext,
        estimate: FairValueEstimate,
    ) -> Optional[ShadowSignal]:
        min_ask = config.SNIPE_FV_MIN_ASK
        candidates: list[tuple[str, float, float, float]] = []
        up_ask = ctx.tick.up.get("ask")
        up_size = ctx.tick.up.get("ask_size") or 0.0
        if (
            self._valid_ask(up_ask)
            and float(up_ask) >= min_ask
            and up_size >= config.SNIPE_MIN_TOP_OF_BOOK_SIZE
        ):
            edge = estimate.p_up - float(up_ask)
            candidates.append(("up", float(up_ask), estimate.p_up, edge))

        down_ask = ctx.tick.down.get("ask")
        down_size = ctx.tick.down.get("ask_size") or 0.0
        if (
            self._valid_ask(down_ask)
            and float(down_ask) >= min_ask
            and down_size >= config.SNIPE_MIN_TOP_OF_BOOK_SIZE
        ):
            edge = estimate.p_down - float(down_ask)
            candidates.append(("down", float(down_ask), estimate.p_down, edge))

        if not candidates:
            return None
        side, ask, model_prob, edge = max(candidates, key=lambda row: row[3])
        if edge < config.SNIPE_FV_MIN_EDGE:
            return None
        return ShadowSignal(
            ts_utc=ctx.tick.ts_utc,
            window_slug=ctx.window.slug,
            side=side,
            ask=ask,
            model_prob=model_prob,
            edge=edge,
            p_up=estimate.p_up,
            p_down=estimate.p_down,
            distance_usd=estimate.distance_usd,
            expected_move_usd=estimate.expected_move_usd,
            sigma_usd_per_sqrt_s=estimate.sigma_usd_per_sqrt_s,
            seconds_remaining=ctx.tick.seconds_remaining,
        )

    def _maybe_print_summary(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_summary_at >= config.SNIPE_FV_SUMMARY_INTERVAL_S:
            self._last_summary_at = now
            self.print_summary(force=force)

    def _init_csv(self) -> None:
        self._csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._csv_path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=FV_FIELDS).writeheader()

    def _append_signal(self, signal: ShadowSignal) -> None:
        self._write_row(signal, "", "", "")

    def _append_resolution(
        self,
        signal: ShadowSignal,
        resolved_side: str,
        result: str,
        pnl: float,
    ) -> None:
        self._write_row(signal, resolved_side, result, f"{pnl:.6f}")

    def _write_row(
        self,
        signal: ShadowSignal,
        resolved_side: str,
        result: str,
        paper_pnl: str,
    ) -> None:
        with open(self._csv_path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=FV_FIELDS).writerow({
                "ts_iso": signal.ts_utc.isoformat(),
                "window_slug": signal.window_slug,
                "seconds_remaining": f"{signal.seconds_remaining:.2f}",
                "side": signal.side,
                "ask": f"{signal.ask:.4f}",
                "model_prob": f"{signal.model_prob:.4f}",
                "edge": f"{signal.edge:+.4f}",
                "p_up": f"{signal.p_up:.4f}",
                "p_down": f"{signal.p_down:.4f}",
                "distance_usd": f"{signal.distance_usd:+.2f}",
                "expected_move_usd": f"{signal.expected_move_usd:.2f}",
                "sigma_usd_per_sqrt_s": f"{signal.sigma_usd_per_sqrt_s:.4f}",
                "resolved_side": resolved_side,
                "result": result,
                "paper_pnl_usd": paper_pnl,
            })

    def _init_calibration_csv(self) -> None:
        assert self._calibration_path is not None
        self._calibration_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._calibration_path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=CALIBRATION_FIELDS).writeheader()

    def _maybe_log_calibration(
        self,
        ctx: LoopContext,
        estimate: FairValueEstimate,
    ) -> None:
        if (
            self._calibration_path is None
            or not config.SNIPE_FV_CALIBRATION_ENABLED
        ):
            return
        slug = ctx.window.slug
        now = time.monotonic()
        last = self._last_calibration_at.get(slug, 0.0)
        if (
            last
            and now - last < config.SNIPE_FV_CALIBRATION_INTERVAL_S
        ):
            return
        self._last_calibration_at[slug] = now
        self._calibrated_windows.add(slug)
        self._calibration_rows += 1

        up_ask = ctx.tick.up.get("ask")
        up_size = ctx.tick.up.get("ask_size")
        down_ask = ctx.tick.down.get("ask")
        down_size = ctx.tick.down.get("ask_size")
        up_edge = (
            estimate.p_up - float(up_ask) if self._valid_ask(up_ask) else None
        )
        down_edge = (
            estimate.p_down - float(down_ask) if self._valid_ask(down_ask) else None
        )

        self._write_calibration_row({
            "ts_iso": ctx.tick.ts_utc.isoformat(),
            "window_slug": slug,
            "kind": "tick",
            "seconds_remaining": f"{ctx.tick.seconds_remaining:.2f}",
            "distance_usd": f"{estimate.distance_usd:+.2f}",
            "expected_move_usd": f"{estimate.expected_move_usd:.2f}",
            "sigma_usd_per_sqrt_s": f"{estimate.sigma_usd_per_sqrt_s:.4f}",
            "p_up": f"{estimate.p_up:.4f}",
            "p_down": f"{estimate.p_down:.4f}",
            "up_ask": _fmt_opt(up_ask, ".4f"),
            "up_ask_size": _fmt_opt(up_size, ".2f"),
            "up_edge": _fmt_signed(up_edge),
            "down_ask": _fmt_opt(down_ask, ".4f"),
            "down_ask_size": _fmt_opt(down_size, ".2f"),
            "down_edge": _fmt_signed(down_edge),
            "total_mid": _fmt_opt(ctx.tick.total_mid, ".4f"),
            "leader_side": ctx.tick.leader_side or "",
            "resolved_side": "",
            "final_up_mid": "",
            "final_down_mid": "",
        })

    def _log_calibration_close(
        self,
        acc: WindowAccumulator,
        resolved: str,
    ) -> None:
        if (
            self._calibration_path is None
            or not config.SNIPE_FV_CALIBRATION_ENABLED
        ):
            return
        if acc.window.slug not in self._calibrated_windows:
            return
        last_tick = acc.last_tick
        self._write_calibration_row({
            "ts_iso": datetime.now(timezone.utc).isoformat(),
            "window_slug": acc.window.slug,
            "kind": "close",
            "seconds_remaining": "0.00",
            "distance_usd": "",
            "expected_move_usd": "",
            "sigma_usd_per_sqrt_s": "",
            "p_up": "",
            "p_down": "",
            "up_ask": "",
            "up_ask_size": "",
            "up_edge": "",
            "down_ask": "",
            "down_ask_size": "",
            "down_edge": "",
            "total_mid": "",
            "leader_side": "",
            "resolved_side": resolved,
            "final_up_mid": (
                _fmt_opt(last_tick.up.get("mid"), ".4f") if last_tick is not None else ""
            ),
            "final_down_mid": (
                _fmt_opt(last_tick.down.get("mid"), ".4f") if last_tick is not None else ""
            ),
        })

    def _write_calibration_row(self, row: dict) -> None:
        assert self._calibration_path is not None
        with open(self._calibration_path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=CALIBRATION_FIELDS).writerow(row)

    @staticmethod
    def _valid_ask(value: object) -> bool:
        try:
            ask = float(value)
        except (TypeError, ValueError):
            return False
        return 0.0 < ask < 1.0

    @staticmethod
    def _price_bucket(price: float) -> str:
        if price < 0.70:
            return "<0.70"
        if price < 0.85:
            return "0.70-0.85"
        if price < 0.95:
            return "0.85-0.95"
        return ">=0.95"
