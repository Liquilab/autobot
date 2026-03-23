"""Polymarket CLOB client wrapper - configured for Gnosis Safe (type 2) proxy wallet."""
import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs, PartialCreateOrderOptions, BalanceAllowanceParams
)
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

CLOB_URL = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS")
SIGNATURE_TYPE = 2  # GNOSIS_SAFE


def get_client():
    """Create and return an authenticated CLOB client."""
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
    return client


def get_balance(client):
    """Get USDC.e balance available for trading."""
    params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=SIGNATURE_TYPE)
    result = client.get_balance_allowance(params)
    return int(result.get("balance", 0)) / 1e6


def get_orderbook(client, token_id):
    """Get the full orderbook for a token."""
    return client.get_order_book(token_id)


def get_best_prices(client, token_id):
    """Get the best bid and ask from the orderbook."""
    book = client.get_order_book(token_id)
    bids = sorted(book.bids, key=lambda x: float(x.price), reverse=True)
    asks = sorted(book.asks, key=lambda x: float(x.price))

    best_bid = float(bids[0].price) if bids else 0
    best_ask = float(asks[0].price) if asks else 1
    bid_size = float(bids[0].size) if bids else 0
    ask_size = float(asks[0].size) if asks else 0

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "spread": best_ask - best_bid,
        "mid": (best_bid + best_ask) / 2,
    }


def place_limit_order(client, token_id, price, size, side=BUY, tick_size="0.01", neg_risk=False):
    """Place a GTC limit order."""
    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side=side,
    )
    options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
    return client.create_and_post_order(order_args, options)


def place_market_buy(client, token_id, size, tick_size="0.01", neg_risk=False):
    """Buy at the best available ask price (aggressive limit)."""
    prices = get_best_prices(client, token_id)
    # Place at best ask to fill immediately
    return place_limit_order(client, token_id, prices["best_ask"], size, BUY, tick_size, neg_risk)


def get_open_orders(client):
    """Get all open orders."""
    try:
        return client.get_orders()
    except Exception:
        return []


def cancel_all_orders(client):
    """Cancel all open orders."""
    return client.cancel_all()


def cancel_order(client, order_id):
    """Cancel a specific order."""
    return client.cancel(order_id)
