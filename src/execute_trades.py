"""Execute initial trades on Polymarket."""
import os
import json
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions, BalanceAllowanceParams
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

SIGNATURE_TYPE = 2


def get_client():
    client = ClobClient(
        "https://clob.polymarket.com",
        key=os.getenv("PRIVATE_KEY"),
        chain_id=137,
        signature_type=SIGNATURE_TYPE,
        funder=os.getenv("FUNDER_ADDRESS"),
    )
    creds = client.derive_api_key()
    client.set_api_creds(creds)
    return client


def get_balance(client):
    params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=SIGNATURE_TYPE)
    result = client.get_balance_allowance(params)
    return int(result.get("balance", 0)) / 1e6


def place_order(client, token_id, price, size, side=BUY, tick_size="0.01", neg_risk=False):
    """Place a limit order and return the response."""
    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side=side,
    )
    options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
    return client.create_and_post_order(order_args, options)


def log_trade(trade_data):
    """Log trade to file."""
    os.makedirs("data", exist_ok=True)
    trades_file = "data/trades.json"
    try:
        with open(trades_file) as f:
            trades = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        trades = []
    trades.append(trade_data)
    with open(trades_file, "w") as f:
        json.dump(trades, f, indent=2, default=str)


def main():
    client = get_client()
    balance = get_balance(client)
    print(f"Starting balance: ${balance:.2f}\n")

    # Trade plan:
    # Diversify across multiple markets, ~$10-20 per position
    # Using limit orders at the current best bid/ask for quick fills

    trades = [
        # 1. US x Iran ceasefire by April 15 - YES at $0.38
        #    Reasoning: tensions are high, ceasefire talks ongoing, 38% seems fair
        #    Buy YES - if ceasefire happens, pays $1.00 (163% return)
        {
            "market": "US x Iran ceasefire by April 15",
            "token_id": "85191934649046129480174964255278880752271767733539167443243111973456166096127",
            "side": BUY,
            "price": 0.38,
            "size": 40,  # 40 shares * $0.38 = $15.20 cost
            "tick_size": "0.01",
            "neg_risk": False,
        },
        # 2. US forces enter Iran by April 30 - NO at $0.51
        #    Reasoning: full invasion unlikely, market is overpricing risk
        #    Buy NO token instead
        {
            "market": "US forces enter Iran by April 30 - NO",
            "token_id": "76533108781962275310651165149634079251899733930834190485860627580128626747247",  # NO token
            "side": BUY,
            "price": 0.51,
            "size": 29,  # 29 * $0.51 = $14.79
            "tick_size": "0.01",
            "neg_risk": False,
        },
        # 3. Iranian regime fall by April 30 - NO at $0.92
        #    Reasoning: regime change in ~5 weeks extremely unlikely, easy NO
        #    Almost certain to resolve NO = $1.00 (8.7% return in 5 weeks)
        {
            "market": "Iranian regime fall by April 30 - NO",
            "token_id": "45752951190517118746418545365916139233368614665273368123939609626397431866529",  # NO token
            "side": BUY,
            "price": 0.91,
            "size": 22,  # 22 * $0.91 = $20.02
            "tick_size": "0.01",
            "neg_risk": False,
        },
        # 4. Bitcoin dip to $65K in March - NO at $0.73
        #    BTC currently around $84K, dropping to $65K in 8 days = -22%
        #    Very unlikely, buying NO
        {
            "market": "Bitcoin dip to $65K March - NO",
            "token_id": "64087619211543545431479218048939484178441767712621033463416084593776314629222",  # NO token
            "side": BUY,
            "price": 0.73,
            "size": 19,  # 19 * $0.73 = $13.87
            "tick_size": "0.01",
            "neg_risk": False,
        },
        # 5. Iranian regime fall by June 30 - NO at $0.80
        #    Regime change within 3 months very unlikely
        {
            "market": "Iranian regime fall by June 30 - NO",
            "token_id": "95949957895141858444199258452803633110472396604599808168788254125381075552218",  # NO token
            "side": BUY,
            "price": 0.79,
            "size": 18,  # 18 * $0.79 = $14.22
            "tick_size": "0.01",
            "neg_risk": False,
        },
    ]

    total_cost = sum(t["price"] * t["size"] for t in trades)
    print(f"Planned trades: {len(trades)}")
    print(f"Total estimated cost: ${total_cost:.2f}")
    print(f"Remaining after trades: ${balance - total_cost:.2f}\n")

    if total_cost > balance:
        print("ERROR: Not enough balance!")
        return

    # Execute trades
    for i, trade in enumerate(trades):
        print(f"--- Trade {i+1}: {trade['market']} ---")
        print(f"  {trade['side']} {trade['size']} shares @ ${trade['price']}")

        try:
            resp = place_order(
                client,
                trade["token_id"],
                trade["price"],
                trade["size"],
                trade["side"],
                trade["tick_size"],
                trade["neg_risk"],
            )
            print(f"  Result: {resp}")

            log_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": trade["market"],
                "token_id": trade["token_id"],
                "side": trade["side"],
                "price": trade["price"],
                "size": trade["size"],
                "cost": trade["price"] * trade["size"],
                "response": resp,
            })

            time.sleep(1)  # Small delay between orders

        except Exception as e:
            print(f"  ERROR: {e}")

    # Check final balance
    time.sleep(2)
    final_balance = get_balance(client)
    print(f"\nFinal balance: ${final_balance:.2f}")
    print(f"Invested: ${balance - final_balance:.2f}")

    # Check open orders
    orders = client.get_orders()
    print(f"Open orders: {len(orders)}")


if __name__ == "__main__":
    main()
