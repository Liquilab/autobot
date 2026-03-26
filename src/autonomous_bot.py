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
from web3 import Web3
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

# On-chain redemption config
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com")
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
NEG_RISK_ADAPTER = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
REDEEM_INTERVAL = 600  # Check for redeemable positions every 10 minutes

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
MAX_THEME_FRACTION = 0.40  # max 40% portfolio in correlated positions (was 25%, raised until ODDS_API_KEY available)

# Exit thresholds
# NO take profit — let winners run to expiry. $80 -> $1000 requires compounding big wins.
# Stop loss only for deadweight positions that lock up capital with no recovery prospect.
STOP_LOSS_PCT = 0.60   # sell if position lost 60%+ value (likely not recovering)

# Divergence thresholds per signal source
DIVERGENCE_THRESHOLDS = {
    "sportsbook_high": 0.02,   # 4+ bookmakers
    "sportsbook_low": 0.04,    # 1-2 bookmakers
    "manifold": 0.06,          # Was 0.08 — verlaagd voor meer diversiteit
    "metaculus": 0.05,
    "crypto_model": 0.05,
    "deribit": 0.05,
    "oil_futures": 0.05,
    "fedwatch": 0.05,
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


def get_portfolio_value() -> tuple[float, float, float]:
    """Get total portfolio value: (cash, positions_value, total).
    Queries CLOB for cash and Data API for position values."""
    cash = get_balance()
    positions_value = 0.0
    try:
        if FUNDER_ADDRESS:
            resp = requests.get(
                f"{DATA_API}/positions",
                params={"user": FUNDER_ADDRESS.lower()},
                timeout=15,
            )
            resp.raise_for_status()
            for p in resp.json():
                size = float(p.get("size", 0))
                if size < 0.1:
                    continue
                positions_value += float(p.get("currentValue", 0))
    except Exception as e:
        log.debug(f"Portfolio value fetch failed: {e}")
        # Fallback to local positions
        positions = load_json(POSITIONS_FILE)
        positions_value = sum(
            p.get("current_value", 0) for p in positions if p.get("status") == "open"
        )
    total = cash + positions_value
    return cash, positions_value, total


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


_tick_size_cache: dict[str, str] = {}

def get_tick_size(token_id: str, neg_risk: bool = False) -> str:
    """Query actual tick size from CLOB API. Caches results."""
    if token_id in _tick_size_cache:
        return _tick_size_cache[token_id]
    try:
        r = requests.get(
            f"{CLOB_URL}/tick-size",
            params={"token_id": token_id},
            timeout=10,
        )
        if r.status_code == 200:
            ts = str(r.json().get("minimum_tick_size", "0.01"))
            _tick_size_cache[token_id] = ts
            return ts
    except Exception as e:
        log.debug(f"Tick size query failed for {token_id[:16]}: {e}")
    # Fallback
    fallback = "0.001" if neg_risk else "0.01"
    _tick_size_cache[token_id] = fallback
    return fallback


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


def position_size(bankroll: float, prob_win: float, price: float,
                   source: str = "") -> float:
    """Dollar amount to bet, using learned Kelly and max_fraction per source."""
    available = bankroll * (1.0 - CASH_RESERVE_FRACTION)

    # Load learned parameters
    kelly_mult = 0.5
    max_frac = MAX_POSITION_FRACTION
    try:
        if STRATEGY_FILE.exists():
            with open(STRATEGY_FILE) as f:
                params = json.load(f)
            if source:
                kelly_mult = params.get("source_kelly", {}).get(source, 0.5)
                max_frac = params.get("source_max_fraction", {}).get(source, MAX_POSITION_FRACTION)
    except Exception:
        pass

    raw_kelly = half_kelly(prob_win, price)
    # Apply source-specific Kelly multiplier (half_kelly already halves, so adjust ratio)
    adjusted_kelly = raw_kelly * (kelly_mult / 0.5)
    fraction = min(adjusted_kelly, max_frac)
    return round(available * fraction, 2)


# ---------------------------------------------------------------------------
# Fase 0: Position reconciliation
# ---------------------------------------------------------------------------

def reconcile_positions():
    """
    Reconcile positions.json against on-chain reality via the Data API.

    The Data API is the ONLY source of truth for:
    - Which positions exist (shares, avg_price, cost, current_value, pnl)

    Local positions.json is the source of truth for:
    - Market names, slugs, side labels, signal_source, theme, category

    This runs every cycle to:
    1. Update shares/cost/value for known positions
    2. Detect NEW positions (fills the bot didn't know about)
    3. Detect CLOSED positions (resolved/redeemed on-chain)
    """
    if not FUNDER_ADDRESS:
        log.error("FUNDER_ADDRESS not set, cannot reconcile.")
        return

    # Fetch actual positions from Data API
    try:
        resp = requests.get(
            f"{DATA_API}/positions",
            params={"user": FUNDER_ADDRESS.lower()},
            timeout=15,
        )
        resp.raise_for_status()
        on_chain = resp.json()
    except Exception as e:
        log.error(f"Data API positions fetch failed: {e}")
        return

    if not isinstance(on_chain, list):
        log.error(f"Unexpected Data API response: {type(on_chain)}")
        return

    # Build lookup of on-chain positions by asset_id (token_id)
    chain_by_token = {}
    for oc in on_chain:
        asset_id = oc.get("asset", "")
        size = float(oc.get("size", 0))
        if size < 0.1:
            continue  # Skip dust
        chain_by_token[asset_id] = oc

    # Load current local positions
    old_positions = load_json(POSITIONS_FILE)
    old_open = {p.get("token_id"): p for p in old_positions if p.get("status") == "open"}
    resolved_positions = [p for p in old_positions if p.get("status") not in ("open", None)]
    resolved_token_ids = {p.get("token_id") for p in resolved_positions if p.get("token_id")}

    new_positions = []
    updated = 0
    discovered = 0

    for token_id, oc in chain_by_token.items():
        size = float(oc.get("size", 0))
        avg_price = float(oc.get("avgPrice", 0))
        initial_value = float(oc.get("initialValue", 0))
        current_value = float(oc.get("currentValue", 0))
        cash_pnl = float(oc.get("cashPnl", 0))
        condition_id = oc.get("conditionId", "")

        if token_id in old_open:
            # KNOWN position — update numbers from chain, keep our metadata
            pos = dict(old_open[token_id])  # copy
            pos["shares"] = round(size, 2)
            pos["avg_price"] = round(avg_price, 4)
            pos["cost"] = round(initial_value, 2)
            pos["max_payout"] = round(size, 2)
            pos["current_value"] = round(current_value, 2)
            pos["unrealized_pnl"] = round(cash_pnl, 2)
            pos["last_updated"] = datetime.now(timezone.utc).isoformat()
            new_positions.append(pos)
            updated += 1
        else:
            # Skip positions that were already resolved/dead — don't rediscover
            if token_id in resolved_token_ids:
                continue

            # NEW position — the bot didn't know about this fill
            # This happens when an order was filled but the bot thought it wasn't
            discovered += 1
            question = oc.get("title", f"Unknown (token: {token_id[:16]}...)")
            theme = detect_theme(question)
            log.warning(
                f"DISCOVERED NEW POSITION: '{question[:50]}' "
                f"shares={size:.1f}, cost=${initial_value:.2f}, value=${current_value:.2f}"
            )
            new_positions.append({
                "market": question,
                "slug": "",
                "condition_id": condition_id,
                "token_id": token_id,
                "side": "UNKNOWN",
                "shares": round(size, 2),
                "avg_price": round(avg_price, 4),
                "cost": round(initial_value, 2),
                "max_payout": round(size, 2),
                "current_value": round(current_value, 2),
                "unrealized_pnl": round(cash_pnl, 2),
                "end_date": "",
                "status": "open",
                "category": "unknown",
                "neg_risk": False,
                "signal_source": "unknown",
                "theme": theme,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            })

    # Check for positions that disappeared from chain (resolved/redeemed)
    closed = 0
    for token_id, pos in old_open.items():
        if token_id not in chain_by_token:
            # Position no longer on chain — it was resolved or redeemed
            pos["status"] = "resolved"
            pos["resolved_at"] = datetime.now(timezone.utc).isoformat()
            # If current_value was tracked, use that for pnl
            cost = pos.get("cost", 0)
            last_value = pos.get("current_value", 0)
            if last_value > cost:
                pos["profit"] = round(last_value - cost, 2)
                pos["won"] = True
                log.info(f"RESOLVED (on-chain): '{pos.get('market', '?')[:40]}' -> +${pos['profit']:.2f}")
            else:
                pos["profit"] = round(last_value - cost, 2)
                pos["won"] = False
                log.info(f"RESOLVED (on-chain): '{pos.get('market', '?')[:40]}' -> ${pos['profit']:.2f}")
            resolved_positions.append(pos)
            closed += 1

            # Record in PnL
            pnl = load_json(PNL_FILE)
            pnl.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": pos.get("market", ""),
                "side": pos.get("side", ""),
                "cost": cost,
                "shares": pos.get("shares", 0),
                "won": pos.get("won", False),
                "payout": last_value,
                "profit": pos.get("profit", 0),
                "exit_type": "resolved_onchain",
                "signal_source": pos.get("signal_source", "unknown"),
            })
            save_json(PNL_FILE, pnl)

            # Quick learn
            quick_learn(pos)

    all_positions = resolved_positions + new_positions

    total_cost = sum(p["cost"] for p in new_positions)
    total_value = sum(p["current_value"] for p in new_positions)
    total_pnl = sum(p["unrealized_pnl"] for p in new_positions)

    save_json(POSITIONS_FILE, all_positions)

    log.info(
        f"RECONCILE: {len(new_positions)} positions, "
        f"${total_cost:.2f} invested, ${total_value:.2f} value, ${total_pnl:+.2f} pnl"
        f"{f', {discovered} NEW discovered' if discovered else ''}"
        f"{f', {closed} resolved on-chain' if closed else ''}"
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

RELAYER_URL = "https://relayer-v2.polymarket.com"


def get_web3():
    """Get Web3 connection to Polygon (PoA chain)."""
    from web3.middleware import ExtraDataToPOAMiddleware
    rpcs = [POLYGON_RPC, "https://rpc.ankr.com/polygon", "https://polygon.drpc.org"]
    for rpc in rpcs:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            if w3.is_connected():
                return w3
        except Exception:
            continue
    return None


def _get_clob_api_creds():
    """Get CLOB API credentials for relayer authentication."""
    client = get_client()
    return client.creds


def _build_relayer_headers(api_creds, method: str, path: str, body: str = ""):
    """Build HMAC authentication headers for the relayer (same format as CLOB Builder keys)."""
    import hmac as hmac_mod
    import hashlib
    import base64

    timestamp = str(int(time.time()))
    message = timestamp + method + path
    if body:
        message += body

    secret_bytes = base64.urlsafe_b64decode(api_creds.api_secret)
    signature = base64.urlsafe_b64encode(
        hmac_mod.new(secret_bytes, message.encode(), hashlib.sha256).digest()
    ).decode()

    return {
        "POLY_BUILDER_API_KEY": api_creds.api_key,
        "POLY_BUILDER_TIMESTAMP": timestamp,
        "POLY_BUILDER_PASSPHRASE": api_creds.api_passphrase,
        "POLY_BUILDER_SIGNATURE": signature,
        "Content-Type": "application/json",
    }


def _sign_safe_tx(to: str, data: str, safe_nonce: int) -> str:
    """
    Sign a Gnosis Safe transaction using EIP-712 typed data.
    Returns the signature hex string.
    """
    from eth_account.messages import encode_defunct
    from eth_account import Account

    safe_addr = Web3.to_checksum_address(FUNDER_ADDRESS)

    # EIP-712 domain separator for Gnosis Safe
    DOMAIN_TYPEHASH = Web3.keccak(text="EIP712Domain(uint256 chainId,address verifyingContract)")
    domain_separator = Web3.keccak(
        b'\x00' * 12 +  # padding
        DOMAIN_TYPEHASH +
        int(CHAIN_ID).to_bytes(32, "big") +
        bytes.fromhex(safe_addr[2:].lower().zfill(64))
    )
    # Simpler: use eth_abi for proper encoding
    from eth_abi import encode as abi_encode
    domain_separator = Web3.keccak(
        abi_encode(
            ["bytes32", "uint256", "address"],
            [DOMAIN_TYPEHASH, CHAIN_ID, safe_addr]
        )
    )

    # Safe transaction typehash
    SAFE_TX_TYPEHASH = Web3.keccak(
        text="SafeTx(address to,uint256 value,bytes data,uint8 operation,"
             "uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,"
             "address gasToken,address refundReceiver,uint256 _nonce)"
    )

    # Hash the data
    data_bytes = bytes.fromhex(data.replace("0x", "")) if data else b""
    data_hash = Web3.keccak(data_bytes)

    # Encode the Safe tx struct
    zero_addr = "0x0000000000000000000000000000000000000000"
    safe_tx_hash = Web3.keccak(
        abi_encode(
            ["bytes32", "address", "uint256", "bytes32", "uint8",
             "uint256", "uint256", "uint256", "address", "address", "uint256"],
            [SAFE_TX_TYPEHASH, Web3.to_checksum_address(to), 0, data_hash, 0,
             0, 0, 0, zero_addr, zero_addr, safe_nonce]
        )
    )

    # EIP-712 message hash
    msg_hash = Web3.keccak(b"\x19\x01" + domain_separator + safe_tx_hash)

    # Sign with private key
    account = Account.from_key(PRIVATE_KEY)
    signed = account.unsafe_sign_hash(msg_hash)

    # Return signature in compact form (r + s + v)
    r = signed.r.to_bytes(32, "big")
    s = signed.s.to_bytes(32, "big")
    v = signed.v.to_bytes(1, "big")
    return "0x" + (r + s + v).hex()


def _encode_redeem_calldata(condition_id: str) -> str:
    """Encode redeemPositions calldata for the CTF contract."""
    from eth_abi import encode as abi_encode
    usdc_addr = Web3.to_checksum_address(USDC_E)
    cid_bytes = bytes.fromhex(condition_id.replace("0x", "")[:64].ljust(64, "0"))
    encoded_args = abi_encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [usdc_addr, b'\x00' * 32, cid_bytes, [1, 2]]
    )
    return "0x01b7037c" + encoded_args.hex()


