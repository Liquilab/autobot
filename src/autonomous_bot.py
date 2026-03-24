"""
Autonomous Polymarket Trading Bot v2
Runs 24/7, scans markets, executes trades, tracks positions, writes reports.
Goal: $80 -> $1,000 in 90 days.

v2 changes (quant review):
- Order fill verification (no more phantom positions)
- On-chain redeem of resolved positions
- External signals integration (sportsbook focus)
- Dynamic divergence thresholds per source
- Correlation limits & position exit logic
- Heartbeat + self-heal
- Env var validation
"""

import os
import sys
import json
import time
import math
import logging
import traceback
import subprocess
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs, PartialCreateOrderOptions, BalanceAllowanceParams
)
from py_clob_client.order_builder.constants import BUY, SELL

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
TRADES_FILE = DATA_DIR / "trades.json"
POSITIONS_FILE = DATA_DIR / "positions.json"
PNL_FILE = DATA_DIR / "pnl.json"
STATE_FILE = DATA_DIR / "bot_state.json"
HEARTBEAT_FILE = DATA_DIR / "heartbeat.json"
STRATEGY_FILE = DATA_DIR / "strategy_params.json"

CLOB_URL = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CHAIN_ID = 137
SIGNATURE_TYPE = 2  # Gnosis Safe proxy

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS")

# GitHub config
GITHUB_PAT = os.getenv("GITHUB_PAT", "")

# Timing (seconds)
SCAN_INTERVAL = 300          # 5 minutes
REPORT_INTERVAL = 28800      # 8 hours
GIT_PUSH_INTERVAL = 3600     # 1 hour
POSITION_CHECK_INTERVAL = 300  # 5 minutes
RESEARCH_INTERVAL = 7200     # 2 hours

# Strategy parameters
MAX_POSITION_FRACTION = 0.15   # max 15% of bankroll per trade
CASH_RESERVE_FRACTION = 0.05   # keep 5% cash reserve
MIN_DAILY_TRADES = 10
MIN_LIQUIDITY = 5000
MIN_VOLUME_24H = 1000
STALE_POSITION_DAYS = 30

# Correlation limits
MAX_THEME_FRACTION = 0.25  # max 25% portfolio in correlated positions

# Exit thresholds
STOP_LOSS_PCT = 0.30   # sell if position lost 30% value
TAKE_PROFIT_PCT = 0.15  # sell if position gained 15% value

# Divergence thresholds per signal source
DIVERGENCE_THRESHOLDS = {
    "sportsbook_high": 0.02,   # 4+ bookmakers
    "sportsbook_low": 0.04,    # 1-2 bookmakers
    "manifold": 0.08,
    "metaculus": 0.05,
    "crypto_model": 0.06,
    "deribit": 0.05,
    "oil_futures": 0.06,
    "fedwatch": 0.06,
}

# Theme keywords for correlation tracking
THEME_KEYWORDS = {
    "iran": ["iran", "iranian", "kharg", "tehran", "persian gulf"],
    "bitcoin": ["bitcoin", "btc"],
    "ethereum": ["ethereum", "eth"],
    "fed": ["federal reserve", "fed rate", "fomc", "interest rate", "rate cut"],
    "oil": ["crude", "oil", "wti", "brent"],
    "ukraine": ["ukraine", "ukrainian", "kyiv", "russia", "putin"],
    "trump": ["trump"],
    "china": ["china", "chinese", "taiwan", "beijing"],
}

# Sport keywords for market categorization
SPORT_KEYWORDS = [
    "nba", "nhl", "nfl", "mlb", "tennis", "ufc", "mma", "boxing",
    "premier league", "la liga", "serie a", "bundesliga", "ligue 1",
    "champions league", "esports", "cs2", "cs:go", "dota", "league of legends",
    "valorant", " vs ", " vs. ", "game ", "match", "fight",
    "thunder", "celtics", "lakers", "warriors", "bucks", "nuggets",
    "rockets", "grizzlies", "cavaliers", "knicks", "76ers", "pacers",
    "hawks", "bulls", "clippers", "pistons", "magic", "raptors",
    "jazz", "heat", "nets", "suns", "kings", "spurs", "blazers",
    "timberwolves", "pelicans", "hornets", "wizards", "mavericks",
    "oilers", "panthers", "rangers", "bruins", "avalanche", "stars",
    "maple leafs", "canadiens", "flames", "jets", "penguins",
    "mongol", "spirit", "navi", "faze", "g2", "vitality",
]

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "bot.log"),
    ],
)
log = logging.getLogger("autobot")

# ---------------------------------------------------------------------------
# Env var validation (Fase 6A)
# ---------------------------------------------------------------------------

def validate_env():
    """Exit if critical env vars are missing."""
    missing = []
    for var in ("PRIVATE_KEY", "WALLET_ADDRESS", "FUNDER_ADDRESS"):
        if not os.getenv(var):
            missing.append(var)
    if missing:
        log.critical(f"Missing required env vars: {', '.join(missing)}. Exiting.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# JSON persistence helpers
# ---------------------------------------------------------------------------

def ensure_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    REPORTS_DIR.mkdir(exist_ok=True)


def load_json(path: Path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_json(path: Path, data):
    ensure_dirs()
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "start_time": datetime.now(timezone.utc).isoformat(),
            "last_scan": None,
            "last_report": None,
            "last_git_push": None,
            "last_position_check": None,
            "last_research": None,
            "total_trades": 0,
            "trades_today": 0,
            "trades_today_date": None,
            "initial_bankroll": 80.0,
            "consecutive_errors": 0,
        }


def save_state(state: dict):
    save_json(STATE_FILE, state)


# ---------------------------------------------------------------------------
# Heartbeat (Fase 6B)
# ---------------------------------------------------------------------------

def write_heartbeat(state: dict, balance: float):
    """Write heartbeat file every cycle for external monitoring."""
    heartbeat = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "balance": balance,
        "consecutive_errors": state.get("consecutive_errors", 0),
        "total_trades": state.get("total_trades", 0),
        "trades_today": state.get("trades_today", 0),
        "pid": os.getpid(),
    }
    save_json(HEARTBEAT_FILE, heartbeat)


# ---------------------------------------------------------------------------
# CLOB Client
# ---------------------------------------------------------------------------

_client = None
_client_created_at = 0
CLIENT_TTL = 1800  # recreate client every 30 min


