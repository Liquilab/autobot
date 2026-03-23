"""
Autonomous Polymarket Trading Bot
Runs 24/7, scans markets, executes trades, tracks positions, writes reports.
Goal: $100 -> $1,000 in 90 days.
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

CLOB_URL = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CHAIN_ID = 137
SIGNATURE_TYPE = 2  # Gnosis Safe proxy

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS")

# GitHub config
GITHUB_REPO = "https://github.com/Liquilab/autobot"
GITHUB_PAT = "***REMOVED***"

# Timing (seconds)
SCAN_INTERVAL = 300          # 5 minutes
REPORT_INTERVAL = 28800      # 8 hours
GIT_PUSH_INTERVAL = 3600     # 1 hour
POSITION_CHECK_INTERVAL = 300  # 5 minutes

# Strategy parameters
MAX_POSITION_FRACTION = 0.15   # max 15% of bankroll per trade
CASH_RESERVE_FRACTION = 0.05   # keep 5% cash reserve
MIN_DAILY_TRADES = 10
MIN_LIQUIDITY = 5000
MIN_VOLUME_24H = 1000
STALE_POSITION_DAYS = 30       # sell positions older than this if better ops exist

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
            "total_trades": 0,
            "trades_today": 0,
            "trades_today_date": None,
            "initial_bankroll": 100.0,
        }


def save_state(state: dict):
    save_json(STATE_FILE, state)


# ---------------------------------------------------------------------------
# CLOB Client
# ---------------------------------------------------------------------------

_client = None
_client_created_at = 0
CLIENT_TTL = 1800  # recreate client every 30 min to refresh API keys


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

    # Sports check
    for kw in SPORT_KEYWORDS:
        if kw in text:
            return "sports"

    # Time-based categorization
    end_date_str = market.get("endDate", "")
    if end_date_str:
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_to_expiry = (end_date - now).total_seconds() / 86400
            if days_to_expiry <= 7:
                return "short_term"
            elif days_to_expiry <= 30:
                return "medium_term"
            else:
                return "long_term"
        except (ValueError, TypeError):
            pass

    return "medium_term"


def days_to_expiry(market: dict) -> float:
    """Return days until market end. Returns 999 if unknown."""
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
# Edge estimation heuristics
# ---------------------------------------------------------------------------

def estimate_sports_edge(market: dict, pt: dict) -> dict | None:
    """
    For sports markets: bet on heavy favorites (price >= 0.65) whose games
    start within 12 hours. Our edge estimate is a small boost over market price
    for favorites (market tends to underprice favorites slightly).
    """
    yes_price = pt["yes_price"]
    no_price = pt["no_price"]
    h = hours_to_expiry(market)

    # Only trade games starting within 12 hours
    if h > 12:
        return None

    # Favorite is the side with the higher price
    if yes_price >= 0.65 and yes_price <= 0.96:
        # Estimate: favorite's true prob is ~2-5% higher than market
        est_prob = min(0.98, yes_price + 0.03)
        kelly_size = half_kelly(est_prob, yes_price)
        if kelly_size > 0.005:
            return {
                "side": "YES",
                "token_id": pt["yes_token"],
                "price": yes_price,
                "est_prob": est_prob,
                "edge": est_prob - yes_price,
            }
    elif no_price >= 0.65 and no_price <= 0.96:
        est_prob = min(0.98, no_price + 0.03)
        kelly_size = half_kelly(est_prob, no_price)
        if kelly_size > 0.005:
            return {
                "side": "NO",
                "token_id": pt["no_token"],
                "price": no_price,
                "est_prob": est_prob,
                "edge": est_prob - no_price,
            }

    # Also look for coin-flip sports markets (value in underdogs)
    # Skip for now - higher risk
    return None


def estimate_event_edge(market: dict, pt: dict, category: str) -> dict | None:
    """
    For event markets: buy NO on extremely unlikely outcomes.
    Price range for NO: $0.70 - $0.96 (means YES is 4-30%).
    Higher NO price = safer but lower return.
    """
    yes_price = pt["yes_price"]
    no_price = pt["no_price"]
    question = market.get("question", "").lower()
    d = days_to_expiry(market)

    # Priority: short-term NO plays
    if category == "short_term" and no_price >= 0.70 and no_price <= 0.96:
        # Short-term unlikely events: our estimate is the event is even less likely
        # than market suggests. Boost NO probability by 2-5%.
        est_prob = min(0.99, no_price + 0.03)
        edge = est_prob - no_price
        if edge > 0.01:
            return {
                "side": "NO",
                "token_id": pt["no_token"],
                "price": no_price,
                "est_prob": est_prob,
                "edge": edge,
            }

    # Medium-term high-conviction NO plays
    if category == "medium_term" and no_price >= 0.75 and no_price <= 0.96:
        # Look for specific keywords suggesting extreme unlikelihood
        extreme_keywords = [
            "regime", "nuclear", "assassin", "invasion", "annex",
            "martial law", "coup", "world war", "default", "collapse",
            "dip to", "crash", "120", "150", "200",  # extreme price targets
        ]
        is_extreme = any(kw in question for kw in extreme_keywords)
        if is_extreme:
            est_prob = min(0.99, no_price + 0.04)
            edge = est_prob - no_price
            if edge > 0.01:
                return {
                    "side": "NO",
                    "token_id": pt["no_token"],
                    "price": no_price,
                    "est_prob": est_prob,
                    "edge": edge,
                }

    # Also consider YES plays on very likely short-term events
    if category == "short_term" and yes_price >= 0.70 and yes_price <= 0.96:
        est_prob = min(0.99, yes_price + 0.03)
        edge = est_prob - yes_price
        if edge > 0.01:
            return {
                "side": "YES",
                "token_id": pt["yes_token"],
                "price": yes_price,
                "est_prob": est_prob,
                "edge": edge,
            }

    return None


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------

def place_order(token_id: str, price: float, size: int, side: str = BUY,
                tick_size: str = "0.01", neg_risk: bool = False) -> dict | None:
    """Place a limit order. Returns response dict or None on failure."""
    if size < 1:
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
        log.info(f"Order placed: {side} {size} @ ${price} -> {resp.get('status', 'unknown')}")
        return resp
    except Exception as e:
        log.error(f"Order failed: {e}")
        return None


def execute_trade(market: dict, signal: dict, bankroll: float) -> dict | None:
    """Size and execute a trade based on a signal. Returns trade record or None."""
    price = signal["price"]
    est_prob = signal["est_prob"]
    token_id = signal["token_id"]
    side_label = signal["side"]
    neg_risk = market.get("negRisk", False)
    question = market.get("question", "")

    # Determine tick size - neg_risk markets often use 0.001
    tick_size = "0.001" if neg_risk else "0.01"

    # Round price to tick
    if tick_size == "0.001":
        price = round(price, 3)
    else:
        price = round(price, 2)

    # Calculate position size in dollars, then shares
    dollar_size = position_size(bankroll, est_prob, price)
    if dollar_size < 1.0:
        return None

    shares = max(1, int(dollar_size / price))
    cost = round(shares * price, 2)

    # Safety: don't exceed max fraction
    if cost > bankroll * MAX_POSITION_FRACTION:
        shares = max(1, int((bankroll * MAX_POSITION_FRACTION) / price))
        cost = round(shares * price, 2)

    # Don't trade if cost would breach cash reserve
    if cost > bankroll * (1.0 - CASH_RESERVE_FRACTION):
        shares = max(1, int((bankroll * (1.0 - CASH_RESERVE_FRACTION)) / price))
        cost = round(shares * price, 2)

    if shares < 1 or cost < 0.50:
        return None

    log.info(f"Executing: {side_label} {shares} shares of '{question[:50]}' @ ${price} (cost: ${cost})")

    resp = place_order(token_id, price, shares, BUY, tick_size, neg_risk)
    if resp is None or not resp.get("success", False):
        log.warning(f"Trade failed for '{question[:40]}': {resp}")
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
        "status": resp.get("status", "unknown"),
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
    """Add or update a position record after a trade."""
    positions = load_json(POSITIONS_FILE)

    # Check if we already have a position for this token
    existing = None
    for p in positions:
        if p.get("token_id") == trade["token_id"]:
            existing = p
            break

    if existing:
        # Update existing position (average in)
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
        # New position
        positions.append({
            "market": trade["market"],
            "slug": trade.get("slug", ""),
            "condition_id": trade.get("condition_id", ""),
            "token_id": trade["token_id"],
            "side": trade["side"],
            "shares": trade["shares"],
            "avg_price": trade["price"],
            "cost": trade["cost"],
            "max_payout": trade["shares"],  # $1 per share if wins
            "entry_date": trade["timestamp"][:10],
            "end_date": trade.get("end_date", ""),
            "status": "open",
            "category": trade.get("category", "unknown"),
            "neg_risk": trade.get("neg_risk", False),
            "order_id": trade.get("order_id", ""),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        })

    save_json(POSITIONS_FILE, positions)


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
                # Also check end date
                end_str = market.get("endDate", "")
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        if datetime.now(timezone.utc) < end_dt:
                            continue
                    except (ValueError, TypeError):
                        pass

            if closed or resolved:
                # Market has resolved
                resolution = market.get("resolution", "")
                winning_side = market.get("winningOutcome", "")

                pos["status"] = "resolved"
                pos["resolution"] = resolution
                pos["resolved_at"] = datetime.now(timezone.utc).isoformat()

                # Determine P&L
                our_side = pos.get("side", "")
                cost = pos.get("cost", 0)
                shares = pos.get("shares", 0)

                # Check if we won
                won = False
                if winning_side:
                    won = (our_side.upper() == winning_side.upper())
                elif resolution:
                    # Sometimes resolution is "Yes" or "No"
                    won = (our_side.upper() == resolution.upper())

                if won:
                    payout = float(shares)  # $1 per share
                    profit = payout - cost
                    pos["payout"] = payout
                    pos["profit"] = profit
                    total_claimed += payout
                    log.info(f"WIN: '{pos['market'][:40]}' -> +${profit:.2f} (payout ${payout:.2f})")
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
                })
                save_json(PNL_FILE, pnl)
                updated = True

            time.sleep(0.5)  # Rate limit
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
            # The CLOB client returns order objects - cancel old ones
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
# Opportunity scanner
# ---------------------------------------------------------------------------

def scan_opportunities() -> list:
    """
    Scan all markets and return a prioritized list of trading opportunities.
    Each opportunity is a dict with market info + signal.
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

            # Priority 1: Sports
            if category == "sports":
                signal = estimate_sports_edge(market, pt)
                if signal:
                    signal["priority"] = 1

            # Priority 2: Short-term event markets
            if signal is None and category == "short_term":
                signal = estimate_event_edge(market, pt, category)
                if signal:
                    signal["priority"] = 2

            # Priority 3: Medium-term high-conviction NO plays
            if signal is None and category == "medium_term":
                signal = estimate_event_edge(market, pt, category)
                if signal:
                    signal["priority"] = 3

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

    # Sort by priority, then edge
    opportunities.sort(key=lambda x: (x["signal"]["priority"], -x["signal"]["edge"]))

    log.info(f"Found {len(opportunities)} opportunities "
             f"(sports: {sum(1 for o in opportunities if o['category']=='sports')}, "
             f"short: {sum(1 for o in opportunities if o['category']=='short_term')}, "
             f"medium: {sum(1 for o in opportunities if o['category']=='medium_term')})")

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

    # Scan for opportunities
    opportunities = scan_opportunities()

    if not opportunities:
        log.info("No opportunities found this cycle.")
        return 0

    trades_executed = 0
    max_trades_per_cycle = min(5, max(1, trades_needed))  # Cap per cycle

    for opp in opportunities:
        if trades_executed >= max_trades_per_cycle:
            break

        market = opp["market"]
        signal = opp["signal"]

        # Skip if already in this market (unless we want to add)
        if opp["already_in"]:
            continue

        # Check bankroll hasn't depleted
        current_balance = bankroll  # Approximate; actual check is expensive
        if current_balance < 2.0:
            break

        trade = execute_trade(market, signal, bankroll)
        if trade:
            trades_executed += 1
            state["total_trades"] = state.get("total_trades", 0) + 1
            state["trades_today"] = state.get("trades_today", 0) + 1
            bankroll -= trade["cost"]  # Track locally
            time.sleep(1.5)  # Rate limit between orders

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
    resolved = [p for p in positions if p.get("status") == "resolved"]
    total_invested = sum(p.get("cost", 0) for p in open_positions)
    total_max_payout = sum(p.get("max_payout", 0) for p in open_positions)

    # P&L calculations
    total_profit = sum(p.get("profit", 0) for p in pnl)
    wins = sum(1 for p in pnl if p.get("won", False))
    losses = sum(1 for p in pnl if not p.get("won", True))
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

    # Trades today
    today = now.strftime("%Y-%m-%d")
    trades_today = [t for t in trades if t.get("timestamp", "").startswith(today)]

    # Portfolio value estimate
    portfolio_value = bankroll + total_invested  # Conservative estimate

    report = f"""# Autobot Verslag - {now.strftime("%Y-%m-%d %H:%M UTC")}

## Samenvatting
- **Saldo (cash):** ${bankroll:.2f}
- **Geinvesteerd:** ${total_invested:.2f}
- **Max uitbetaling open posities:** ${total_max_payout:.2f}
- **Geschatte portfoliowaarde:** ${portfolio_value:.2f}
- **Totale P&L (gesloten):** ${total_profit:+.2f}
- **Win rate:** {win_rate:.0f}% ({wins}W / {losses}L)

## Open Posities ({len(open_positions)})
"""

    for p in open_positions:
        report += f"- **{p.get('market', 'Onbekend')[:60]}**\n"
        report += f"  {p.get('side', '?')} | {p.get('shares', 0)} shares @ ${p.get('avg_price', 0):.2f} | "
        report += f"Kosten: ${p.get('cost', 0):.2f} | Max: ${p.get('max_payout', 0):.2f}\n"

    report += f"""
## Trades Vandaag ({len(trades_today)})
"""
    for t in trades_today[-10:]:  # Last 10
        report += f"- {t.get('timestamp', '')[:16]} | {t.get('side', '?')} {t.get('shares', t.get('size', 0))} x "
        report += f"'{t.get('market', '')[:40]}' @ ${t.get('price', 0):.2f}\n"

    report += f"""
## Resolved Posities ({len(resolved)})
"""
    for p in resolved[-10:]:
        won = "WIN" if p.get("profit", 0) > 0 else "VERLIES"
        report += f"- {won}: {p.get('market', '')[:50]} -> ${p.get('profit', 0):+.2f}\n"

    report += f"""
## Statistieken
- Totaal trades: {state.get('total_trades', len(trades))}
- Trades vandaag: {state.get('trades_today', len(trades_today))}
- Doel trades/dag: {MIN_DAILY_TRADES}
- Bot running since: {state.get('start_time', 'onbekend')}

## Strategie
- Prioriteit 1: Sports (NBA, NHL, esports) - snelle resolutie
- Prioriteit 2: Short-term event markets (< 7 dagen) - NO op onwaarschijnlijke uitkomsten
- Prioriteit 3: Medium-term high-conviction NO plays
- Position sizing: half-Kelly, max {MAX_POSITION_FRACTION*100:.0f}% per trade
- Cash reserve: {CASH_RESERVE_FRACTION*100:.0f}%

## Volgende Acties
- Blijf markten scannen elke 5 minuten
- Check resolved posities en claim winsten
- Herinvesteer vrij kapitaal onmiddellijk
"""

    with open(filepath, "w") as f:
        f.write(report)

    log.info(f"Report written: {filepath}")
    return filepath


