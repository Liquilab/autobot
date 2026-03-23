"""
Polymarket Trading Bot
Goal: $100 USDC.e -> $1,000 in 90 days

Main trading loop:
1. Scan markets for opportunities
2. Evaluate edge using simple heuristics
3. Size positions with half-Kelly
4. Place orders and manage positions
5. Track P&L
"""
import os
import json
import time
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

CLOB_URL = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# Position tracking file
POSITIONS_FILE = "data/positions.json"
TRADES_FILE = "data/trades.json"
PNL_FILE = "data/pnl.json"


def ensure_data_dir():
    os.makedirs("data", exist_ok=True)
    for f in [POSITIONS_FILE, TRADES_FILE, PNL_FILE]:
        if not os.path.exists(f):
            with open(f, "w") as fh:
                json.dump([], fh)


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def get_client():
    """Create authenticated CLOB client."""
    client = ClobClient(CLOB_URL, key=PRIVATE_KEY, chain_id=CHAIN_ID)
    try:
        creds = client.derive_api_key()
    except Exception:
        creds = client.create_api_key()
    client.set_api_creds(creds)
    return client


def get_markets(limit=100):
    """Fetch active markets sorted by 24h volume."""
    r = requests.get(f"{GAMMA_API}/markets", params={
        "active": "true",
        "closed": "false",
        "limit": limit,
        "order": "volume24hr",
        "ascending": "false",
    })
    return r.json()


def get_market_by_slug(slug):
    """Fetch a specific market by slug."""
    r = requests.get(f"{GAMMA_API}/markets", params={"slug": slug})
    markets = r.json()
    return markets[0] if markets else None


def evaluate_sports_market(market):
    """
    Simple evaluation for sports markets.
    Returns (side, token_id, price, confidence) or None.
    """
    question = market.get("question", "")
    prices_raw = market.get("outcomePrices", "")
    clob_ids_raw = market.get("clobTokenIds", "")

    if not prices_raw or not clob_ids_raw:
        return None

    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        clob_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
    except (json.JSONDecodeError, ValueError):
        return None

    if len(prices) < 2 or len(clob_ids) < 2:
        return None

    yes_price = float(prices[0])
    no_price = float(prices[1])
    yes_token = clob_ids[0]
    no_token = clob_ids[1]

    # For sports, we don't have an edge model yet.
    # Skip for now - will be implemented with real analysis
    return None


def evaluate_event_market(market):
    """
    Evaluate event/political markets for mispricing.
    Returns (side, token_id, price, estimated_prob, edge) or None.
    """
    question = market.get("question", "").lower()
    prices_raw = market.get("outcomePrices", "")
    clob_ids_raw = market.get("clobTokenIds", "")
    volume_24h = float(market.get("volume24hr", 0))
    liquidity = float(market.get("liquidity", 0))

    if not prices_raw or not clob_ids_raw:
        return None
    if liquidity < 50000:
        return None

    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        clob_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
    except (json.JSONDecodeError, ValueError):
        return None

    if len(prices) < 2 or len(clob_ids) < 2:
        return None

    yes_price = float(prices[0])
    no_price = float(prices[1])
    yes_token = clob_ids[0]
    no_token = clob_ids[1]
    neg_risk = market.get("negRisk", False)

    return {
        "question": market.get("question", ""),
        "slug": market.get("slug", ""),
        "yes_price": yes_price,
        "no_price": no_price,
        "yes_token": yes_token,
        "no_token": no_token,
        "volume_24h": volume_24h,
        "liquidity": liquidity,
        "neg_risk": neg_risk,
        "end_date": market.get("endDate", ""),
    }


def half_kelly(prob_win, price):
    """Calculate half-Kelly bet fraction."""
    if price <= 0 or price >= 1 or prob_win <= 0 or prob_win >= 1:
        return 0
    odds = 1.0 / price
    b = odds - 1
    q = 1 - prob_win
    f = (b * prob_win - q) / b
    return max(0, f / 2)


def calculate_position_size(bankroll, prob_win, price, max_fraction=0.15):
    """Calculate dollar amount to bet."""
    kelly = half_kelly(prob_win, price)
    fraction = min(kelly, max_fraction)
    return round(bankroll * fraction, 2)


def place_order(client, token_id, price, size, side=BUY, tick_size="0.01", neg_risk=False):
    """Place a limit order and log it."""
    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side=side,
    )

    print(f"Placing {side} order: {size} shares @ ${price} (token: {token_id[:16]}...)")

    try:
        resp = client.create_and_post_order(
            order_args,
            options={"tick_size": tick_size, "neg_risk": neg_risk},
            order_type=OrderType.GTC,
        )
        print(f"  Order response: {resp}")

        # Log the trade
        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "token_id": token_id,
            "side": side,
            "price": price,
            "size": size,
            "response": str(resp),
        }
        trades = load_json(TRADES_FILE)
        trades.append(trade)
        save_json(TRADES_FILE, trades)

        return resp
    except Exception as e:
        print(f"  Order failed: {e}")
        return None


def get_open_orders(client):
    """Get all open orders."""
    try:
        return client.get_orders()
    except Exception as e:
        print(f"Error getting orders: {e}")
        return []


def get_balances(client):
    """Get token balances (positions)."""
    # Note: The CLOB client may not directly expose balance checks
    # We track positions locally
    return load_json(POSITIONS_FILE)


def run_scan(client):
    """Scan markets and print opportunities."""
    print(f"\n{'='*60}")
    print(f"Market Scan: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    markets = get_markets(100)
    opportunities = []

    for market in markets:
        info = evaluate_event_market(market)
        if info:
            opportunities.append(info)

    # Sort by volume
    opportunities.sort(key=lambda x: x["volume_24h"], reverse=True)

    print(f"Found {len(opportunities)} tradeable markets\n")

    for i, opp in enumerate(opportunities[:20]):
        print(f"{i+1:3}. {opp['question'][:55]}")
        print(f"     YES: ${opp['yes_price']:.3f}  NO: ${opp['no_price']:.3f}  "
              f"Vol: ${opp['volume_24h']:,.0f}  Liq: ${opp['liquidity']:,.0f}")

    return opportunities


def run_trading_loop(client, bankroll=100.0):
    """Main trading loop - scan and execute."""
    ensure_data_dir()

    print(f"\n🤖 Polymarket Trading Bot Started")
    print(f"   Bankroll: ${bankroll:.2f}")
    print(f"   Target: $1,000")
    print(f"   Strategy: Event + Sports markets with Kelly sizing\n")

    while True:
        try:
            # Scan markets
            opportunities = run_scan(client)

            # Check open orders
            orders = get_open_orders(client)
            print(f"\nOpen orders: {len(orders) if orders else 0}")

            # Wait before next scan
            print(f"\nNext scan in 5 minutes...")
            time.sleep(300)

        except KeyboardInterrupt:
            print("\nBot stopped by user.")
            break
        except Exception as e:
            print(f"\nError in trading loop: {e}")
            time.sleep(60)


if __name__ == "__main__":
    ensure_data_dir()
    print("Connecting to Polymarket CLOB...")
    client = get_client()
    print("Connected!\n")

    # For now, just scan - don't auto-trade yet
    run_scan(client)
