"""
External Signal Aggregation for Polymarket Trading Bot.

Fetches probability estimates from free external sources (sportsbooks,
prediction markets, crypto options, commodity prices) and compares them
to current Polymarket prices to find divergences worth trading.

Designed for 24/7 operation on a 1 GB RAM VPS:
- All API calls wrapped in try/except
- Simple dict-based cache with TTL
- No heavy dependencies beyond requests
"""

import os
import re
import math
import time
import logging
import hashlib
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
from difflib import SequenceMatcher
from typing import Optional

import requests

log = logging.getLogger("autobot.signals")

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, object]] = {}
CACHE_TTL = 300  # 5 minutes


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, value):
    _cache[key] = (time.time(), value)
    # Evict old entries to keep memory bounded
    if len(_cache) > 500:
        cutoff = time.time() - CACHE_TTL
        to_del = [k for k, (ts, _) in _cache.items() if ts < cutoff]
        for k in to_del:
            del _cache[k]


def _safe_get(url: str, params: dict | None = None, headers: dict | None = None,
              timeout: int = 10) -> dict | list | None:
    """GET request with full error handling and caching."""
    cache_key = hashlib.md5(f"{url}|{params}".encode()).hexdigest()
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        _cache_set(cache_key, data)
        return data
    except Exception as e:
        log.debug(f"API call failed: {url} - {e}")
        return None


# ---------------------------------------------------------------------------
# Text matching utilities
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_keywords(question: str, min_len: int = 3) -> list[str]:
    """Extract meaningful keywords from a market question."""
    stop = {
        "will", "the", "and", "for", "this", "that", "with", "from",
        "have", "has", "been", "was", "were", "are", "not", "but",
        "what", "who", "how", "when", "where", "which", "does", "did",
        "can", "would", "could", "should", "may", "might", "shall",
        "than", "then", "them", "they", "their", "there", "these",
        "those", "other", "each", "every", "both", "few", "more",
        "most", "some", "any", "all", "into", "over", "under",
        "between", "through", "during", "before", "after", "above",
        "below", "about", "against", "along", "among", "around",
        "yes", "market", "question", "prediction",
    }
    words = _normalize(question).split()
    return [w for w in words if len(w) >= min_len and w not in stop]


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _keyword_overlap(keywords: list[str], text: str) -> float:
    """Fraction of keywords found in text."""
    if not keywords:
        return 0.0
    text_norm = _normalize(text)
    found = sum(1 for kw in keywords if kw in text_norm)
    return found / len(keywords)


# ---------------------------------------------------------------------------
# 1. Sports - Sportsbook Odds via The Odds API
# ---------------------------------------------------------------------------

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

if not ODDS_API_KEY:
    log.warning("ODDS_API_KEY not set — sportsbook signals (primary edge source) will be unavailable!")

# Map common sport names to Odds API sport keys
_SPORT_MAP = {
    "nba": "basketball_nba",
    "nhl": "icehockey_nhl",
    "nfl": "americanfootball_nfl",
    "mlb": "baseball_mlb",
    "premier league": "soccer_epl",
    "la liga": "soccer_spain_la_liga",
    "serie a": "soccer_italy_serie_a",
    "bundesliga": "soccer_germany_bundesliga",
    "ligue 1": "soccer_france_ligue_one",
    "champions league": "soccer_uefa_champs_league",
    "ufc": "mma_mixed_martial_arts",
    "mma": "mma_mixed_martial_arts",
}