# ---------------------------------------------------------------------------
# Git push
# ---------------------------------------------------------------------------

def git_push():
    """Stage and push reports/ and data/ to GitHub."""
    try:
        # Configure git remote with PAT if needed
        remote_url = f"https://x-access-token:{GITHUB_PAT}@github.com/Liquilab/autobot.git"

        cmds = [
            ["git", "-C", str(BASE_DIR), "remote", "set-url", "origin", remote_url],
            ["git", "-C", str(BASE_DIR), "add", "reports/", "data/"],
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
    """Check if enough time has passed since last run of a task."""
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
    log.info("=" * 60)
    log.info("  AUTOBOT - Autonomous Polymarket Trading Bot")
    log.info("=" * 60)
    ensure_dirs()
    state = load_state()

    # Initial balance check
    balance = get_balance()
    log.info(f"Starting bankroll: ${balance:.2f}")
    log.info(f"Target: $1,000")
    log.info(f"Strategy: Sports favorites + Short-term NO plays + Event markets")
    log.info(f"Scan interval: {SCAN_INTERVAL}s | Report interval: {REPORT_INTERVAL}s")
    log.info("")

    cycle_count = 0

    while True:
        try:
            now_str = datetime.now(timezone.utc).isoformat()
            cycle_count += 1
            log.info(f"--- Cycle {cycle_count} ---")

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

            # 3. Write report (every 8 hours)
            if should_run(state, "last_report", REPORT_INTERVAL):
                try:
                    write_report(state)
                    state["last_report"] = now_str
                except Exception as e:
                    log.error(f"Report error: {e}\n{traceback.format_exc()}")

            # 4. Git push (every hour)
            if should_run(state, "last_git_push", GIT_PUSH_INTERVAL):
                try:
                    git_push()
                    state["last_git_push"] = now_str
                except Exception as e:
                    log.error(f"Git push error: {e}\n{traceback.format_exc()}")

            # Save state
            save_state(state)

            # Sleep until next cycle (60 seconds base, aligned to 5 min intervals)
            log.info(f"Sleeping 60s until next cycle check...")
            time.sleep(60)

        except KeyboardInterrupt:
            log.info("Bot stopped by user (Ctrl+C).")
            save_state(state)
            break
        except Exception as e:
            log.error(f"Unexpected error in main loop: {e}\n{traceback.format_exc()}")
            save_state(state)
            time.sleep(30)  # Back off on unexpected errors


if __name__ == "__main__":
    main()