def get_client() -> ClobClient:
    global _client, _client_created_at
    now = time.time()
    if _client is not None and (now - _client_created_at) < CLIENT_TTL:
        return _client

    log.info("Creating new CLOB client...")
    client = ClobClient(
        CLOB_URL,
        key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        signature_type=SIGNATURE_TYPE,
        funder=FUNDER_ADDRESS,
    )
    try:
        creds = client.derive_api_key()
    except Exception:
        creds = client.create_api_key()
    client.set_api_creds(creds)
    _client = client
    _client_created_at = now
    log.info("CLOB client ready.")
    return client


def reset_client():
    """Force client recreation (self-heal)."""
    global _client, _client_created_at
    _client = None
    _client_created_at = 0
    log.info("CLOB client reset.")


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------

def get_balance() -> float:
    """Get USDC.e balance available for trading (in dollars)."""
    try:
        client = get_client()
        params = BalanceAllowanceParams(
            asset_type="COLLATERAL", signature_type=SIGNATURE_TYPE
        )
        result = client.get_balance_allowance(params)
        return int(result.get("balance", 0)) / 1e6
    except Exception as e:
        log.error(f"Balance check failed: {e}")
        return 0.0


# ---------------------------------------------------------------------------
# Market fetching
# ---------------------------------------------------------------------------

def api_get(url: str, params: dict = None, retries: int = 3) -> dict | list | None:
    """GET with retry and backoff."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                wait = 2 ** (attempt + 1)
                log.warning(f"Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                log.error(f"API GET {url} failed after {retries} attempts: {e}")
                return None
    return None


def fetch_markets(limit: int = 200) -> list:
    """Fetch active markets sorted by 24h volume."""
    data = api_get(f"{GAMMA_API}/markets", {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "order": "volume24hr",
        "ascending": "false",
    })
    return data if isinstance(data, list) else []


def fetch_events(limit: int = 100) -> list:
    """Fetch events (grouped markets) from Gamma API."""
    data = api_get(f"{GAMMA_API}/events", {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "order": "volume24hr",
        "ascending": "false",
    })
    return data if isinstance(data, list) else []


def fetch_market_by_slug(slug: str) -> dict | None:
    data = api_get(f"{GAMMA_API}/markets", {"slug": slug})
    if isinstance(data, list) and data:
        return data[0]
    return None


# ---------------------------------------------------------------------------
# Market analysis & categorization
# ---------------------------------------------------------------------------

def parse_prices_and_tokens(market: dict):
    """Extract prices and token IDs from a market dict. Returns None on failure."""
    prices_raw = market.get("outcomePrices", "")
    tokens_raw = market.get("clobTokenIds", "")
    if not prices_raw or not tokens_raw:
        return None

    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
    except (json.JSONDecodeError, ValueError):
        return None

    if len(prices) < 2 or len(tokens) < 2:
        return None

    return {
        "yes_price": float(prices[0]),
        "no_price": float(prices[1]),
        "yes_token": tokens[0],
        "no_token": tokens[1],
    }


def categorize_market(market: dict) -> str:
    """Return 'sports', 'short_term', 'medium_term', or 'long_term'."""
    question = market.get("question", "").lower()
    slug = market.get("slug", "").lower()
    text = question + " " + slug

    for kw in SPORT_KEYWORDS:
        if kw in text:
            return "sports"

    end_date_str = market.get("endDate", "")
    if end_date_str:
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_to_exp = (end_date - now).total_seconds() / 86400
            if days_to_exp <= 7:
                return "short_term"
            elif days_to_exp <= 30:
                return "medium_term"
            else:
                return "long_term"
        except (ValueError, TypeError):
            pass

    return "medium_term"


def days_to_expiry(market: dict) -> float:
    end_date_str = market.get("endDate", "")
    if not end_date_str:
        return 999.0
    try:
        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return max(0, (end_date - datetime.now(timezone.utc)).total_seconds() / 86400)
    except (ValueError, TypeError):
        return 999.0


def hours_to_expiry(market: dict) -> float:
    return days_to_expiry(market) * 24.0


def detect_theme(text: str) -> str:
    """Detect correlation theme from market text. Returns theme or 'other'."""
    text_lower = text.lower()
    for theme, keywords in THEME_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return theme
    return "other"


# ---------------------------------------------------------------------------
# Kelly criterion & position sizing
# ---------------------------------------------------------------------------

def half_kelly(prob_win: float, price: float) -> float:
    """Half-Kelly fraction. prob_win is our estimated true probability."""
    if price <= 0 or price >= 1 or prob_win <= 0 or prob_win >= 1:
        return 0.0
    odds = 1.0 / price
    b = odds - 1.0
    q = 1.0 - prob_win
    f = (b * prob_win - q) / b
    return max(0.0, f / 2.0)


def position_size(bankroll: float, prob_win: float, price: float) -> float:
    """Dollar amount to bet, capped by max fraction and cash reserve."""
    available = bankroll * (1.0 - CASH_RESERVE_FRACTION)
    kelly = half_kelly(prob_win, price)
    fraction = min(kelly, MAX_POSITION_FRACTION)
    return round(available * fraction, 2)


# ---------------------------------------------------------------------------
# Fase 0: Position reconciliation
# ---------------------------------------------------------------------------

def reconcile_positions():
    """
    Reconcile positions.json against actual order fill status.
    Rebuild positions from trades that were actually matched (have transactionsHashes).
    Remove phantom positions from unfilled orders.
    """
    positions = load_json(POSITIONS_FILE)
    trades = load_json(TRADES_FILE)

    if not trades:
        return

    # Cancel all open orders via CLOB (clean slate)
    try:
        client = get_client()
        open_orders = client.get_orders() or []
        for order in open_orders:
            order_id = order.get("id", "") or order.get("orderID", "")
            if order_id:
                try:
                    client.cancel(order_id)
                    log.info(f"Cancelled open order {order_id[:16]}...")
                except Exception:
                    pass
    except Exception as e:
        log.error(f"Failed to fetch/cancel open orders: {e}")

    # Rebuild positions from MATCHED trades only
    matched_positions = {}  # token_id -> position

    for t in trades:
        resp = t.get("response", {})
        status = resp.get("status", "")

        if status != "matched" or not resp.get("transactionsHashes"):
            market = t.get("market", "?")[:40
            ]
            log.warning(f"PHANTOM TRADE: '{market}' status={status} — not a real fill")
            continue

        token_id = t.get("token_id", "")
        if not token_id:
            continue

        if token_id in matched_positions:
            # Average into existing position
            pos = matched_positions[token_id]
            old_shares = pos["shares"]
            old_cost = pos["cost"]
            new_shares = old_shares + t.get("shares", t.get("size", 0))
            new_cost = old_cost + t.get("cost", 0)
            pos["shares"] = new_shares
            pos["cost"] = round(new_cost, 2)
            pos["avg_price"] = round(new_cost / new_shares, 4) if new_shares > 0 else 0
            pos["max_payout"] = new_shares
        else:
            # Determine theme from market question
            market_text = t.get("market", "")
            theme = detect_theme(market_text)
            shares = t.get("shares", t.get("size", 0))

            matched_positions[token_id] = {
                "market": market_text,
                "slug": t.get("slug", ""),
                "condition_id": t.get("condition_id", ""),
                "token_id": token_id,
                "side": t.get("side", "BUY"),
                "shares": shares,
                "avg_price": t.get("price", 0),
                "cost": round(t.get("cost", 0), 2),
                "max_payout": shares,
                "entry_date": t.get("timestamp", "")[:10],
                "end_date": t.get("end_date", ""),
                "status": "open",
                "category": t.get("category", "unknown"),
                "neg_risk": t.get("neg_risk", False),
                "order_id": resp.get("orderID", ""),
                "signal_source": t.get("signal_source", "heuristic"),
                "theme": theme,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }

    # Preserve resolved/sold positions from old list
    resolved_positions = [p for p in positions if p.get("status") in ("resolved", "sold")]

    # Combine
    new_positions = resolved_positions + list(matched_positions.values())

    # Calculate stats
    old_open = [p for p in positions if p.get("status") == "open"]
    old_cost = sum(p.get("cost", 0) for p in old_open)
    new_cost = sum(p.get("cost", 0) for p in matched_positions.values())
    phantoms = len(old_open) - len(matched_positions)

    save_json(POSITIONS_FILE, new_positions)

    log.info(
        f"RECONCILIATION COMPLETE: "
        f"{len(matched_positions)} confirmed positions (${new_cost:.2f}), "
        f"{max(0, phantoms)} phantom positions removed "
        f"(phantom cost: ${max(0, old_cost - new_cost):.2f})"
    )


# ---------------------------------------------------------------------------
# Fase 1: Order fill verification
# ---------------------------------------------------------------------------

def place_order_with_verification(
    token_id: str, price: float, size: int, side: str = BUY,
    tick_size: str = "0.01", neg_risk: bool = False
) -> dict | None:
    """
    Place a limit order and verify it was filled.
    Returns response dict with verified fill status, or None on failure.
    Only returns success if order was actually matched.
    """
    if size < 5:  # Polymarket minimum
        return None

    try:
        client = get_client()
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        resp = client.create_and_post_order(order_args, options)

        status = resp.get("status", "unknown")
        order_id = resp.get("orderID", "")

        if status == "matched" and resp.get("transactionsHashes"):
            log.info(f"ORDER FILLED: {side} {size} @ ${price} (tx: {resp['transactionsHashes'][0][:16]}...)")
            return resp

        if status in ("live", "delayed"):
            # Order not immediately filled — wait and poll
            log.info(f"Order {status}, waiting for fill (max 30s)...")
            filled = _poll_order_fill(order_id, timeout=30)
            if filled:
                log.info(f"ORDER FILLED (delayed): {side} {size} @ ${price}")
                resp["status"] = "matched"
                resp["fill_type"] = "delayed"
                return resp
            else:
                # Cancel unfilled order
                log.warning(f"Order not filled after 30s, cancelling {order_id[:16]}...")
                try:
                    client.cancel(order_id)
                except Exception:
                    pass
                return None

        log.warning(f"Order rejected or failed: status={status}, resp={resp}")
        return None

    except Exception as e:
        log.error(f"Order failed: {e}")
        return None


def _poll_order_fill(order_id: str, timeout: int = 30) -> bool:
    """Poll order status until filled or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            client = get_client()
            order = client.get_order(order_id)
            if order:
                status = order.get("status", "")
                if status == "matched":
                    return True
                if status in ("cancelled", "expired"):
                    return False
            # Also check if order has associated fills
            size_matched = float(order.get("size_matched", 0) if order else 0)
            if size_matched > 0:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


