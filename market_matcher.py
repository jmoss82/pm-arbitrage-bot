"""
Cross-platform market matcher — pairs equivalent Kalshi and Polymarket markets.

Strategy:
  1. Match at the Kalshi EVENT level (clean titles) against Polymarket questions.
  2. Use multi-signal scoring: fuzzy, keyword overlap, entity extraction, date alignment.
  3. Resolve matched events to specific tradeable Kalshi sub-markets.
  4. Support manual overrides via JSON mapping file.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from thefuzz import fuzz

from kalshi_client import KalshiClient, Market as KalshiMarket, Event as KalshiEvent
from polymarket_client import PolymarketMarket

logger = logging.getLogger(__name__)

MAPPING_FILE = Path("data/market_pairs.json")

STOP_WORDS = {
    "will", "the", "a", "an", "be", "to", "in", "on", "at", "by", "of",
    "or", "and", "for", "is", "it", "this", "that", "if", "do", "does",
    "before", "after", "above", "below", "between", "from", "up", "down",
    "how", "what", "when", "where", "who", "which", "than", "their", "them",
    "his", "her", "its", "our", "your", "not", "any", "all", "each",
    "has", "have", "had", "was", "were", "been", "being", "are", "am",
    "with", "into", "over", "under", "more", "most", "next", "new",
}


@dataclass
class MarketPair:
    """A matched pair: Kalshi event/market <-> Polymarket market."""
    kalshi_event: KalshiEvent | None
    kalshi_market: KalshiMarket | None
    poly: PolymarketMarket
    match_score: float
    match_method: str  # "manual", "fuzzy", "keyword"
    notes: str = ""

    @property
    def kalshi(self) -> KalshiMarket | None:
        return self.kalshi_market

    @property
    def label(self) -> str:
        if self.kalshi_event and self.kalshi_event.title:
            return self.kalshi_event.title
        if self.kalshi_market and self.kalshi_market.title:
            return self.kalshi_market.title
        return self.poly.question or "unknown"

    @property
    def kalshi_ticker(self) -> str:
        if self.kalshi_market:
            return self.kalshi_market.ticker
        if self.kalshi_event:
            return self.kalshi_event.event_ticker
        return ""


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[''\"()?{},.\!;:/\\\[\]\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_keywords(text: str) -> set[str]:
    words = _normalize(text).split()
    return {w for w in words if w not in STOP_WORDS and len(w) > 2}


def _extract_entities(text: str) -> set[str]:
    """
    Extract proper-noun-like entities: capitalized words, numbers, acronyms.
    These are the strongest matching signals.
    """
    entities = set()
    # Capitalized words (2+ chars)
    for m in re.finditer(r'\b([A-Z][a-zA-Z]{1,})\b', text):
        entities.add(m.group(1).lower())
    # Numbers and years
    for m in re.finditer(r'\b(\d{4})\b', text):
        entities.add(m.group(1))
    for m in re.finditer(r'\b(\d+\.?\d*%?)\b', text):
        entities.add(m.group(1))
    # Multi-word proper nouns (e.g. "Stanley Cup", "New York")
    for m in re.finditer(r'\b([A-Z][a-z]+ [A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b', text):
        entities.add(m.group(1).lower())
    return entities


def _date_signature(text: str) -> str | None:
    patterns = [
        r"\b(20\d{2})\b",
        r"\b(\w+ \d{1,2},? 20\d{2})\b",
        r"\b(Q[1-4] 20\d{2})\b",
        r"\b(20\d{2}-\d{2}-\d{2})\b",
    ]
    found = []
    for pat in patterns:
        found.extend(re.findall(pat, text, re.IGNORECASE))
    return " ".join(sorted(found)).strip() or None


def _compute_score(kalshi_title: str, poly_question: str) -> float:
    """
    Multi-signal similarity score (0-100).
    Emphasizes entity overlap heavily to prevent false positives.
    """
    norm_k = _normalize(kalshi_title)
    norm_p = _normalize(poly_question)

    # Token-sort fuzzy ratio
    fuzzy_score = fuzz.token_sort_ratio(norm_k, norm_p)

    # Keyword overlap (Jaccard)
    kw_k = _extract_keywords(kalshi_title)
    kw_p = _extract_keywords(poly_question)
    kw_jaccard = len(kw_k & kw_p) / len(kw_k | kw_p) if (kw_k and kw_p) else 0.0

    # Entity overlap (strongest signal — proper nouns, numbers, names)
    ent_k = _extract_entities(kalshi_title)
    ent_p = _extract_entities(poly_question)
    if ent_k and ent_p:
        ent_overlap = len(ent_k & ent_p) / min(len(ent_k), len(ent_p))
    else:
        ent_overlap = 0.0

    # Date alignment
    date_k = _date_signature(kalshi_title)
    date_p = _date_signature(poly_question)
    date_bonus = 5 if (date_k and date_p and date_k == date_p) else 0
    date_penalty = -10 if (date_k and date_p and date_k != date_p) else 0

    # Weighted combination: entities matter most
    combined = (
        0.30 * fuzzy_score +
        0.25 * (kw_jaccard * 100) +
        0.45 * (ent_overlap * 100) +
        date_bonus +
        date_penalty
    )
    return min(100.0, max(0.0, combined))


# ── Manual Mapping ────────────────────────────────────────────────────────

def load_manual_pairs() -> dict[str, str]:
    if not MAPPING_FILE.exists():
        return {}
    try:
        with open(MAPPING_FILE) as f:
            data = json.load(f)
        return data.get("pairs", {})
    except Exception as e:
        logger.warning("Failed to load manual pairs: %s", e)
        return {}


def save_manual_pairs(pairs: dict[str, str]):
    MAPPING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MAPPING_FILE, "w") as f:
        json.dump({"pairs": pairs}, f, indent=2)


# ── Event-Based Matching ─────────────────────────────────────────────────

def match_events_to_markets(
    kalshi_events: list[KalshiEvent],
    poly_markets: list[PolymarketMarket],
    kalshi_client: KalshiClient | None = None,
    min_score: float = 65.0,
) -> list[MarketPair]:
    """
    Match Kalshi events to Polymarket markets by title similarity.

    If a kalshi_client is provided, resolves matched events to their
    best tradeable sub-market.
    """
    manual_map = load_manual_pairs()
    pairs: list[MarketPair] = []
    used_events: set[str] = set()
    used_poly: set[str] = set()

    # Manual overrides
    poly_by_cid = {m.condition_id: m for m in poly_markets}
    for evt in kalshi_events:
        if evt.event_ticker in manual_map:
            cid = manual_map[evt.event_ticker]
            pm = poly_by_cid.get(cid)
            if pm:
                km = _resolve_best_market(evt, kalshi_client) if kalshi_client else None
                pairs.append(MarketPair(
                    kalshi_event=evt, kalshi_market=km, poly=pm,
                    match_score=100.0, match_method="manual",
                ))
                used_events.add(evt.event_ticker)
                used_poly.add(pm.condition_id)

    # Fuzzy matching
    candidates: list[tuple[float, KalshiEvent, PolymarketMarket]] = []
    for evt in kalshi_events:
        if evt.event_ticker in used_events or not evt.title:
            continue
        for pm in poly_markets:
            if pm.condition_id in used_poly or not pm.question:
                continue
            score = _compute_score(evt.title, pm.question)
            if score >= min_score:
                candidates.append((score, evt, pm))

    candidates.sort(key=lambda x: -x[0])

    for score, evt, pm in candidates:
        if evt.event_ticker in used_events or pm.condition_id in used_poly:
            continue
        km = _resolve_best_market(evt, kalshi_client) if kalshi_client else None
        pairs.append(MarketPair(
            kalshi_event=evt, kalshi_market=km, poly=pm,
            match_score=score, match_method="fuzzy",
        ))
        used_events.add(evt.event_ticker)
        used_poly.add(pm.condition_id)

    pairs.sort(key=lambda p: -p.match_score)
    return pairs


def _resolve_best_market(event: KalshiEvent, client: KalshiClient | None) -> KalshiMarket | None:
    """
    Given a Kalshi event, find its best tradeable sub-market.
    Prefers markets with order book activity.
    """
    if not client:
        return event.markets[0] if event.markets else None

    try:
        markets = client.get_markets(event_ticker=event.event_ticker, status="open")
        if not markets:
            return None

        # Prefer markets with bid/ask activity
        with_quotes = [m for m in markets if m.yes_bid is not None or m.yes_ask is not None]
        if with_quotes:
            return max(with_quotes, key=lambda m: (m.volume or 0))

        # Fall back to highest volume
        return max(markets, key=lambda m: (m.volume or 0))
    except Exception as e:
        logger.warning("Failed to resolve markets for %s: %s", event.event_ticker, e)
        return event.markets[0] if event.markets else None


# Legacy compat: match_markets wraps the event-based flow
def match_markets(
    kalshi_markets: list[KalshiMarket],
    poly_markets: list[PolymarketMarket],
    min_score: float = 65.0,
    kalshi_client: KalshiClient | None = None,
    kalshi_events: list[KalshiEvent] | None = None,
) -> list[MarketPair]:
    """
    Main matching entry point.
    If kalshi_events are provided, uses event-level matching (preferred).
    Otherwise falls back to market-level matching.
    """
    if kalshi_events:
        return match_events_to_markets(kalshi_events, poly_markets, kalshi_client, min_score)

    # Fallback: wrap markets as pseudo-events for matching
    pseudo_events = []
    for m in kalshi_markets:
        if m.title and "," not in m.title:
            evt = KalshiEvent(
                event_ticker=m.event_ticker or m.ticker,
                title=m.title,
                category=m.category,
                markets=[m],
            )
            pseudo_events.append(evt)
    return match_events_to_markets(pseudo_events, poly_markets, kalshi_client, min_score)


def print_pairs(pairs: list[MarketPair]):
    if not pairs:
        print("  No matched pairs found.")
        return

    for i, p in enumerate(pairs, 1):
        tradeable = "TRADEABLE" if p.kalshi_market else "no sub-market"
        print(f"\n  [{i}] Score: {p.match_score:.0f} ({p.match_method}) [{tradeable}]")
        if p.kalshi_event:
            print(f"      Kalshi event:  {p.kalshi_event.title}")
            print(f"      Kalshi ticker: {p.kalshi_ticker}")
        if p.kalshi_market:
            print(f"      Kalshi market: {p.kalshi_market.title or p.kalshi_market.ticker}")
            if p.kalshi_market.yes_bid is not None:
                print(f"      K price:       {p.kalshi_market.yes_bid}c / {p.kalshi_market.yes_ask}c")
        print(f"      Polymarket:    {p.poly.question}")
        if p.poly.price_yes is not None:
            print(f"      P price:       {p.poly.price_yes:.2f} / {p.poly.price_no:.2f}")