# Common team name aliases -> canonical short name
_TEAM_ALIASES = {
    "thunder": "thunder", "okc": "thunder",
    "celtics": "celtics", "boston": "celtics",
    "lakers": "lakers", "los angeles lakers": "lakers",
    "warriors": "warriors", "golden state": "warriors",
    "bucks": "bucks", "milwaukee": "bucks",
    "nuggets": "nuggets", "denver": "nuggets",
    "cavaliers": "cavaliers", "cleveland": "cavaliers", "cavs": "cavaliers",
    "knicks": "knicks", "new york knicks": "knicks",
    "76ers": "76ers", "sixers": "76ers", "philadelphia": "76ers",
    "heat": "heat", "miami": "heat",
    "mavericks": "mavericks", "dallas": "mavericks", "mavs": "mavericks",
    "suns": "suns", "phoenix": "suns",
    "clippers": "clippers", "la clippers": "clippers",
    "hawks": "hawks", "atlanta": "hawks",
    "pacers": "pacers", "indiana": "pacers",
    "rockets": "rockets", "houston": "rockets",
    "grizzlies": "grizzlies", "memphis": "grizzlies",
    "timberwolves": "timberwolves", "minnesota": "timberwolves", "wolves": "timberwolves",
    "pelicans": "pelicans", "new orleans": "pelicans",
    "kings": "kings", "sacramento": "kings",
    "magic": "magic", "orlando": "magic",
    "raptors": "raptors", "toronto": "raptors",
    "bulls": "bulls", "chicago": "bulls",
    "nets": "nets", "brooklyn": "nets",
    "spurs": "spurs", "san antonio": "spurs",
    "blazers": "blazers", "trail blazers": "blazers", "portland": "blazers",
    "hornets": "hornets", "charlotte": "hornets",
    "wizards": "wizards", "washington": "wizards",
    "jazz": "jazz", "utah": "jazz",
    "pistons": "pistons", "detroit": "pistons",
    # NHL
    "oilers": "oilers", "edmonton": "oilers",
    "panthers": "panthers", "florida": "panthers",
    "rangers": "rangers", "new york rangers": "rangers",
    "bruins": "bruins", "boston bruins": "bruins",
    "avalanche": "avalanche", "colorado": "avalanche",
    "stars": "stars", "dallas stars": "stars",
    "maple leafs": "maple leafs", "leafs": "maple leafs",
    "canadiens": "canadiens", "montreal": "canadiens",
    "flames": "flames", "calgary": "flames",
    "jets": "jets", "winnipeg": "jets",
    "penguins": "penguins", "pittsburgh": "penguins",
}


def _extract_teams(question: str) -> list[str]:
    """Extract team names from a market question."""
    q = question.lower()
    teams = []
    for alias, canonical in _TEAM_ALIASES.items():
        if alias in q and canonical not in teams:
            teams.append(canonical)
    return teams


def _detect_sport(question: str) -> str | None:
    """Detect which sport API key to use."""
    q = question.lower()
    for keyword, sport_key in _SPORT_MAP.items():
        if keyword in q:
            return sport_key
    # Heuristic: if we found NBA team names, it's NBA
    teams = _extract_teams(question)
    if teams:
        # Check NBA teams first (most common on Polymarket)
        nba_teams = {
            "thunder", "celtics", "lakers", "warriors", "bucks", "nuggets",
            "cavaliers", "knicks", "76ers", "heat", "mavericks", "suns",
            "clippers", "hawks", "pacers", "rockets", "grizzlies",
            "timberwolves", "pelicans", "kings", "magic", "raptors",
            "bulls", "nets", "spurs", "blazers", "hornets", "wizards",
            "jazz", "pistons",
        }
        nhl_teams = {
            "oilers", "panthers", "rangers", "bruins", "avalanche", "stars",
            "maple leafs", "canadiens", "flames", "jets", "penguins",
        }
        if any(t in nba_teams for t in teams):
            return "basketball_nba"
        if any(t in nhl_teams for t in teams):
            return "icehockey_nhl"
    return None