def _redeem_via_relayer(condition_id: str, redeem_data: str) -> bool:
    """Try redemption via Polymarket relayer (gas-free). Needs RELAYER_API_KEY env var."""
    relayer_key = os.getenv("RELAYER_API_KEY", "")
    if not relayer_key:
        return False

    try:
        from py_builder_relayer_client.builder.safe import create_struct_hash, create_safe_signature, split_and_pack_sig
        from py_builder_relayer_client.signer import Signer as RelayerSigner
        from py_builder_relayer_client.models import OperationType, SafeTransaction as RelayerSafeTx
        from py_builder_relayer_client.client import build_safe_transaction_request, SafeTransactionArgs
        from py_builder_relayer_client.config import get_contract_config

        ctf_addr = Web3.to_checksum_address(CONDITIONAL_TOKENS)
        signer = RelayerSigner(PRIVATE_KEY, CHAIN_ID)
        config = get_contract_config(CHAIN_ID)

        # Get Safe nonce from relayer
        r = requests.get(
            f"{RELAYER_URL}/nonce",
            params={"address": WALLET_ADDRESS, "type": "SAFE"},
            timeout=10
        )
        if r.status_code != 200:
            return False
        nonce = r.json()["nonce"]

        # Build and sign request using official SDK
        tx = RelayerSafeTx(to=ctf_addr, value="0", data=redeem_data, operation=OperationType.Call)
        args = SafeTransactionArgs(from_address=WALLET_ADDRESS, nonce=nonce, chain_id=CHAIN_ID, transactions=[tx])
        txn_request = build_safe_transaction_request(signer=signer, args=args, config=config, metadata="Redeem").to_dict()

        headers = {
            "RELAYER_API_KEY": relayer_key,
            "RELAYER_API_KEY_ADDRESS": WALLET_ADDRESS,
            "Content-Type": "application/json",
        }

        resp = requests.post(f"{RELAYER_URL}/submit", json=txn_request, headers=headers, timeout=30)
        if resp.status_code == 200:
            result = resp.json()
            log.info(f"REDEEM via relayer: {condition_id[:16]}... txID={result.get('transactionID', '?')[:20]}")
            return True
        else:
            log.debug(f"Relayer rejected: {resp.status_code} {resp.text[:100]}")
            return False

    except Exception as e:
        log.error(f"Relayer redeem error: {e}")
        return False


