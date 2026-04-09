import os
from dotenv import load_dotenv

load_dotenv()

# ── Polymarket ──────────────────────────────────────────────
POLY_API_KEY = os.getenv("POLY_API_KEY")
POLY_API_SECRET = os.getenv("POLY_API_SECRET")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE")
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY")
POLY_FUNDER = os.getenv("POLY_FUNDER")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")

# ── Kalshi ──────────────────────────────────────────────────
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY_PEM")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")
KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://trading-api.kalshi.com/trade-api/v2")
KALSHI_WS_URL = os.getenv("KALSHI_WS_URL", "wss://trading-api.kalshi.com/trade-api/ws/v2")

# ── Arbitrage Engine ───────────────────────────────────────
ARB_SCAN_INTERVAL = int(os.getenv("ARB_SCAN_INTERVAL", "5"))
ARB_BTC15_ONLY = os.getenv("ARB_BTC15_ONLY", "true").lower() in ("true", "1", "yes")
ARB_MIN_EDGE = float(os.getenv("ARB_MIN_EDGE", "0.05"))
ARB_MAX_POSITION_USD = float(os.getenv("ARB_MAX_POSITION_USD", "50.0"))
ARB_MAX_DAILY_SPEND = float(os.getenv("ARB_MAX_DAILY_SPEND", "500.0"))
ARB_DRY_RUN = os.getenv("ARB_DRY_RUN", "true").lower() in ("true", "1", "yes")
ARB_ENABLE_LIVE = os.getenv("ARB_ENABLE_LIVE", "false").lower() in ("true", "1", "yes")
ARB_REQUIRE_BALANCE_CHECK = os.getenv("ARB_REQUIRE_BALANCE_CHECK", "true").lower() in (
    "true", "1", "yes"
)
ARB_MIN_KALSHI_BALANCE_USD = float(os.getenv("ARB_MIN_KALSHI_BALANCE_USD", "25.0"))
ARB_MIN_POLY_BALANCE_USD = float(os.getenv("ARB_MIN_POLY_BALANCE_USD", "25.0"))
ARB_MAX_OPEN_POSITIONS = int(os.getenv("ARB_MAX_OPEN_POSITIONS", "5"))

# BTC 15-minute strategy controls
ARB_BTC15_TIME_GATING = os.getenv("ARB_BTC15_TIME_GATING", "true").lower() in (
    "true", "1", "yes"
)
ARB_ENTRY_MIN_SECONDS_IN_WINDOW = int(os.getenv("ARB_ENTRY_MIN_SECONDS_IN_WINDOW", "45"))
ARB_ENTRY_MAX_SECONDS_IN_WINDOW = int(os.getenv("ARB_ENTRY_MAX_SECONDS_IN_WINDOW", "780"))
ARB_ENTRY_COOLDOWN_SECONDS_AFTER_ROLLOVER = int(
    os.getenv("ARB_ENTRY_COOLDOWN_SECONDS_AFTER_ROLLOVER", "20")
)
ARB_MIN_EDGE_PERSIST_SCANS = int(os.getenv("ARB_MIN_EDGE_PERSIST_SCANS", "2"))
ARB_MAX_POLY_OVERROUND = float(os.getenv("ARB_MAX_POLY_OVERROUND", "0.04"))
ARB_MIN_KALSHI_LEVEL_QTY = int(os.getenv("ARB_MIN_KALSHI_LEVEL_QTY", "10"))
ARB_MAX_SIGNAL_AGE_SECONDS = int(os.getenv("ARB_MAX_SIGNAL_AGE_SECONDS", "8"))

# Limit order / execution controls
ARB_POLY_LIMIT_OFFSET = float(os.getenv("ARB_POLY_LIMIT_OFFSET", "0.00"))
ARB_KALSHI_LIMIT_OFFSET_CENTS = int(os.getenv("ARB_KALSHI_LIMIT_OFFSET_CENTS", "0"))
ARB_ORDER_REPRICE_ATTEMPTS = int(os.getenv("ARB_ORDER_REPRICE_ATTEMPTS", "0"))
ARB_ORDER_TIMEOUT_SECONDS = int(os.getenv("ARB_ORDER_TIMEOUT_SECONDS", "4"))
ARB_ALLOW_PARTIAL_FILLS = os.getenv("ARB_ALLOW_PARTIAL_FILLS", "false").lower() in (
    "true", "1", "yes"
)
ARB_ENTRY_MARKETABLE = os.getenv("ARB_ENTRY_MARKETABLE", "true").lower() in (
    "true", "1", "yes"
)
ARB_POLY_ENTRY_AGGRESSION = float(os.getenv("ARB_POLY_ENTRY_AGGRESSION", "0.01"))
ARB_KALSHI_ENTRY_AGGRESSION_CENTS = int(os.getenv("ARB_KALSHI_ENTRY_AGGRESSION_CENTS", "1"))
ARB_EXIT_LIMIT_ONLY = os.getenv("ARB_EXIT_LIMIT_ONLY", "true").lower() in (
    "true", "1", "yes"
)
ARB_POLY_EXIT_PASSIVE_OFFSET = float(os.getenv("ARB_POLY_EXIT_PASSIVE_OFFSET", "0.01"))
ARB_KALSHI_EXIT_PASSIVE_OFFSET_CENTS = int(os.getenv("ARB_KALSHI_EXIT_PASSIVE_OFFSET_CENTS", "1"))

# ── Logging ─────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def resolve_kalshi_pem() -> str | None:
    """Return the Kalshi private key PEM string, loading from file if needed."""
    if KALSHI_PRIVATE_KEY_PEM:
        return KALSHI_PRIVATE_KEY_PEM.replace("\\n", "\n")
    if KALSHI_PRIVATE_KEY_PATH:
        with open(KALSHI_PRIVATE_KEY_PATH, "r") as f:
            return f.read()
    return None


def live_mode_requested() -> bool:
    """
    Live mode requires both:
      - ARB_DRY_RUN=false
      - ARB_ENABLE_LIVE=true
    """
    return (not ARB_DRY_RUN) and ARB_ENABLE_LIVE


def live_mode_armed() -> bool:
    """
    Live mode is armed when both ARB_DRY_RUN=false and ARB_ENABLE_LIVE=true.
    """
    return live_mode_requested()
