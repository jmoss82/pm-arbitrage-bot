import os
import base64
from dotenv import load_dotenv

load_dotenv()


def _strip_wrapping_quotes(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _kalshi_default_urls() -> tuple[str, str]:
    env = os.getenv("KALSHI_ENV", "").strip().lower()
    if env in ("demo", "paper", "sandbox", "test", "elections"):
        return (
            "https://api.elections.kalshi.com/trade-api/v2",
            "wss://api.elections.kalshi.com/trade-api/ws/v2",
        )
    return (
        "https://trading-api.kalshi.com/trade-api/v2",
        "wss://trading-api.kalshi.com/trade-api/ws/v2",
    )

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
KALSHI_PRIVATE_KEY_PEM = _strip_wrapping_quotes(os.getenv("KALSHI_PRIVATE_KEY_PEM"))
KALSHI_PRIVATE_KEY_BASE64 = _strip_wrapping_quotes(os.getenv("KALSHI_PRIVATE_KEY_BASE64"))
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")
_DEFAULT_KALSHI_BASE_URL, _DEFAULT_KALSHI_WS_URL = _kalshi_default_urls()
KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", _DEFAULT_KALSHI_BASE_URL)
KALSHI_WS_URL = os.getenv("KALSHI_WS_URL", _DEFAULT_KALSHI_WS_URL)

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
ARB_MAX_OPEN_POSITIONS = int(os.getenv("ARB_MAX_OPEN_POSITIONS", "1"))

# BTC 15-minute strategy controls
ARB_BTC15_TIME_GATING = os.getenv("ARB_BTC15_TIME_GATING", "true").lower() in (
    "true", "1", "yes"
)
ARB_ENTRY_MIN_SECONDS_IN_WINDOW = int(os.getenv("ARB_ENTRY_MIN_SECONDS_IN_WINDOW", "45"))
ARB_ENTRY_MAX_SECONDS_IN_WINDOW = int(os.getenv("ARB_ENTRY_MAX_SECONDS_IN_WINDOW", "780"))
ARB_ENTRY_COOLDOWN_SECONDS_AFTER_ROLLOVER = int(
    os.getenv("ARB_ENTRY_COOLDOWN_SECONDS_AFTER_ROLLOVER", "20")
)
ARB_FORCE_EXIT_SECONDS_REMAINING = int(os.getenv("ARB_FORCE_EXIT_SECONDS_REMAINING", "180"))
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
ARB_EXIT_REPRICE_ATTEMPTS = int(os.getenv("ARB_EXIT_REPRICE_ATTEMPTS", "2"))
ARB_EXIT_FILL_TIMEOUT_SECONDS = int(os.getenv("ARB_EXIT_FILL_TIMEOUT_SECONDS", "2"))
ARB_ENTRY_MARKETABLE = os.getenv("ARB_ENTRY_MARKETABLE", "true").lower() in (
    "true", "1", "yes"
)
ARB_POLY_ENTRY_AGGRESSION = float(os.getenv("ARB_POLY_ENTRY_AGGRESSION", "0.01"))
ARB_KALSHI_ENTRY_AGGRESSION_CENTS = int(os.getenv("ARB_KALSHI_ENTRY_AGGRESSION_CENTS", "1"))
ARB_EXIT_LIMIT_ONLY = os.getenv("ARB_EXIT_LIMIT_ONLY", "false").lower() in (
    "true", "1", "yes"
)
ARB_POLY_EXIT_PASSIVE_OFFSET = float(os.getenv("ARB_POLY_EXIT_PASSIVE_OFFSET", "0.01"))
ARB_KALSHI_EXIT_PASSIVE_OFFSET_CENTS = int(os.getenv("ARB_KALSHI_EXIT_PASSIVE_OFFSET_CENTS", "1"))
ARB_ESTIMATED_ROUND_TRIP_SLIPPAGE = float(os.getenv("ARB_ESTIMATED_ROUND_TRIP_SLIPPAGE", "0.01"))
ARB_POLY_SELL_SIZE_FACTOR = float(os.getenv("ARB_POLY_SELL_SIZE_FACTOR", "0.95"))
ARB_EXIT_COOLDOWN_SECONDS = int(os.getenv("ARB_EXIT_COOLDOWN_SECONDS", "60"))

# ── Logging ─────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def resolve_kalshi_pem() -> str | None:
    """Return the Kalshi private key PEM string, loading from file if needed."""
    if KALSHI_PRIVATE_KEY_PEM:
        return KALSHI_PRIVATE_KEY_PEM.replace("\\n", "\n")
    if KALSHI_PRIVATE_KEY_BASE64:
        decoded = base64.b64decode(KALSHI_PRIVATE_KEY_BASE64).decode()
        return _strip_wrapping_quotes(decoded)
    if KALSHI_PRIVATE_KEY_PATH:
        with open(KALSHI_PRIVATE_KEY_PATH, "r") as f:
            return f.read()
    return None


def kalshi_config_summary() -> dict[str, str | bool]:
    pem = resolve_kalshi_pem()
    base_url = KALSHI_BASE_URL
    env_name = "demo" if "elections.kalshi.com" in base_url else "prod"
    return {
        "env": env_name,
        "base_url": base_url,
        "has_api_key": bool(KALSHI_API_KEY_ID),
        "has_private_key": bool(pem),
        "private_key_source": (
            "pem"
            if KALSHI_PRIVATE_KEY_PEM
            else "base64"
            if KALSHI_PRIVATE_KEY_BASE64
            else "path"
            if KALSHI_PRIVATE_KEY_PATH
            else "missing"
        ),
    }


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