# ---------------------------------------------------------------------------
# Fase 2: Redeem resolved positions
# ---------------------------------------------------------------------------

def redeem_position(condition_id: str) -> bool:
    """
    Redeem a resolved position via the CTF contract.
    This converts winning conditional tokens back to USDC.
    """
    if not condition_id:
        return False

    try:
        # Use the CLOB/CTF redeem endpoint if available via REST
        # Polymarket provides a redeem API through the neg-risk adapter
        # For now, try the strapi/data API approach
        url = f"{CLOB_URL}/redeem"
        headers = {}

        # Try using py_clob_client if it has redeem support
        client = get_client()

        # Check if client has a redeem method (varies by library version)
        if hasattr(client, "redeem"):
            result = client.redeem(condition_id)
            log.info(f"Redeemed position {condition_id[:16]}...: {result}")
            return True

        # Fallback: use the Polymarket Neg Risk Adapter via direct HTTP
        # The proxy wallet needs to call redeemPositions on the contract
        # This requires web3 — add if needed
        log.warning(
            f"Redeem not available via CLOB client for {condition_id[:16]}... "
            f"Manual redemption may be needed."
        )
        return False

    except Exception as e:
        log.error(f"Redeem failed for {condition_id[:16]}...: {e}")
        return False


# ---------------------------------------------------------------------------
# Fase 3: External signals integration
# ---------------------------------------------------------------------------

# Import signals module
try:
    from signals import get_external_signal
    SIGNALS_AVAILABLE = True
    log.info("External signals module loaded.")
except ImportError:
    SIGNALS_AVAILABLE = False
    log.warning("signals.py not found — running without external signals.")

    def get_external_signal(market):
        return None


def get_divergence_threshold(signal: dict) -> float:
    """Get the appropriate divergence threshold for a signal source."""
    source = signal.get("source", "")
    confidence = signal.get("confidence", 0)

    if source == "sportsbook":
        # High confidence = 4+ bookmakers
        if confidence >= 0.7:
            return DIVERGENCE_THRESHOLDS["sportsbook_high"]
        else:
            return DIVERGENCE_THRESHOLDS["sportsbook_low"]

    return DIVERGENCE_THRESHOLDS.get(source, 0.06)


