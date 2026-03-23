"""Check wallet balances on Polygon."""
import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

WALLET = os.getenv("WALLET_ADDRESS")
FUNDER = os.getenv("FUNDER_ADDRESS")

# Polygon token addresses
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

POLYGONSCAN_API = "https://api.polygonscan.com/api"


def get_pol_balance(address):
    r = requests.get(POLYGONSCAN_API, params={
        "module": "account", "action": "balance",
        "address": address, "tag": "latest"
    })
    data = r.json()
    if data["status"] == "1":
        return int(data["result"]) / 1e18
    return 0


def get_token_balance(address, contract):
    r = requests.get(POLYGONSCAN_API, params={
        "module": "account", "action": "tokenbalance",
        "contractaddress": contract,
        "address": address, "tag": "latest"
    })
    data = r.json()
    if data["status"] == "1":
        return int(data["result"]) / 1e6
    return 0


def main():
    print("=== Bot Wallet ===")
    print(f"Address: {WALLET}")
    print(f"POL:     {get_pol_balance(WALLET):.6f}")
    print(f"USDC:    {get_token_balance(WALLET, USDC_NATIVE):.6f}")
    print(f"USDC.e:  {get_token_balance(WALLET, USDC_E):.6f}")

    print()
    print("=== Funder Wallet ===")
    print(f"Address: {FUNDER}")
    print(f"POL:     {get_pol_balance(FUNDER):.6f}")
    print(f"USDC:    {get_token_balance(FUNDER, USDC_NATIVE):.6f}")
    print(f"USDC.e:  {get_token_balance(FUNDER, USDC_E):.6f}")


if __name__ == "__main__":
    main()