def _redeem_via_onchain(condition_id: str, redeem_data: str) -> bool:
    """Try redemption via direct on-chain tx (needs POL for gas)."""
    w3 = get_web3()
    if not w3:
        return False

    wallet_cs = Web3.to_checksum_address(WALLET_ADDRESS)
    funder_cs = Web3.to_checksum_address(FUNDER_ADDRESS)
    ctf_cs = Web3.to_checksum_address(CONDITIONAL_TOKENS)

    # Check gas balance
    pol_balance = w3.eth.get_balance(wallet_cs)
    gas_price = w3.eth.gas_price
    min_cost = 150000 * gas_price
    if pol_balance < min_cost:
        return False

    # Gnosis Safe execTransaction ABI
    SAFE_EXEC_ABI = json.loads('[{"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"signatures","type":"bytes"}],"name":"execTransaction","outputs":[{"name":"success","type":"bool"}],"stateMutability":"payable","type":"function"}]')

    safe = w3.eth.contract(address=funder_cs, abi=SAFE_EXEC_ABI)

    # Owner signature: r=owner, s=0, v=1
    owner_bytes = bytes.fromhex(WALLET_ADDRESS.replace("0x", "").lower().zfill(64))
    signature = owner_bytes + b'\x00' * 32 + b'\x01'

    nonce = w3.eth.get_transaction_count(wallet_cs)
    tx = safe.functions.execTransaction(
        ctf_cs, 0, bytes.fromhex(redeem_data[2:]), 0, 0, 0, 0,
        "0x0000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000000",
        signature
    ).build_transaction({
        "from": wallet_cs, "nonce": nonce, "gas": 150000,
        "gasPrice": int(gas_price * 1.5), "chainId": CHAIN_ID,
    })

    account = w3.eth.account.from_key(PRIVATE_KEY)
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt["status"] == 1:
        log.info(f"REDEEM on-chain: {condition_id[:16]}... tx={tx_hash.hex()[:16]}")
        return True
    return False