def evaluate_with_signal(market: dict, pt: dict) -> dict | None:
    """
    Evaluate a market using external signals.
    Returns a trade signal dict or None.
    """
    if not SIGNALS_AVAILABLE:
        return None

    ext_signal = get_external_signal(market)
    if not ext_signal:
        return None

    source = ext_signal.get("source", "unknown")
    ext_prob = ext_signal.get("external_prob")
    poly_prob = ext_signal.get("polymarket_prob")
    divergence = ext_signal.get("divergence", 0)
    confidence = ext_signal.get("confidence", 0)

    if ext_prob is None or poly_prob is None:
        return None

    # Get threshold for this source
    threshold = get_divergence_threshold(ext_signal)
    abs_div = abs(divergence)

    if abs_div < threshold:
        return None  # Not enough divergence

    # Minimum confidence
    if confidence < 0.3:
        return None

    # Determine trade direction
    # If external says YES is more likely than Polymarket thinks → buy YES
    # If external says YES is less likely → buy NO
    if divergence > 0:
        # External thinks YES is underpriced on Polymarket
        side = "YES"
        token_id = pt["yes_token"]
        price = pt["yes_price"]
        est_prob = ext_prob
    else:
        # External thinks NO is underpriced (YES is overpriced)
        side = "NO"
        token_id = pt["no_token"]
        price = pt["no_price"]
        est_prob = 1.0 - ext_prob

    # Sanity: est_prob must be > price for positive Kelly
    if est_prob <= price:
        return None

    edge = est_prob - price
    kelly_size = half_kelly(est_prob, price)
    if kelly_size < 0.005:
        return None

    # Priority based on source reliability and speed
    priority_map = {
        "sportsbook": 1,
        "crypto_model": 2,
        "deribit": 2,
        "oil_futures": 3,
        "metaculus": 4,
        "manifold": 4,
        "fedwatch": 3,
    }

    return {
        "side": side,
        "token_id": token_id,
        "price": price,
        "est_prob": est_prob,
        "edge": edge,
        "priority": priority_map.get(source, 5),
        "signal_source": source,
        "signal_details": ext_signal.get("details", ""),
        "divergence": divergence,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Trade execution (updated with fill verification — Fase 1)
# ---------------------------------------------------------------------------

def execute_trade(market: dict, signal: dict, bankroll: float) -> dict | None:
    """Size and execute a trade. Only registers position if order is filled."""
    price = signal["price"]
    est_prob = signal["est_prob"]
    token_id = signal["token_id"]
    side_label = signal["side"]
    neg_risk = market.get("negRisk", False)
    question = market.get("question", "")

    tick_size = "0.001" if neg_risk else "0.01"
    if tick_size == "0.001":
        price = round(price, 3)
    else:
        price = round(price, 2)

    dollar_size = position_size(bankroll, est_prob, price)
    if dollar_size < 1.0:
        return None

    shares = max(5, int(dollar_size / price))  # Min 5 shares (Polymarket minimum)
    cost = round(shares * price, 2)

    # Safety caps
    if cost > bankroll * MAX_POSITION_FRACTION:
        shares = max(5, int((bankroll * MAX_POSITION_FRACTION) / price))
        cost = round(shares * price, 2)

    if cost > bankroll * (1.0 - CASH_RESERVE_FRACTION):
        shares = max(5, int((bankroll * (1.0 - CASH_RESERVE_FRACTION)) / price))
        cost = round(shares * price, 2)

    if shares < 5 or cost < 0.50:
        return None

    # Check correlation limits before trading
    theme = detect_theme(question)
    if not check_correlation_limit(theme, cost, bankroll):
        log.info(f"SKIP (correlation): '{question[:40]}' theme={theme}, would exceed {MAX_THEME_FRACTION*100:.0f}% limit")
        return None

    log.info(
        f"Executing: {side_label} {shares} shares of '{question[:50]}' @ ${price} "
        f"(cost: ${cost}, signal: {signal.get('signal_source', 'heuristic')}, "
        f"div: {signal.get('divergence', 0):+.2%})"
    )

    # Use verified order placement (Fase 1)
    resp = place_order_with_verification(token_id, price, shares, BUY, tick_size, neg_risk)
    if resp is None:
        log.warning(f"Trade NOT filled for '{question[:40]}' — no position registered.")
        return None

    trade_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market": question,
        "slug": market.get("slug", ""),
        "condition_id": market.get("conditionId", ""),
        "token_id": token_id,
        "side": side_label,
        "price": price,
        "shares": shares,
        "cost": cost,
        "est_prob": est_prob,
        "edge": signal["edge"],
        "category": categorize_market(market),
        "neg_risk": neg_risk,
        "end_date": market.get("endDate", ""),
        "order_id": resp.get("orderID", ""),
        "status": "matched",
        "signal_source": signal.get("signal_source", "heuristic"),
        "signal_details": signal.get("signal_details", ""),
        "divergence": signal.get("divergence", 0),
        "theme": theme,
        "response": resp,
    }

    # Save trade
    trades = load_json(TRADES_FILE)
    trades.append(trade_record)
    save_json(TRADES_FILE, trades)

    # Update positions
    update_position_from_trade(trade_record)

    return trade_record


def update_position_from_trade(trade: dict):
    """Add or update a position record after a CONFIRMED trade."""
    positions = load_json(POSITIONS_FILE)

    existing = None
    for p in positions:
        if p.get("token_id") == trade["token_id"]:
            existing = p
            break

    if existing:
        old_shares = existing.get("shares", 0)
        old_cost = existing.get("cost", 0)
        new_shares = old_shares + trade["shares"]
        new_cost = old_cost + trade["cost"]
        existing["shares"] = new_shares
        existing["cost"] = new_cost
        existing["avg_price"] = round(new_cost / new_shares, 4) if new_shares > 0 else 0
        existing["max_payout"] = new_shares
        existing["last_updated"] = datetime.now(timezone.utc).isoformat()
    else:
        positions.append({
            "market": trade["market"],
            "slug": trade.get("slug", ""),
            "condition_id": trade.get("condition_id", ""),
            "token_id": trade["token_id"],
            "side": trade["side"],
            "shares": trade["shares"],
            "avg_price": trade["price"],
            "cost": trade["cost"],
            "max_payout": trade["shares"],
            "entry_date": trade["timestamp"][:10],
            "end_date": trade.get("end_date", ""),
            "status": "open",
            "category": trade.get("category", "unknown"),
            "neg_risk": trade.get("neg_risk", False),
            "order_id": trade.get("order_id", ""),
            "signal_source": trade.get("signal_source", "heuristic"),
            "theme": trade.get("theme", "other"),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        })

    save_json(POSITIONS_FILE, positions)


# ---------------------------------------------------------------------------
# Fase 4A: Correlation limit check
# ---------------------------------------------------------------------------

