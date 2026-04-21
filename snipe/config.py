"""
Runtime configuration for the snipe strategy.

All environment variables are prefixed ``SNIPE_`` so they do not collide with
the ``ARB_`` settings used by the cross-platform arbitrage engine.  The two
strategies can run on the same host and share ``.env`` without interfering.
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _bool_env(key: str, default: str) -> bool:
    return os.getenv(key, default).strip().lower() in ("true", "1", "yes", "on")


# ── Market identity ─────────────────────────────────────────
# Polymarket lists BTC up/down markets with slugs like
# ``btc-updown-15m-<unix_ts>`` for 15-minute windows.  The 5-minute windows
# are assumed to follow the same pattern (``btc-updown-5m-<unix_ts>``).  If
# Polymarket changes the slug format, override ``SNIPE_POLY_SLUG_PATTERN``
# without touching code.
SNIPE_WINDOW_MINUTES = int(os.getenv("SNIPE_WINDOW_MINUTES", "5"))
SNIPE_POLY_SLUG_PATTERN = os.getenv(
    "SNIPE_POLY_SLUG_PATTERN",
    "btc-updown-5m-{ts}",
)

# Gamma search fallback used by the probe command when direct slug lookup
# returns nothing.  Keeps us unblocked if the slug convention ever changes.
SNIPE_GAMMA_SEARCH_QUERY = os.getenv("SNIPE_GAMMA_SEARCH_QUERY", "bitcoin up or down")


# ── Polling cadence ─────────────────────────────────────────
# Normal polling cadence in seconds (most of the window).
SNIPE_POLL_INTERVAL_S = float(os.getenv("SNIPE_POLL_INTERVAL_S", "1.0"))

# Tighter cadence inside the tail of the window, where all the interesting
# behavior happens.  Kept above 0.2s to stay polite on Gamma/CLOB.
SNIPE_POLL_INTERVAL_TAIL_S = float(os.getenv("SNIPE_POLL_INTERVAL_TAIL_S", "0.3"))

# Seconds remaining in the window below which the tail cadence kicks in.
SNIPE_TAIL_WINDOW_S = float(os.getenv("SNIPE_TAIL_WINDOW_S", "45"))


# ── Entry gates (Phase 2) ───────────────────────────────────
# None of these are consulted by the Phase 1 monitor; they are defined here
# so the whole strategy lives in one config file.
SNIPE_MIN_SECONDS_REMAINING = float(os.getenv("SNIPE_MIN_SECONDS_REMAINING", "3"))
SNIPE_MAX_SECONDS_REMAINING = float(os.getenv("SNIPE_MAX_SECONDS_REMAINING", "15"))
SNIPE_MIN_ENTRY_PRICE = float(os.getenv("SNIPE_MIN_ENTRY_PRICE", "0.95"))
SNIPE_MAX_ENTRY_PRICE = float(os.getenv("SNIPE_MAX_ENTRY_PRICE", "0.99"))
SNIPE_MIN_LEADER_PERSIST_TICKS = int(os.getenv("SNIPE_MIN_LEADER_PERSIST_TICKS", "2"))
SNIPE_MIN_TOP_OF_BOOK_SIZE = float(os.getenv("SNIPE_MIN_TOP_OF_BOOK_SIZE", "10"))


# ── Sizing & budgets ────────────────────────────────────────
SNIPE_POSITION_USD = float(os.getenv("SNIPE_POSITION_USD", "5.0"))
SNIPE_MAX_ENTRIES_PER_WINDOW = int(os.getenv("SNIPE_MAX_ENTRIES_PER_WINDOW", "1"))
SNIPE_MAX_SPEND_PER_DAY_USD = float(os.getenv("SNIPE_MAX_SPEND_PER_DAY_USD", "50.0"))
SNIPE_MAX_OPEN_POSITIONS = int(os.getenv("SNIPE_MAX_OPEN_POSITIONS", "3"))


# ── Live arming (fail closed) ───────────────────────────────
# Independent of the arb engine's ARB_DRY_RUN / ARB_ENABLE_LIVE so the two
# strategies have independent kill switches.
SNIPE_DRY_RUN = _bool_env("SNIPE_DRY_RUN", "true")
SNIPE_ENABLE_LIVE = _bool_env("SNIPE_ENABLE_LIVE", "false")
SNIPE_REQUIRE_BALANCE_CHECK = _bool_env("SNIPE_REQUIRE_BALANCE_CHECK", "true")
SNIPE_MIN_POLY_BALANCE_USD = float(os.getenv("SNIPE_MIN_POLY_BALANCE_USD", "10.0"))


# ── Output paths ────────────────────────────────────────────
SNIPE_DATA_DIR = os.getenv("SNIPE_DATA_DIR", "data/snipe")


def snipe_live_mode_requested() -> bool:
    """Both flags must be set explicitly for live orders to be sent."""
    return (not SNIPE_DRY_RUN) and SNIPE_ENABLE_LIVE


def snipe_live_mode_armed() -> bool:
    """Alias for live_mode_requested; mirrors the arb engine's API surface."""
    return snipe_live_mode_requested()