def redeem_position(condition_id: str) -> bool:
    """
    Redeem a resolved position. Tries relayer (gas-free) first, falls back to on-chain.
    """
    if not condition_id or not PRIVATE_KEY or not FUNDER_ADDRESS:
        return False

    try:
        redeem_data = _encode_redeem_calldata(condition_id)

        # Try 1: Relayer (gas-free, needs RELAYER_API_KEY)
        if _redeem_via_relayer(condition_id, redeem_data):
            return True

        # Try 2: On-chain (needs POL for gas)
        if _redeem_via_onchain(condition_id, redeem_data):
            return True

        log.warning(
            f"Cannot redeem {condition_id[:16]}... "
            f"Need RELAYER_API_KEY env var (from polymarket.com/settings) "
            f"or POL on wallet for gas"
        )
        return False

    except Exception as e:
        log.error(f"Redeem failed for {condition_id[:16]}...: {e}")
        return False


def check_and_redeem_positions():
    """
    Check Data API for redeemable positions and redeem them via relayer (gas-free).
    Returns total USDC redeemed.
    """
    if not FUNDER_ADDRESS:
        return 0

    try:
        funder = FUNDER_ADDRESS.lower()
        resp = requests.get(
            f"{DATA_API}/positions",
            params={"user": funder},
            timeout=15
        )
        if resp.status_code != 200:
            return 0

        positions = resp.json()
        redeemable = [p for p in positions if p.get("redeemable")]

        if not redeemable:
            return 0

        # Only try if we have a way to redeem (relayer key or POL)
        has_relayer = bool(os.getenv("RELAYER_API_KEY", ""))
        has_pol = False
        try:
            w3 = get_web3()
            if w3:
                wallet_cs = Web3.to_checksum_address(WALLET_ADDRESS)
                pol = w3.eth.get_balance(wallet_cs)
                has_pol = pol > 150000 * w3.eth.gas_price
        except Exception:
            pass

        if not has_relayer and not has_pol:
            # Only log once per hour
            total_payout = sum(float(p.get("payout", 0)) for p in redeemable)
            if total_payout > 0:
                log.warning(
                    f"{len(redeemable)} redeemable positions (${total_payout:.2f} payout) "
                    f"but no RELAYER_API_KEY and no POL for gas"
                )
            return 0

        log.info(f"Found {len(redeemable)} redeemable positions")

        total_redeemed = 0
        redeemed_count = 0

        for pos in redeemable:
            condition_id = pos.get("conditionId", "")
            payout = float(pos.get("payout", 0))
            title = pos.get("title", "?")[:40]

            if not condition_id:
                continue

            log.info(f"Redeeming: '{title}' payout=${payout:.2f}")

            if redeem_position(condition_id):
                total_redeemed += payout
                redeemed_count += 1
                time.sleep(3)  # Wait between redemptions for nonce update
            else:
                # If first attempt fails, no point trying the rest with same method
                break

        if redeemed_count > 0:
            log.info(f"Redeemed {redeemed_count} positions, total payout: ${total_redeemed:.2f}")

        return total_redeemed

    except Exception as e:
        log.error(f"Redeem check error: {e}")
        return 0


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
    """Get the appropriate divergence threshold for a signal source.
    Uses learned thresholds from research_loop if available."""
    source = signal.get("source", "")
    confidence = signal.get("confidence", 0)

    # Check learned thresholds first
    try:
        if STRATEGY_FILE.exists():
            with open(STRATEGY_FILE) as f:
                params = json.load(f)
            learned = params.get("source_thresholds", {})
            if source in learned:
                return learned[source]
    except Exception:
        pass

    # Fallback to hardcoded defaults
    if source == "sportsbook":
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

    # Sanity checks
    if est_prob <= price:
        return None

    # Skip extreme penny markets (price <$0.05 = <5% probability)
    if price < 0.05:
        return None

    # Skip very high price markets (>$0.96 = tiny upside)
    if price > 0.96:
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

    tick_size = get_tick_size(token_id, neg_risk)
    if tick_size == "0.001":
        price = round(price, 3)
    else:
        price = round(price, 2)

    # Check existing position size BEFORE sizing new order
    # This prevents accidental over-allocation from repeated fills
    # Check ALL statuses (not just open) to prevent rebuying resolved positions
    positions = load_json(POSITIONS_FILE)
    existing_cost = sum(
        p.get("cost", 0) for p in positions
        if p.get("token_id") == token_id
    )
    max_per_position = bankroll * MAX_POSITION_FRACTION
    remaining_budget = max(0, max_per_position - existing_cost)

    if existing_cost > 0:
        if remaining_budget < 1.0:
            log.info(f"SKIP (max position): '{question[:40]}' already ${existing_cost:.2f} invested (max ${max_per_position:.2f})")
            return None
        log.info(f"Existing position ${existing_cost:.2f} in '{question[:30]}', budget remaining: ${remaining_budget:.2f}")

    # Check subcategory block
    source_name = signal.get("signal_source", "")
    try:
        if STRATEGY_FILE.exists():
            with open(STRATEGY_FILE) as f:
                sparams = json.load(f)
            from research_loop import classify_subcategory
            subcat = classify_subcategory(question, source_name)
            if subcat in sparams.get("blocked_subcategories", []):
                log.info(f"SKIP (blocked subcategory): '{question[:40]}' subcat={subcat}")
                return None
    except Exception:
        pass

    dollar_size = position_size(bankroll, est_prob, price, source=source_name)
    if dollar_size < 1.0:
        return None

    # Cap by remaining budget for this position
    if existing_cost > 0:
        dollar_size = min(dollar_size, remaining_budget)

    shares = max(5, int(dollar_size / price))  # Min 5 shares (Polymarket minimum)
    cost = round(shares * price, 2)

    # Safety caps
    if cost > max_per_position - existing_cost:
        shares = max(5, int((max_per_position - existing_cost) / price))
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
        # If position was resolved/dead but we're buying back in, reopen it
        if existing.get("status") not in ("open", "matched"):
            existing["status"] = "open"
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
    Check open positions for exit conditions.
    NO take profit — let winners run. $80→$1000 requires compounding big wins.
    Stop loss only for deadweight (>60% loss) to free capital for better opportunities.
    Uses current_value from Data API reconciliation.
    """
    positions = load_json(POSITIONS_FILE)
    exits_done = 0

    for pos in positions:
        if pos.get("status") != "open":
            continue

        cost = pos.get("cost", 0)
        current_value = pos.get("current_value")

        # Need current_value from reconciliation
        if current_value is None or cost <= 0:
            continue

        pnl_pct = (current_value - cost) / cost

        # Only exit: deadweight positions losing >60% with no recovery prospect
        if pnl_pct < -STOP_LOSS_PCT:
            # If value is $0 or near-zero, mark as resolved loss — nothing to sell
            if current_value < 0.01:
                log.info(
                    f"DEAD POSITION: '{pos['market'][:40]}' "
                    f"cost=${cost:.2f}, value=$0 — marking as resolved loss"
                )
                positions = load_json(POSITIONS_FILE)
                for p in positions:
                    if p.get("token_id") == pos.get("token_id") and p.get("status") == "open":
                        p["status"] = "resolved_loss"
                        p["profit"] = -cost
                        p["resolved_at"] = datetime.now(timezone.utc).isoformat()
                save_json(POSITIONS_FILE, positions)

                # Record in PnL
                pnl = load_json(PNL_FILE)
                pnl.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": pos.get("market", ""),
                    "side": pos.get("side", ""),
                    "cost": cost,
                    "shares": pos.get("shares", 0),
                    "won": False,
                    "payout": 0,
                    "profit": -cost,
                    "exit_type": "dead_position",
                    "signal_source": pos.get("signal_source", "unknown"),
                })
                save_json(PNL_FILE, pnl)
                exits_done += 1
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

                side = pos.get("side", "").upper()
                if side == "YES":
                    current_price = pt["yes_price"]
                else:
                    current_price = pt["no_price"]

                log.info(
                    f"STOP LOSS: '{pos['market'][:40]}' "
                    f"P&L: {pnl_pct*100:.1f}% (threshold: -{STOP_LOSS_PCT*100:.0f}%)"
                )
                success = sell_position(pos, market, pt, current_price)
                if success:
                    exits_done += 1

                time.sleep(0.5)
            except Exception as e:
                log.debug(f"Error checking exit for '{pos.get('market', '')[:30]}': {e}")

    if exits_done > 0:
        log.info(f"Exited {exits_done} deadweight positions")

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

    tick_size = get_tick_size(token_id, neg_risk)

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
        err_str = str(e)
        log.error(f"Sell failed: {e}")
        # If balance/allowance error, tokens are likely already redeemed/gone
        if "not enough balance" in err_str or "allowance" in err_str:
            log.info(f"Marking '{pos['market'][:40]}' as resolved (tokens gone from chain)")
            positions = load_json(POSITIONS_FILE)
            for p in positions:
                if p.get("token_id") == token_id and p.get("status") == "open":
                    p["status"] = "resolved_loss"
                    p["profit"] = -pos.get("cost", 0)
                    p["resolved_at"] = datetime.now(timezone.utc).isoformat()
            save_json(POSITIONS_FILE, positions)
        return False


# ---------------------------------------------------------------------------
# Position management: check resolved markets, claim winnings
# ---------------------------------------------------------------------------

def fetch_market_by_condition(condition_id: str) -> dict | None:
    """Fetch market by conditionId — more reliable than slug."""
    data = api_get(f"{GAMMA_API}/markets", {"conditionId": condition_id})
    if isinstance(data, list) and data:
        return data[0]
    return None


def check_positions_resolved():
    """Check all open positions to see if their markets have resolved.
    Uses conditionId for lookup (not slug) to avoid wrong market matches."""
    positions = load_json(POSITIONS_FILE)
    updated = False
    total_claimed = 0.0

    for pos in positions:
        if pos.get("status") != "open":
            continue

        condition_id = pos.get("condition_id", "")
        slug = pos.get("slug", "")
        if not condition_id and not slug:
            continue

        try:
            # Prefer conditionId lookup, fall back to slug
            market = None
            if condition_id:
                market = fetch_market_by_condition(condition_id)
            if market is None and slug:
                market = fetch_market_by_slug(slug)
            if market is None:
                continue

            # Sanity check 1: does the returned market match our position?
            market_question = market.get("question", "").lower()
            pos_market = pos.get("market", "").lower()
            if pos_market and market_question and "unknown" not in pos_market:
                pos_words = set(w for w in pos_market.split() if len(w) > 3)
                market_words = set(w for w in market_question.split() if len(w) > 3)
                if pos_words and market_words and not pos_words.intersection(market_words):
                    log.warning(
                        f"Market mismatch! Position: '{pos_market[:40]}' vs "
                        f"API returned: '{market_question[:40]}' — skipping"
                    )
                    continue

            # Sanity check 2: if market endDate is ancient, don't trust it
            api_end = market.get("endDate", "")
            if api_end:
                try:
                    end_dt = datetime.fromisoformat(api_end.replace("Z", "+00:00"))
                    days_ago = (datetime.now(timezone.utc) - end_dt).total_seconds() / 86400
                    if days_ago > 7:
                        log.warning(
                            f"Stale market returned for '{pos_market[:40]}' "
                            f"(ended {days_ago:.0f}d ago) — skipping resolved check"
                        )
                        continue
                except (ValueError, TypeError):
                    pass

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
    cash, pos_val, total = get_portfolio_value()
    bankroll = cash
    log.info(f"Portfolio: ${total:.2f} (cash: ${cash:.2f}, positions: ${pos_val:.2f})")

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
- **Risk:** Max {MAX_THEME_FRACTION*100:.0f}% per thema, stop loss -{STOP_LOSS_PCT*100:.0f}%
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
    cash, pos_val, total = get_portfolio_value()
    balance = cash
    log.info(f"Portfolio: ${total:.2f} (cash: ${cash:.2f}, positions: ${pos_val:.2f})")
    log.info(f"Target: $1,000")
    log.info(f"Signals: {'Available' if SIGNALS_AVAILABLE else 'Not available'}")
    log.info(f"Scan interval: {SCAN_INTERVAL}s | Report interval: {REPORT_INTERVAL}s")
    log.info("")

    # Fase 0: Initial reconciliation
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

            # 1. Reconcile & check positions (every 5 min)
            if should_run(state, "last_position_check", POSITION_CHECK_INTERVAL):
                try:
                    reconcile_positions()
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

            # 2b. Redeem resolved positions (every 10 min)
            if should_run(state, "last_redeem", REDEEM_INTERVAL):
                try:
                    redeemed = check_and_redeem_positions()
                    state["last_redeem"] = now_str
                    if redeemed > 0:
                        log.info(f"Redeemed ${redeemed:.2f} — reinvesting next cycle")
                except Exception as e:
                    log.error(f"Redeem error: {e}")

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