def _american_to_prob(odds: int) -> float:
    """Convert American odds to implied probability."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    else:
        return abs(odds) / (abs(odds) + 100.0)


def _decimal_to_prob(odds: float) -> float:
    if odds <= 1.0:
        return 1.0
    return 1.0 / odds


def _remove_vig(probs: list[float]) -> list[float]:
    """Remove overround/vig from a set of implied probabilities."""
    total = sum(probs)
    if total <= 0:
        return probs
    return [p / total for p in probs]


def _fetch_sportsbook_signal(market: dict) -> dict | None:
    """
    Match a Polymarket sports market to sportsbook odds.
    Returns signal dict or None.
    """
    if not ODDS_API_KEY:
        return None

    question = market.get("question", "")
    sport_key = _detect_sport(question)
    if not sport_key:
        return None

    teams = _extract_teams(question)
    if len(teams) < 1:
        return None

    # Fetch odds
    data = _safe_get(
        f"{ODDS_API_BASE}/sports/{sport_key}/odds",
        params={
            "apiKey": ODDS_API_KEY,
            "regions": "us,eu",
            "markets": "h2h",
            "oddsFormat": "american",
        },
        timeout=15,
    )
    if not data or not isinstance(data, list):
        return None

    # Find matching event
    best_match = None
    best_score = 0.0

    for event in data:
        home = _normalize(event.get("home_team", ""))
        away = _normalize(event.get("away_team", ""))
        event_text = f"{home} {away}"

        # Check if our teams appear in this event
        match_count = sum(1 for t in teams if t in event_text or
                          any(alias in event_text for alias, canon in _TEAM_ALIASES.items() if canon == t))

        if match_count > 0:
            score = match_count / max(len(teams), 1)
            # Boost score if question text is similar to event description
            event_desc = f"{event.get('home_team', '')} vs {event.get('away_team', '')}"
            score += _similarity(question, event_desc) * 0.5
            if score > best_score:
                best_score = score
                best_match = event

    if not best_match or best_score < 0.3:
        return None

    # Determine which team/outcome the Polymarket question is about
    q_lower = question.lower()
    # The question is usually "Will X win?" or "X vs Y" - figure out which side
    target_team = teams[0] if teams else None

    # Collect implied probabilities from all bookmakers
    team_probs: dict[str, list[float]] = {}
    bookmaker_details = []

    for bookmaker in best_match.get("bookmakers", []):
        bk_name = bookmaker.get("title", "Unknown")
        for market_data in bookmaker.get("markets", []):
            if market_data.get("key") != "h2h":
                continue
            outcomes = market_data.get("outcomes", [])
            raw_probs = []
            outcome_map = {}
            for outcome in outcomes:
                name = _normalize(outcome.get("name", ""))
                odds_val = outcome.get("price", 0)
                prob = _american_to_prob(odds_val) if isinstance(odds_val, int) else 0
                raw_probs.append(prob)
                outcome_map[name] = prob

            # Remove vig
            if raw_probs and len(raw_probs) == len(outcomes):
                fair_probs = _remove_vig(raw_probs)
                for i, outcome in enumerate(outcomes):
                    name = _normalize(outcome.get("name", ""))
                    team_probs.setdefault(name, []).append(fair_probs[i])
                    # Track for details
                    if target_team and target_team in name:
                        bookmaker_details.append(
                            f"{bk_name}: {fair_probs[i]*100:.0f}%"
                        )

    if not team_probs:
        return None

    # Find the probability for our target team
    target_prob = None
    for team_name, probs in team_probs.items():
        if target_team and target_team in team_name:
            target_prob = sum(probs) / len(probs)
            break

    # Fallback: if question asks about "win", pick the team with higher mention
    if target_prob is None:
        # Pick the team most mentioned in question
        best_overlap = 0
        for team_name, probs in team_probs.items():
            overlap = _keyword_overlap(teams, team_name)
            if overlap > best_overlap:
                best_overlap = overlap
                target_prob = sum(probs) / len(probs)

    if target_prob is None:
        return None

    # Determine confidence based on number of bookmakers
    n_books = max(len(v) for v in team_probs.values()) if team_probs else 0
    if n_books >= 4:
        confidence = 0.9
    elif n_books >= 2:
        confidence = 0.75
    else:
        confidence = 0.55

    confidence *= min(1.0, best_score)  # Reduce if match quality is low

    return {
        "source": "sportsbook",
        "external_prob": round(target_prob, 4),
        "confidence": round(confidence, 2),
        "details": "; ".join(bookmaker_details[:5]) if bookmaker_details else f"Consensus from {n_books} bookmakers",
        "match_score": round(best_score, 2),
    }


# ---------------------------------------------------------------------------
# 2. Prediction Market Aggregation (Metaculus + Manifold)
# ---------------------------------------------------------------------------

def _fetch_metaculus_signal(market: dict) -> dict | None:
    """Search Metaculus for a similar question and return community probability."""
    question = market.get("question", "")
    keywords = _extract_keywords(question)
    if len(keywords) < 2:
        return None

    search_query = " ".join(keywords[:6])

    data = _safe_get(
        "https://www.metaculus.com/api2/questions/",
        params={
            "search": search_query,
            "status": "open",
            "type": "forecast",
            "limit": 5,
            "order_by": "-activity",
        },
        timeout=15,
    )

    if not data or not isinstance(data, dict):
        return None

    results = data.get("results", [])
    if not results:
        return None

    # Find best match by title similarity
    best = None
    best_sim = 0.0

    for q in results:
        title = q.get("title", "")
        sim = _similarity(question, title)
        # Also check keyword overlap
        overlap = _keyword_overlap(keywords, title)
        combined = sim * 0.6 + overlap * 0.4

        if combined > best_sim:
            best_sim = combined
            best = q

    if not best or best_sim < 0.25:
        return None

    # Get community prediction
    community_pred = best.get("community_prediction", {})
    prob = community_pred.get("full", {}).get("q2")  # median

    if prob is None:
        # Try alternative fields
        prob = best.get("my_predictions", {}).get("latest", {}).get("prediction")

    if prob is None or not isinstance(prob, (int, float)):
        return None

    # Check if question polarity matches (is it asking the same direction?)
    # Simple heuristic: if both contain "will" + similar subject, assume same direction
    prob = float(prob)

    # Confidence based on match quality and number of forecasters
    n_forecasters = best.get("number_of_forecasters", 0)
    confidence = min(0.85, best_sim * 0.7 + min(n_forecasters / 100, 0.3))

    return {
        "source": "metaculus",
        "external_prob": round(prob, 4),
        "confidence": round(confidence, 2),
        "details": f"Metaculus: {prob*100:.0f}% ({n_forecasters} forecasters, match={best_sim:.2f})",
        "match_score": round(best_sim, 2),
    }


def _fetch_manifold_signal(market: dict) -> dict | None:
    """Search Manifold Markets for a similar question."""
    question = market.get("question", "")
    keywords = _extract_keywords(question)
    if len(keywords) < 2:
        return None

    search_query = " ".join(keywords[:6])

    data = _safe_get(
        "https://api.manifold.markets/v0/search-markets",
        params={
            "term": search_query,
            "sort": "relevance",
            "limit": 5,
            "filter": "open",
        },
        timeout=15,
    )

    if not data or not isinstance(data, list):
        return None

    best = None
    best_sim = 0.0

    for m in data:
        title = m.get("question", "")
        sim = _similarity(question, title)
        overlap = _keyword_overlap(keywords, title)
        combined = sim * 0.6 + overlap * 0.4

        if combined > best_sim:
            best_sim = combined
            best = m

    if not best or best_sim < 0.25:
        return None

    prob = best.get("probability")
    if prob is None or not isinstance(prob, (int, float)):
        return None

    prob = float(prob)
    n_traders = best.get("uniqueBettorCount", 0)
    volume = best.get("volume", 0)

    confidence = min(0.80, best_sim * 0.6 + min(n_traders / 50, 0.2) + min(volume / 10000, 0.1))

    return {
        "source": "manifold",
        "external_prob": round(prob, 4),
        "confidence": round(confidence, 2),
        "details": f"Manifold: {prob*100:.0f}% ({n_traders} traders, ${volume:.0f} vol, match={best_sim:.2f})",
        "match_score": round(best_sim, 2),
    }


# ---------------------------------------------------------------------------
# 3. Crypto Price Targets (BTC/ETH will hit $X)
# ---------------------------------------------------------------------------

_CRYPTO_PATTERNS = [
    # "Will Bitcoin hit $100,000?" / "Will BTC reach $120k?"
    re.compile(
        r"(?:will|does|can)\s+(?:bitcoin|btc|ethereum|eth|solana|sol)"
        r".*?(?:hit|reach|exceed|surpass|above|over|break|cross)\s*"
        r"\$?([\d,]+\.?\d*)\s*([km])?",
        re.IGNORECASE
    ),
    # "Will BTC dip to $80,000?" / "Will Bitcoin drop below $X?"
    re.compile(
        r"(?:will|does|can)\s+(?:bitcoin|btc|ethereum|eth|solana|sol)"
        r".*?(?:dip|drop|fall|below|under|crash)\s*(?:to\s*)?"
        r"\$?([\d,]+\.?\d*)\s*([km])?",
        re.IGNORECASE
    ),
    # "Bitcoin above $X" / "BTC $100k"
    re.compile(
        r"(?:bitcoin|btc|ethereum|eth|solana|sol)"
        r".*?\$\s*([\d,]+\.?\d*)\s*([km])?",
        re.IGNORECASE
    ),
]

_CRYPTO_SYMBOLS = {
    "bitcoin": ("BTCUSDT", "BTC"),
    "btc": ("BTCUSDT", "BTC"),
    "ethereum": ("ETHUSDT", "ETH"),
    "eth": ("ETHUSDT", "ETH"),
    "solana": ("SOLUSDT", "SOL"),
    "sol": ("SOLUSDT", "SOL"),
}


def _parse_crypto_target(question: str) -> tuple[str, str, float, str] | None:
    """
    Parse crypto market question.
    Returns (binance_symbol, crypto_name, target_price, direction) or None.
    direction is 'above' or 'below'.
    """
    q = question.lower()

    # Determine which crypto
    symbol = None
    crypto_name = None
    for name, (sym, short) in _CRYPTO_SYMBOLS.items():
        if name in q:
            symbol = sym
            crypto_name = short
            break

    if not symbol:
        return None

    # Determine direction
    below_words = {"dip", "drop", "fall", "below", "under", "crash"}
    direction = "below" if any(w in q for w in below_words) else "above"

    # Extract target price
    for pattern in _CRYPTO_PATTERNS:
        match = pattern.search(question)
        if match:
            price_str = match.group(1).replace(",", "")
            try:
                target = float(price_str)
            except ValueError:
                continue
            suffix = match.group(2).lower() if match.group(2) else ""
            if suffix == "k":
                target *= 1000
            elif suffix == "m":
                target *= 1_000_000
            return symbol, crypto_name, target, direction

    return None


def _get_crypto_price(symbol: str) -> float | None:
    """Get current price from Binance."""
    data = _safe_get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": symbol},
        timeout=10,
    )
    if data and "price" in data:
        try:
            return float(data["price"])
        except (ValueError, TypeError):
            return None
    return None


def _get_crypto_volatility(symbol: str, days: int = 30) -> float | None:
    """
    Get annualized volatility from Binance klines (daily close prices).
    Returns annualized volatility as a fraction (e.g. 0.60 for 60%).
    """
    data = _safe_get(
        "https://api.binance.com/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": "1d",
            "limit": days + 1,
        },
        timeout=10,
    )
    if not data or not isinstance(data, list) or len(data) < 10:
        return None

    closes = []
    for candle in data:
        try:
            closes.append(float(candle[4]))  # close price
        except (IndexError, ValueError):
            continue

    if len(closes) < 10:
        return None

    # Calculate daily log returns
    log_returns = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
    mean_ret = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_ret) ** 2 for r in log_returns) / (len(log_returns) - 1)
    daily_vol = math.sqrt(variance)
    annualized = daily_vol * math.sqrt(365)
    return annualized


def _prob_price_target(current: float, target: float, vol: float,
                       days: float, direction: str) -> float:
    """
    Estimate probability of hitting a price target using simplified
    Black-Scholes / geometric Brownian motion.

    For "above": P(S_T >= target) using lognormal assumption
    For "below": P(S_T <= target) using lognormal assumption
    """
    if current <= 0 or target <= 0 or vol <= 0 or days <= 0:
        return 0.5

    T = days / 365.0
    sigma = vol

    # d2 from Black-Scholes (assuming drift = 0 for risk-neutral)
    d2 = (math.log(current / target) + (-0.5 * sigma**2) * T) / (sigma * math.sqrt(T))

    # CDF of standard normal
    prob_above = _norm_cdf(d2)

    if direction == "above":
        return prob_above
    else:  # below
        return 1.0 - prob_above


def _norm_cdf(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz and Stegun)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _fetch_crypto_signal(market: dict) -> dict | None:
    """Estimate probability for crypto price target markets."""
    question = market.get("question", "")
    parsed = _parse_crypto_target(question)
    if not parsed:
        return None

    symbol, crypto_name, target, direction = parsed

    current_price = _get_crypto_price(symbol)
    if not current_price:
        return None

    vol = _get_crypto_volatility(symbol)
    if not vol:
        vol = 0.60  # Default assumption: 60% annualized vol for crypto

    # Days to market expiry
    end_date_str = market.get("endDate", "")
    days = 30.0  # default
    if end_date_str:
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            days = max(0.5, (end_date - datetime.now(timezone.utc)).total_seconds() / 86400)
        except (ValueError, TypeError):
            pass

    prob = _prob_price_target(current_price, target, vol, days, direction)
    prob = max(0.01, min(0.99, prob))

    pct_away = abs(target - current_price) / current_price * 100

    return {
        "source": "crypto_model",
        "external_prob": round(prob, 4),
        "confidence": round(min(0.75, 0.5 + 0.25 * (1.0 - pct_away / 50.0)), 2),
        "details": (
            f"{crypto_name} {direction} ${target:,.0f}: current ${current_price:,.0f}, "
            f"vol={vol*100:.0f}%, {days:.0f}d, P={prob*100:.1f}%"
        ),
        "match_score": 1.0,  # Direct parse, no fuzzy match
    }


# ---------------------------------------------------------------------------
# 4. Deribit Options (more sophisticated crypto probability)
# ---------------------------------------------------------------------------

def _fetch_deribit_signal(market: dict) -> dict | None:
    """
    Use Deribit options data for BTC/ETH price target probability.
    Options implied probabilities are more market-informed than our vol model.
    """
    question = market.get("question", "")
    parsed = _parse_crypto_target(question)
    if not parsed:
        return None

    symbol, crypto_name, target, direction = parsed

    if crypto_name not in ("BTC", "ETH"):
        return None  # Deribit only has BTC and ETH

    # Get option book summary
    data = _safe_get(
        "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
        params={
            "currency": crypto_name,
            "kind": "option",
        },
        timeout=15,
    )

    if not data or not isinstance(data, dict):
        return None

    result = data.get("result", [])
    if not result:
        return None

    # Find call/put options near the target strike
    # Instrument names look like: BTC-28MAR25-90000-C
    end_date_str = market.get("endDate", "")
    market_end = None
    if end_date_str:
        try:
            market_end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    best_option = None
    best_strike_diff = float("inf")

    for opt in result:
        name = opt.get("instrument_name", "")
        # Parse instrument: BTC-28MAR25-90000-C
        parts = name.split("-")
        if len(parts) < 4:
            continue

        try:
            strike = float(parts[2])
        except ValueError:
            continue

        opt_type = parts[3]  # C or P

        # We want calls for "above" targets, puts for "below"
        if direction == "above" and opt_type != "C":
            continue
        if direction == "below" and opt_type != "P":
            continue

        strike_diff = abs(strike - target)
        if strike_diff < best_strike_diff:
            best_strike_diff = strike_diff
            best_option = opt

    if not best_option:
        return None

    # Use mark_price (in BTC/ETH terms) and underlying_price to get implied prob
    mark_price = best_option.get("mark_price", 0)
    underlying = best_option.get("underlying_price", 0)

    if not underlying or not mark_price:
        return None

    # For deep OTM options, mark_price approximates the probability
    # (in risk-neutral terms). This is a simplification but useful.
    # A call option price / intrinsic value gives rough probability
    strike_pct_diff = best_strike_diff / target
    if strike_pct_diff > 0.20:
        return None  # Strike too far from target, not useful

    # Option price in USD terms / max payoff gives rough probability estimate
    option_usd = mark_price * underlying
    prob = mark_price  # For deep OTM, this approximates risk-neutral prob

    if direction == "above":
        prob = min(0.95, max(0.02, prob))
    else:
        prob = min(0.95, max(0.02, prob))

    return {
        "source": "deribit",
        "external_prob": round(prob, 4),
        "confidence": round(max(0.3, 0.7 - strike_pct_diff), 2),
        "details": f"Deribit {best_option.get('instrument_name', '?')}: mark={mark_price:.4f}, underlying=${underlying:,.0f}",
        "match_score": round(1.0 - strike_pct_diff, 2),
    }


# ---------------------------------------------------------------------------
# 5. Commodities (Oil price targets)
# ---------------------------------------------------------------------------

_COMMODITY_PATTERNS = [
    re.compile(
        r"(?:will|does)\s+(?:crude\s+)?oil"
        r".*?(?:hit|reach|exceed|above|over|break|cross|surpass)\s*"
        r"\$?([\d,]+\.?\d*)",
        re.IGNORECASE
    ),
    re.compile(
        r"(?:will|does)\s+(?:crude\s+)?oil"
        r".*?(?:dip|drop|fall|below|under)\s*(?:to\s*)?"
        r"\$?([\d,]+\.?\d*)",
        re.IGNORECASE
    ),
    re.compile(
        r"(?:crude|oil|wti|brent).*?\$\s*([\d,]+\.?\d*)",
        re.IGNORECASE
    ),
]


def _parse_oil_target(question: str) -> tuple[float, str] | None:
    """Parse oil price target from question. Returns (target, direction)."""
    q = question.lower()
    if not any(w in q for w in ("oil", "crude", "wti", "brent")):
        return None

    below_words = {"dip", "drop", "fall", "below", "under"}
    direction = "below" if any(w in q for w in below_words) else "above"

    for pattern in _COMMODITY_PATTERNS:
        match = pattern.search(question)
        if match:
            try:
                target = float(match.group(1).replace(",", ""))
                if 10 < target < 300:  # Sanity check for oil prices
                    return target, direction
            except ValueError:
                continue
    return None


def _get_oil_price() -> float | None:
    """Get current crude oil price. Try multiple free sources."""
    # Try 1: API Ninjas
    api_ninjas_key = os.environ.get("API_NINJAS_KEY", "")
    if api_ninjas_key:
        data = _safe_get(
            "https://api.api-ninjas.com/v1/commodityprice",
            params={"name": "crude_oil"},
            headers={"X-Api-Key": api_ninjas_key},
            timeout=10,
        )
        if data and isinstance(data, dict) and "price" in data:
            try:
                return float(data["price"])
            except (ValueError, TypeError):
                pass

    # Try 2: Use a free alternative - Yahoo Finance via simple endpoint
    # Try exchangerate or similar free commodity API
    data = _safe_get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": "BRENTUSDT"},
        timeout=10,
    )
    if data and "price" in data:
        try:
            return float(data["price"])
        except (ValueError, TypeError):
            pass

    return None


def _fetch_oil_signal(market: dict) -> dict | None:
    """Estimate probability for oil price target markets."""
    question = market.get("question", "")
    parsed = _parse_oil_target(question)
    if not parsed:
        return None

    target, direction = parsed
    current_price = _get_oil_price()
    if not current_price:
        return None

    # Use rough historical volatility for oil (~30% annualized)
    vol = 0.30

    end_date_str = market.get("endDate", "")
    days = 30.0
    if end_date_str:
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            days = max(0.5, (end_date - datetime.now(timezone.utc)).total_seconds() / 86400)
        except (ValueError, TypeError):
            pass

    prob = _prob_price_target(current_price, target, vol, days, direction)
    prob = max(0.01, min(0.99, prob))

    pct_away = abs(target - current_price) / current_price * 100

    return {
        "source": "oil_futures",
        "external_prob": round(prob, 4),
        "confidence": round(min(0.65, 0.4 + 0.25 * (1.0 - pct_away / 30.0)), 2),
        "details": f"Oil {direction} ${target:.0f}: current ${current_price:.2f}, vol=30%, {days:.0f}d, P={prob*100:.1f}%",
        "match_score": 1.0,
    }


# ---------------------------------------------------------------------------
# 6. Fed/Macro (via prediction market aggregation)
# ---------------------------------------------------------------------------

_FED_KEYWORDS = [
    "federal reserve", "fed rate", "interest rate", "fomc",
    "rate cut", "rate hike", "basis points", "fed funds",
]


def _is_fed_market(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in _FED_KEYWORDS)


def _fetch_fed_signal(market: dict) -> dict | None:
    """For Fed/macro markets, aggregate from Metaculus + Manifold."""
    question = market.get("question", "")
    if not _is_fed_market(question):
        return None

    signals = []

    meta = _fetch_metaculus_signal(market)
    if meta:
        signals.append(meta)

    mani = _fetch_manifold_signal(market)
    if mani:
        signals.append(mani)

    if not signals:
        return None

    # Weighted average by confidence
    total_weight = sum(s["confidence"] for s in signals)
    if total_weight <= 0:
        return None

    avg_prob = sum(s["external_prob"] * s["confidence"] for s in signals) / total_weight
    avg_conf = sum(s["confidence"] for s in signals) / len(signals)

    details_parts = [s["details"] for s in signals]

    return {
        "source": "fedwatch",
        "external_prob": round(avg_prob, 4),
        "confidence": round(avg_conf, 2),
        "details": " | ".join(details_parts),
        "match_score": max(s["match_score"] for s in signals),
    }


# ---------------------------------------------------------------------------
# Main aggregator
# ---------------------------------------------------------------------------

def _is_sports_market(question: str) -> bool:
    """Quick check if a market is sports-related."""
    q = question.lower()
    sport_hints = [
        "nba", "nhl", "nfl", "mlb", "ufc", "mma", "tennis", "boxing",
        "premier league", "champions league", "la liga", "serie a",
        "bundesliga", "ligue 1", " vs ", " vs. ", "game ", "match",
        "fight", "win",
    ]
    return any(h in q for h in sport_hints)


def _is_crypto_market(question: str) -> bool:
    q = question.lower()
    return any(c in q for c in ("bitcoin", "btc", "ethereum", "eth", "solana", "sol"))


def _is_oil_market(question: str) -> bool:
    q = question.lower()
    return any(w in q for w in ("oil", "crude", "wti", "brent"))


def _get_polymarket_yes_price(market: dict) -> float | None:
    """Extract current YES price from market dict."""
    # Gamma API format: outcomePrices is a JSON string like "[\"0.53\",\"0.47\"]"
    prices_str = market.get("outcomePrices", "")
    if prices_str:
        try:
            if isinstance(prices_str, str):
                import json
                prices = json.loads(prices_str)
            else:
                prices = prices_str
            if prices and len(prices) >= 1:
                return float(prices[0])
        except (ValueError, TypeError):
            pass

    # Alternative: bestAsk or clobTokenIds based pricing
    # Check tokens array
    tokens = market.get("tokens", [])
    for token in tokens:
        if token.get("outcome", "").upper() == "YES":
            price = token.get("price")
            if price:
                return float(price)

    return None


def get_external_signal(market: dict) -> dict | None:
    """
    Given a Polymarket market dict (from Gamma API), find external signals
    and return a consensus probability estimate.

    Returns: {
        "source": "sportsbook" | "metaculus" | "manifold" | "deribit" | "fedwatch" | "oil_futures" | "crypto_model",
        "external_prob": 0.61,      # External consensus probability for YES
        "polymarket_prob": 0.53,    # Current Polymarket YES price
        "divergence": 0.08,         # external - polymarket
        "confidence": 0.8,          # How confident we are in the signal (0-1)
        "details": "DraftKings: 61%, Bet365: 59%, FanDuel: 62%"
    }
    """
    question = market.get("question", "")
    if not question:
        return None

    polymarket_prob = _get_polymarket_yes_price(market)

    # Try sources in priority order based on market type
    signals = []

    try:
        if _is_sports_market(question):
            sig = _fetch_sportsbook_signal(market)
            if sig:
                signals.append(sig)
    except Exception as e:
        log.debug(f"Sportsbook signal failed: {e}")

    try:
        if _is_crypto_market(question):
            # Try Deribit first (more market-informed), then our vol model
            sig = _fetch_deribit_signal(market)
            if sig:
                signals.append(sig)
            sig = _fetch_crypto_signal(market)
            if sig:
                signals.append(sig)
    except Exception as e:
        log.debug(f"Crypto signal failed: {e}")

    try:
        if _is_oil_market(question):
            sig = _fetch_oil_signal(market)
            if sig:
                signals.append(sig)
    except Exception as e:
        log.debug(f"Oil signal failed: {e}")

    try:
        if _is_fed_market(question):
            sig = _fetch_fed_signal(market)
            if sig:
                signals.append(sig)
    except Exception as e:
        log.debug(f"Fed signal failed: {e}")

    # Always try prediction market aggregation as a fallback
    try:
        meta = _fetch_metaculus_signal(market)
        if meta:
            signals.append(meta)
    except Exception as e:
        log.debug(f"Metaculus signal failed: {e}")

    try:
        mani = _fetch_manifold_signal(market)
        if mani:
            signals.append(mani)
    except Exception as e:
        log.debug(f"Manifold signal failed: {e}")

    if not signals:
        return None

    # Pick the best signal (highest confidence * match_score)
    # But if we have multiple, do a weighted average
    if len(signals) == 1:
        best = signals[0]
    else:
        # Weighted average of probabilities by confidence
        total_weight = sum(s["confidence"] * s.get("match_score", 0.5) for s in signals)
        if total_weight <= 0:
            return None

        avg_prob = sum(
            s["external_prob"] * s["confidence"] * s.get("match_score", 0.5)
            for s in signals
        ) / total_weight

        # Use highest confidence source as primary
        best = max(signals, key=lambda s: s["confidence"] * s.get("match_score", 0.5))
        best = dict(best)  # copy
        best["external_prob"] = round(avg_prob, 4)
        best["details"] = " | ".join(s["details"] for s in signals)

    # Add Polymarket comparison
    result = {
        "source": best["source"],
        "external_prob": best["external_prob"],
        "polymarket_prob": polymarket_prob,
        "divergence": round(best["external_prob"] - (polymarket_prob or 0.5), 4),
        "confidence": best["confidence"],
        "details": best["details"],
    }

    return result


# ---------------------------------------------------------------------------
# CLI demo / test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    example_markets = [
        {
            "question": "Will the Thunder win vs the Celtics?",
            "slug": "thunder-celtics-nba",
            "outcomePrices": '["0.55","0.45"]',
            "endDate": "2026-03-25T03:00:00Z",
        },
        {
            "question": "Will Bitcoin hit $120,000 by March 31?",
            "slug": "bitcoin-120k-march",
            "outcomePrices": '["0.30","0.70"]',
            "endDate": "2026-03-31T23:59:00Z",
        },
        {
            "question": "Will Bitcoin dip to $75,000 by April 2026?",
            "slug": "bitcoin-75k-dip",
            "outcomePrices": '["0.15","0.85"]',
            "endDate": "2026-04-30T23:59:00Z",
        },
        {
            "question": "Will the Fed cut rates by 50 basis points at the next FOMC meeting?",
            "slug": "fed-rate-cut-50bp",
            "outcomePrices": '["0.12","0.88"]',
            "endDate": "2026-05-01T23:59:00Z",
        },
        {
            "question": "Will crude oil hit $90 per barrel by June 2026?",
            "slug": "oil-90-june",
            "outcomePrices": '["0.20","0.80"]',
            "endDate": "2026-06-30T23:59:00Z",
        },
        {
            "question": "Will Trump be indicted in 2026?",
            "slug": "trump-indictment-2026",
            "outcomePrices": '["0.25","0.75"]',
            "endDate": "2026-12-31T23:59:00Z",
        },
    ]

    for m in example_markets:
        print(f"\n{'='*70}")
        print(f"Market: {m['question']}")
        print(f"Polymarket YES price: {_get_polymarket_yes_price(m)}")
        signal = get_external_signal(m)
        if signal:
            print(f"  Source:      {signal['source']}")
            print(f"  External P:  {signal['external_prob']:.2%}")
            print(f"  Poly P:      {signal['polymarket_prob']:.2%}" if signal['polymarket_prob'] else "  Poly P:      N/A")
            print(f"  Divergence:  {signal['divergence']:+.2%}")
            print(f"  Confidence:  {signal['confidence']:.2f}")
            print(f"  Details:     {signal['details']}")
        else:
            print("  No external signal found.")