def check_correlation_limit(theme: str, new_cost: float, bankroll: float) -> bool:
    """Check if adding this trade would exceed theme correlation limit."""
    if theme == "other":
        return True  # No correlation concern for uncategorized

    positions = load_json(POSITIONS_FILE)
    theme_cost = sum(
        p.get("cost", 0) for p in positions
        if p.get("status") == "open" and p.get("theme") == theme
    )

    total_portfolio = bankroll + sum(
        p.get("cost", 0) for p in positions if p.get("status") == "open"
    )

    if total_portfolio <= 0:
        return True

    new_theme_fraction = (theme_cost + new_cost) / total_portfolio
    return new_theme_fraction <= MAX_THEME_FRACTION


# ---------------------------------------------------------------------------
# Fase 4B & 4C: Position exit logic & sell orders
# ---------------------------------------------------------------------------

def check_position_exits():
    """
    Check open positions for exit conditions:
    - Stop loss: position lost >30% of value → sell
    - Take profit: position gained >15% → sell
    """
    positions = load_json(POSITIONS_FILE)
    exits_done = 0

    for pos in positions:
        if pos.get("status") != "open":
            continue

        slug = pos.get("slug", "")
        if not slug:
            continue

        try:
            market = fetch_market_by_slug(slug)
            if not market:
                continue

            pt = parse_prices_and_tokens(market)
            if not pt:
                continue

            # Get current price of our position
            side = pos.get("side", "").upper()
            if side == "YES":
                current_price = pt["yes_price"]
            else:
                current_price = pt["no_price"]

            avg_price = pos.get("avg_price", 0)
            if avg_price <= 0:
                continue

            shares = pos.get("shares", 0)
            cost = pos.get("cost", 0)
            current_value = shares * current_price

            # Calculate P&L percentage
            pnl_pct = (current_value - cost) / cost if cost > 0 else 0

            # Stop loss
            if pnl_pct < -STOP_LOSS_PCT:
                log.info(
                    f"STOP LOSS: '{pos['market'][:40]}' "
                    f"P&L: {pnl_pct*100:.1f}% (threshold: -{STOP_LOSS_PCT*100:.0f}%)"
                )
                success = sell_position(pos, market, pt, current_price)
                if success:
                    exits_done += 1

            # Take profit
            elif pnl_pct > TAKE_PROFIT_PCT:
                log.info(
                    f"TAKE PROFIT: '{pos['market'][:40]}' "
                    f"P&L: +{pnl_pct*100:.1f}% (threshold: +{TAKE_PROFIT_PCT*100:.0f}%)"
                )
                success = sell_position(pos, market, pt, current_price)
                if success:
                    exits_done += 1

            time.sleep(0.5)
        except Exception as e:
            log.debug(f"Error checking exit for '{pos.get('market', '')[:30]}': {e}")

    if exits_done > 0:
        log.info(f"Exited {exits_done} positions (stop loss / take profit)")

    return exits_done


def sell_position(pos: dict, market: dict, pt: dict, current_price: float) -> bool:
    """Sell a position by placing a SELL limit order near market price."""
    side = pos.get("side", "").upper()
    shares = pos.get("shares", 0)
    neg_risk = pos.get("neg_risk", False) or market.get("negRisk", False)

    if side == "YES":
        token_id = pt["yes_token"]
    else:
        token_id = pt["no_token"]

    tick_size = "0.001" if neg_risk else "0.01"

    # Price slightly below market for quick fill
    # SELL limit orders near market price get matched instantly on Polymarket
    sell_price = current_price
    if tick_size == "0.001":
        sell_price = round(sell_price, 3)
    else:
        sell_price = round(sell_price, 2)

    if sell_price <= 0.01:
        return False

    try:
        client = get_client()
        order_args = OrderArgs(
            token_id=token_id,
            price=sell_price,
            size=shares,
            side=SELL,
        )
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        resp = client.create_and_post_order(order_args, options)

        status = resp.get("status", "unknown")
        if status == "matched" or resp.get("transactionsHashes"):
            proceeds = shares * sell_price
            profit = proceeds - pos.get("cost", 0)

            log.info(
                f"SOLD: '{pos['market'][:40]}' {shares} shares @ ${sell_price} "
                f"= ${proceeds:.2f} (P&L: ${profit:+.2f})"
            )

            # Update position status
            positions = load_json(POSITIONS_FILE)
            for p in positions:
                if p.get("token_id") == token_id and p.get("status") == "open":
                    p["status"] = "sold"
                    p["sell_price"] = sell_price
                    p["proceeds"] = proceeds
                    p["profit"] = profit
                    p["sold_at"] = datetime.now(timezone.utc).isoformat()
            save_json(POSITIONS_FILE, positions)

            # Record in PnL
            pnl = load_json(PNL_FILE)
            pnl.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": pos["market"],
                "side": side,
                "cost": pos.get("cost", 0),
                "shares": shares,
                "won": profit > 0,
                "payout": proceeds,
                "profit": profit,
                "exit_type": "sell",
                "signal_source": pos.get("signal_source", "heuristic"),
            })
            save_json(PNL_FILE, pnl)

            return True
        else:
            log.warning(f"Sell order not immediately filled: status={status}")
            return False

    except Exception as e:
        log.error(f"Sell failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Position management: check resolved markets, claim winnings
# ---------------------------------------------------------------------------

def check_positions_resolved():
    """Check all open positions to see if their markets have resolved."""
    positions = load_json(POSITIONS_FILE)
    updated = False
    total_claimed = 0.0

    for pos in positions:
        if pos.get("status") != "open":
            continue

        slug = pos.get("slug", "")
        if not slug:
            continue

        try:
            market = fetch_market_by_slug(slug)
            if market is None:
                continue

            closed = market.get("closed", False)
            resolved = market.get("resolved", False)

            if not closed and not resolved:
                end_str = market.get("endDate", "")
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        if datetime.now(timezone.utc) < end_dt:
                            continue
                    except (ValueError, TypeError):
                        pass

            if closed or resolved:
                resolution = market.get("resolution", "")
                winning_side = market.get("winningOutcome", "")

                pos["status"] = "resolved"
                pos["resolution"] = resolution
                pos["resolved_at"] = datetime.now(timezone.utc).isoformat()

                our_side = pos.get("side", "")
                cost = pos.get("cost", 0)
                shares = pos.get("shares", 0)

                won = False
                if winning_side:
                    won = (our_side.upper() == winning_side.upper())
                elif resolution:
                    won = (our_side.upper() == resolution.upper())

                if won:
                    payout = float(shares)
                    profit = payout - cost
                    pos["payout"] = payout
                    pos["profit"] = profit
                    total_claimed += payout
                    log.info(f"WIN: '{pos['market'][:40]}' -> +${profit:.2f} (payout ${payout:.2f})")

                    # Fase 2: Try to redeem on-chain
                    condition_id = pos.get("condition_id", "")
                    if condition_id:
                        redeem_position(condition_id)
                else:
                    pos["payout"] = 0.0
                    pos["profit"] = -cost
                    log.info(f"LOSS: '{pos['market'][:40]}' -> -${cost:.2f}")

                # Record P&L
                pnl = load_json(PNL_FILE)
                pnl.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": pos["market"],
                    "side": our_side,
                    "cost": cost,
                    "shares": shares,
                    "won": won,
                    "payout": pos.get("payout", 0),
                    "profit": pos.get("profit", 0),
                    "exit_type": "resolved",
                    "signal_source": pos.get("signal_source", "heuristic"),
                })
                save_json(PNL_FILE, pnl)
                updated = True

                # Fase 5: Quick learn after each resolved trade
                quick_learn(pos)

            time.sleep(0.5)
        except Exception as e:
            log.error(f"Error checking position '{pos.get('market', '')[:30]}': {e}")

    if updated:
        save_json(POSITIONS_FILE, positions)

    if total_claimed > 0:
        log.info(f"Total claimed from resolved positions: ${total_claimed:.2f}")

    return total_claimed


def check_open_orders():
    """Check and log status of open orders."""
    try:
        client = get_client()
        orders = client.get_orders()
        if orders:
            log.info(f"Open orders: {len(orders)}")
        return orders or []
    except Exception as e:
        log.error(f"Error checking open orders: {e}")
        return []


def cancel_stale_orders():
    """Cancel orders that have been open too long (> 1 hour unfilled)."""
    try:
        client = get_client()
        orders = client.get_orders()
        if not orders:
            return

        for order in orders:
            try:
                created = order.get("createdAt", "") or order.get("timestamp", "")
                if created:
                    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
                    if age_hours > 1:
                        order_id = order.get("id", "") or order.get("orderID", "")
                        if order_id:
                            client.cancel(order_id)
                            log.info(f"Cancelled stale order {order_id[:16]}... (age: {age_hours:.1f}h)")
            except Exception:
                pass
    except Exception as e:
        log.error(f"Error cancelling stale orders: {e}")


# ---------------------------------------------------------------------------
# Fase 5: Quick learn (simple per-source P&L tracking)
# ---------------------------------------------------------------------------

def quick_learn(resolved_pos: dict):
    """
    Called after each resolved trade. Updates rolling per-source stats.
    Simple: track win rate and ROI per signal source, drop losers after 20 trades.
    """
    source = resolved_pos.get("signal_source", "heuristic")
    won = resolved_pos.get("profit", 0) > 0
    cost = resolved_pos.get("cost", 0)
    profit = resolved_pos.get("profit", 0)

    try:
        params = {}
        if STRATEGY_FILE.exists():
            with open(STRATEGY_FILE) as f:
                params = json.load(f)

        source_stats = params.get("source_stats", {})
        s = source_stats.get(source, {"trades": 0, "wins": 0, "total_cost": 0, "total_profit": 0})
        s["trades"] = s.get("trades", 0) + 1
        s["wins"] = s.get("wins", 0) + (1 if won else 0)
        s["total_cost"] = s.get("total_cost", 0) + cost
        s["total_profit"] = s.get("total_profit", 0) + profit
        s["win_rate"] = round(s["wins"] / s["trades"], 3) if s["trades"] > 0 else 0
        s["roi"] = round(s["total_profit"] / s["total_cost"], 4) if s["total_cost"] > 0 else 0
        s["last_updated"] = datetime.now(timezone.utc).isoformat()

        source_stats[source] = s
        params["source_stats"] = source_stats

        # Binary decision: after 20 trades, drop sources with negative ROI
        blocked = params.get("blocked_sources", [])
        if s["trades"] >= 20 and s["roi"] < 0 and source not in blocked:
            blocked.append(source)
            params["blocked_sources"] = blocked
            log.warning(
                f"SOURCE BLOCKED: '{source}' after {s['trades']} trades, "
                f"ROI: {s['roi']*100:.1f}%, win rate: {s['win_rate']*100:.0f}%"
            )

        params["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_json(STRATEGY_FILE, params)

        log.info(
            f"LEARN: {source} -> {'WIN' if won else 'LOSS'} "
            f"(total: {s['trades']}, win rate: {s['win_rate']*100:.0f}%, ROI: {s['roi']*100:.1f}%)"
        )

    except Exception as e:
        log.error(f"Quick learn failed: {e}")


def is_source_blocked(source: str) -> bool:
    """Check if a signal source has been blocked by the learning system."""
    try:
        if STRATEGY_FILE.exists():
            with open(STRATEGY_FILE) as f:
                params = json.load(f)
            return source in params.get("blocked_sources", [])
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Opportunity scanner (updated with signals — Fase 3)
# ---------------------------------------------------------------------------

def scan_opportunities() -> list:
    """
    Scan all markets and return a prioritized list of trading opportunities.
    Uses external signals with dynamic divergence thresholds.
    """
    log.info("Scanning markets for opportunities...")
    markets = fetch_markets(300)
    if not markets:
        log.warning("No markets returned from API")
        return []

    opportunities = []

    for market in markets:
        try:
            pt = parse_prices_and_tokens(market)
            if pt is None:
                continue

            volume_24h = float(market.get("volume24hr", 0))
            liquidity = float(market.get("liquidity", 0))

            if liquidity < MIN_LIQUIDITY or volume_24h < MIN_VOLUME_24H:
                continue

            category = categorize_market(market)

            # Check if we already have a position in this market
            positions = load_json(POSITIONS_FILE)
            already_in = any(
                p.get("token_id") in (pt["yes_token"], pt["no_token"])
                and p.get("status") == "open"
                for p in positions
            )

            signal = None

            # Primary: use external signals (Fase 3)
            signal = evaluate_with_signal(market, pt)

            # Check if source is blocked
            if signal and is_source_blocked(signal.get("signal_source", "")):
                signal = None

            if signal and signal.get("edge", 0) > 0.01:
                opportunities.append({
                    "market": market,
                    "signal": signal,
                    "category": category,
                    "volume_24h": volume_24h,
                    "liquidity": liquidity,
                    "already_in": already_in,
                    "days_to_expiry": days_to_expiry(market),
                })

        except Exception as e:
            log.debug(f"Error analyzing market: {e}")
            continue

    # Sort by priority (source quality), then edge size
    opportunities.sort(key=lambda x: (x["signal"]["priority"], -x["signal"]["edge"]))

    sports_count = sum(1 for o in opportunities if o['category'] == 'sports')
    log.info(
        f"Found {len(opportunities)} signal-informed opportunities "
        f"(sports: {sports_count}, "
        f"short: {sum(1 for o in opportunities if o['category']=='short_term')}, "
        f"medium: {sum(1 for o in opportunities if o['category']=='medium_term')})"
    )

    return opportunities


# ---------------------------------------------------------------------------
# Main trading logic
# ---------------------------------------------------------------------------

def run_trading_cycle(state: dict):
    """One full cycle: scan, evaluate, trade."""
    bankroll = get_balance()
    log.info(f"Current bankroll: ${bankroll:.2f}")

    if bankroll < 1.0:
        log.warning("Bankroll too low to trade. Waiting for resolved positions...")
        return 0

    # Check how many trades we've done today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("trades_today_date") != today:
        state["trades_today"] = 0
        state["trades_today_date"] = today

    trades_needed = max(0, MIN_DAILY_TRADES - state.get("trades_today", 0))

    # Check position exits before scanning for new trades (Fase 4B)
    try:
        check_position_exits()
    except Exception as e:
        log.error(f"Position exit check error: {e}")

    # Scan for opportunities
    opportunities = scan_opportunities()

    if not opportunities:
        log.info("No opportunities found this cycle.")
        return 0

    trades_executed = 0
    max_trades_per_cycle = min(5, max(1, trades_needed))

    for opp in opportunities:
        if trades_executed >= max_trades_per_cycle:
            break

        market = opp["market"]
        signal = opp["signal"]

        if opp["already_in"]:
            continue

        current_balance = bankroll
        if current_balance < 2.0:
            break

        trade = execute_trade(market, signal, bankroll)
        if trade:
            trades_executed += 1
            state["total_trades"] = state.get("total_trades", 0) + 1
            state["trades_today"] = state.get("trades_today", 0) + 1
            bankroll -= trade["cost"]
            time.sleep(1.5)

    log.info(f"Cycle complete: {trades_executed} trades executed. "
             f"Today: {state.get('trades_today', 0)}/{MIN_DAILY_TRADES}")

    return trades_executed


# ---------------------------------------------------------------------------
# Reporting (in Dutch)
# ---------------------------------------------------------------------------

def write_report(state: dict):
    """Write a status report in Dutch to reports/."""
    ensure_dirs()
    now = datetime.now(timezone.utc)
    filename = now.strftime("%Y-%m-%d-%H-%M") + ".md"
    filepath = REPORTS_DIR / filename

    bankroll = get_balance()
    positions = load_json(POSITIONS_FILE)
    trades = load_json(TRADES_FILE)
    pnl = load_json(PNL_FILE)

    open_positions = [p for p in positions if p.get("status") == "open"]
    resolved = [p for p in positions if p.get("status") in ("resolved", "sold")]
    total_invested = sum(p.get("cost", 0) for p in open_positions)
    total_max_payout = sum(p.get("max_payout", 0) for p in open_positions)

    total_profit = sum(p.get("profit", 0) for p in pnl)
    wins = sum(1 for p in pnl if p.get("won", False))
    losses = sum(1 for p in pnl if not p.get("won", True))
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

    today = now.strftime("%Y-%m-%d")
    trades_today = [t for t in trades if t.get("timestamp", "").startswith(today)]

    portfolio_value = bankroll + total_invested

    # Source stats
    source_summary = ""
    try:
        if STRATEGY_FILE.exists():
            with open(STRATEGY_FILE) as f:
                params = json.load(f)
            ss = params.get("source_stats", {})
            if ss:
                source_summary = "\n## Signaal Bronnen Performance\n"
                source_summary += "| Bron | Trades | Win% | ROI | Status |\n"
                source_summary += "|------|--------|------|-----|--------|\n"
                blocked = params.get("blocked_sources", [])
                for src, stats in ss.items():
                    status = "GEBLOKKEERD" if src in blocked else "Actief"
                    source_summary += (
                        f"| {src} | {stats.get('trades', 0)} | "
                        f"{stats.get('win_rate', 0)*100:.0f}% | "
                        f"{stats.get('roi', 0)*100:.1f}% | {status} |\n"
                    )
    except Exception:
        pass

    # Theme exposure
    theme_summary = ""
    theme_costs = {}
    for p in open_positions:
        theme = p.get("theme", "other")
        theme_costs[theme] = theme_costs.get(theme, 0) + p.get("cost", 0)
    if theme_costs:
        theme_summary = "\n## Thema Exposure\n"
        for theme, cost in sorted(theme_costs.items(), key=lambda x: -x[1]):
            pct = cost / portfolio_value * 100 if portfolio_value > 0 else 0
            theme_summary += f"- **{theme}**: ${cost:.2f} ({pct:.0f}%)\n"

    report = f"""# Autobot Verslag - {now.strftime("%Y-%m-%d %H:%M UTC")}

## Samenvatting
- **Saldo (cash):** ${bankroll:.2f}
- **Geinvesteerd:** ${total_invested:.2f}
- **Max uitbetaling open posities:** ${total_max_payout:.2f}
- **Geschatte portfoliowaarde:** ${portfolio_value:.2f}
- **Totale P&L (gesloten):** ${total_profit:+.2f}
- **Win rate:** {win_rate:.0f}% ({wins}W / {losses}L)
- **Signalen:** {'Actief' if SIGNALS_AVAILABLE else 'Niet beschikbaar'}

## Open Posities ({len(open_positions)})
"""

    for p in open_positions:
        report += f"- **{p.get('market', 'Onbekend')[:60]}**\n"
        report += (
            f"  {p.get('side', '?')} | {p.get('shares', 0)} shares @ ${p.get('avg_price', 0):.2f} | "
            f"Kosten: ${p.get('cost', 0):.2f} | Max: ${p.get('max_payout', 0):.2f} | "
            f"Signaal: {p.get('signal_source', 'heuristic')} | Thema: {p.get('theme', '?')}\n"
        )

    report += f"""
## Trades Vandaag ({len(trades_today)})
"""
    for t in trades_today[-10:]:
        report += (
            f"- {t.get('timestamp', '')[:16]} | {t.get('side', '?')} {t.get('shares', t.get('size', 0))} x "
            f"'{t.get('market', '')[:40]}' @ ${t.get('price', 0):.2f} "
            f"[{t.get('signal_source', '?')}]\n"
        )

    report += f"""
## Gesloten Posities ({len(resolved)})
"""
    for p in resolved[-10:]:
        result = "WIN" if p.get("profit", 0) > 0 else "VERLIES"
        exit_type = p.get("status", "resolved")
        report += f"- {result}: {p.get('market', '')[:50]} -> ${p.get('profit', 0):+.2f} ({exit_type})\n"

    report += f"""
{source_summary}
{theme_summary}
## Statistieken
- Totaal trades: {state.get('total_trades', len(trades))}
- Trades vandaag: {state.get('trades_today', len(trades_today))}
- Doel trades/dag: {MIN_DAILY_TRADES}
- Bot running since: {state.get('start_time', 'onbekend')}
- Opeenvolgende fouten: {state.get('consecutive_errors', 0)}

## Strategie v2
- **Primair:** Sports + sportsbook odds (>=2% divergentie)
- **Secundair:** Crypto (model + Deribit), Commodities, Macro
- **Tertiar:** Manifold/Metaculus vergelijking (>=5-8% divergentie)
- **Risk:** Max {MAX_THEME_FRACTION*100:.0f}% per thema, stop loss -{STOP_LOSS_PCT*100:.0f}%, take profit +{TAKE_PROFIT_PCT*100:.0f}%
- **Sizing:** Half-Kelly, max {MAX_POSITION_FRACTION*100:.0f}% per trade, {CASH_RESERVE_FRACTION*100:.0f}% cash reserve
"""

    with open(filepath, "w") as f:
        f.write(report)

    log.info(f"Report written: {filepath}")
    return filepath


# ---------------------------------------------------------------------------
# Git push (via GitHub Contents API)
# ---------------------------------------------------------------------------

def git_push():
    """Push reports and data to GitHub via Contents API."""
    if not GITHUB_PAT:
        log.warning("GITHUB_PAT not set, skipping git push.")
        return

    try:
        remote_url = f"https://x-access-token:{GITHUB_PAT}@github.com/Liquilab/autobot.git"

        cmds = [
            ["git", "-C", str(BASE_DIR), "remote", "set-url", "origin", remote_url],
            ["git", "-C", str(BASE_DIR), "add", "reports/", "data/", "src/"],
            ["git", "-C", str(BASE_DIR), "commit", "-m",
             f"auto: update reports & data {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"],
            ["git", "-C", str(BASE_DIR), "push", "origin", "HEAD"],
        ]

        for cmd in cmds:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0 and "nothing to commit" not in result.stdout + result.stderr:
                log.warning(f"Git command returned {result.returncode}: {result.stderr[:200]}")

        log.info("Git push complete.")
    except Exception as e:
        log.error(f"Git push failed: {e}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def should_run(state: dict, key: str, interval: int) -> bool:
    last = state.get(key)
    if last is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return elapsed >= interval
    except (ValueError, TypeError):
        return True


def main():
    # Fase 6A: Validate env
    validate_env()

    log.info("=" * 60)
    log.info("  AUTOBOT v2 - Autonomous Polymarket Trading Bot")
    log.info("  Signal-informed | Fill-verified | Risk-managed")
    log.info("=" * 60)
    ensure_dirs()
    state = load_state()

    # Initial balance check
    balance = get_balance()
    log.info(f"Starting bankroll: ${balance:.2f}")
    log.info(f"Target: $1,000")
    log.info(f"Signals: {'Available' if SIGNALS_AVAILABLE else 'Not available'}")
    log.info(f"Scan interval: {SCAN_INTERVAL}s | Report interval: {REPORT_INTERVAL}s")
    log.info("")

    # Fase 0: Reconcile positions on startup
    log.info("Running position reconciliation...")
    try:
        reconcile_positions()
    except Exception as e:
        log.error(f"Reconciliation failed: {e}")

    cycle_count = 0

    while True:
        try:
            now_str = datetime.now(timezone.utc).isoformat()
            cycle_count += 1
            log.info(f"--- Cycle {cycle_count} ---")

            # Write heartbeat
            balance = get_balance()
            write_heartbeat(state, balance)

            # Reset error counter on successful cycle start
            state["consecutive_errors"] = 0

            # 1. Check resolved positions (every 5 min)
            if should_run(state, "last_position_check", POSITION_CHECK_INTERVAL):
                try:
                    check_positions_resolved()
                    cancel_stale_orders()
                    state["last_position_check"] = now_str
                except Exception as e:
                    log.error(f"Position check error: {e}\n{traceback.format_exc()}")

            # 2. Scan & trade (every 5 min)
            if should_run(state, "last_scan", SCAN_INTERVAL):
                try:
                    trades_done = run_trading_cycle(state)
                    state["last_scan"] = now_str
                except Exception as e:
                    log.error(f"Trading cycle error: {e}\n{traceback.format_exc()}")

            # 3. Research loop (every 2 hours) — Fase 5
            if should_run(state, "last_research", RESEARCH_INTERVAL):
                try:
                    from research_loop import run_research_loop
                    run_research_loop()
                    state["last_research"] = now_str
                except Exception as e:
                    log.error(f"Research loop error: {e}")

            # 4. Write report (every 8 hours)
            if should_run(state, "last_report", REPORT_INTERVAL):
                try:
                    write_report(state)
                    state["last_report"] = now_str
                except Exception as e:
                    log.error(f"Report error: {e}\n{traceback.format_exc()}")

            # 5. Git push (every hour)
            if should_run(state, "last_git_push", GIT_PUSH_INTERVAL):
                try:
                    git_push()
                    state["last_git_push"] = now_str
                except Exception as e:
                    log.error(f"Git push error: {e}\n{traceback.format_exc()}")

            # Save state
            save_state(state)

            log.info(f"Sleeping 60s until next cycle check...")
            time.sleep(60)

        except KeyboardInterrupt:
            log.info("Bot stopped by user (Ctrl+C).")
            save_state(state)
            break
        except Exception as e:
            # Fase 6C: Self-heal
            state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
            errors = state["consecutive_errors"]
            log.error(f"Unexpected error (#{errors}): {e}\n{traceback.format_exc()}")

            if errors > 10:
                log.warning("10+ consecutive errors — resetting CLOB client...")
                reset_client()

            if errors > 30:
                log.critical("30+ consecutive errors — restarting process...")
                save_state(state)
                os.execv(sys.executable, [sys.executable] + sys.argv)

            save_state(state)
            time.sleep(30)


if __name__ == "__main__":
    main()
