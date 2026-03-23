"""
Fund bot wallet and set up all approvals for Polymarket trading.

Prerequisites: Both funder and bot wallets need a small amount of POL for gas.
Send ~0.5 POL ($0.25) to the funder wallet: 0x1240Ff4f31BF4e872d4700363Cc6EE2D11CCeec2

This script will:
1. Transfer USDC.e from funder to bot wallet
2. Approve USDC.e on Polymarket exchanges
3. Approve Conditional Tokens on Polymarket exchanges
4. Verify everything is ready
"""
import os
import sys
import json
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET = os.getenv("WALLET_ADDRESS")
FUNDER = os.getenv("FUNDER_ADDRESS")
RPC = os.getenv("POLYGON_RPC")

# Contracts
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

ERC20_ABI = json.loads("""[
    {"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[{"name":"_to","type":"address"},{"name":"_value","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"type":"function"}
]""")

ERC1155_ABI = json.loads("""[
    {"constant":false,"inputs":[{"name":"_operator","type":"address"},{"name":"_approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"type":"function"},
    {"constant":true,"inputs":[{"name":"_owner","type":"address"},{"name":"_operator","type":"address"}],"name":"isApprovedForAll","outputs":[{"name":"","type":"bool"}],"type":"function"}
]""")


def get_web3():
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.is_connected(), f"Cannot connect to {RPC}"
    return w3


def check_status(w3):
    """Check all balances and approvals."""
    wallet_cs = Web3.to_checksum_address(WALLET)
    funder_cs = Web3.to_checksum_address(FUNDER)
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)
    ct = w3.eth.contract(address=Web3.to_checksum_address(CONDITIONAL_TOKENS), abi=ERC1155_ABI)

    status = {
        "funder_pol": w3.from_wei(w3.eth.get_balance(funder_cs), 'ether'),
        "funder_usdc_e": usdc.functions.balanceOf(funder_cs).call() / 1e6,
        "bot_pol": w3.from_wei(w3.eth.get_balance(wallet_cs), 'ether'),
        "bot_usdc_e": usdc.functions.balanceOf(wallet_cs).call() / 1e6,
        "allowance_ctf": usdc.functions.allowance(wallet_cs, Web3.to_checksum_address(CTF_EXCHANGE)).call() / 1e6,
        "allowance_neg": usdc.functions.allowance(wallet_cs, Web3.to_checksum_address(NEG_RISK_CTF_EXCHANGE)).call() / 1e6,
        "ct_approved_ctf": ct.functions.isApprovedForAll(wallet_cs, Web3.to_checksum_address(CTF_EXCHANGE)).call(),
        "ct_approved_neg": ct.functions.isApprovedForAll(wallet_cs, Web3.to_checksum_address(NEG_RISK_CTF_EXCHANGE)).call(),
    }

    print("=== Current Status ===")
    print(f"Funder: {float(status['funder_pol']):.6f} POL, {status['funder_usdc_e']:.2f} USDC.e")
    print(f"Bot:    {float(status['bot_pol']):.6f} POL, {status['bot_usdc_e']:.2f} USDC.e")
    print(f"Allowance CTF Exchange:     {status['allowance_ctf']:.2f} USDC.e")
    print(f"Allowance Neg Risk Exchange: {status['allowance_neg']:.2f} USDC.e")
    print(f"CT approved CTF:     {status['ct_approved_ctf']}")
    print(f"CT approved Neg Risk: {status['ct_approved_neg']}")

    return status


def send_tx(w3, account, tx_data):
    """Sign and send a transaction, wait for receipt."""
    signed = account.sign_transaction(tx_data)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  TX: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    status = "OK" if receipt['status'] == 1 else "FAILED"
    gas_used = receipt['gasUsed']
    print(f"  {status} (gas: {gas_used})")
    return receipt


def step1_transfer_usdc(w3, status):
    """Transfer USDC.e from funder to bot wallet."""
    if status["bot_usdc_e"] >= 90:
        print("\n[Step 1] SKIP - Bot already has enough USDC.e")
        return True

    if status["funder_usdc_e"] < 1:
        print("\n[Step 1] FAIL - Funder has no USDC.e")
        return False

    if status["funder_pol"] < 0.0001:
        print("\n[Step 1] FAIL - Funder needs POL for gas")
        print(f"  Send ~0.5 POL to: {FUNDER}")
        return False

    # We need the funder's private key for this
    funder_key = os.getenv("FUNDER_PRIVATE_KEY")
    if not funder_key:
        print("\n[Step 1] SKIP - No FUNDER_PRIVATE_KEY in .env")
        print(f"  Manually transfer {status['funder_usdc_e']:.2f} USDC.e")
        print(f"  From: {FUNDER}")
        print(f"  To:   {WALLET}")
        return False

    print(f"\n[Step 1] Transferring {status['funder_usdc_e']:.2f} USDC.e to bot...")
    funder_cs = Web3.to_checksum_address(FUNDER)
    wallet_cs = Web3.to_checksum_address(WALLET)
    account = w3.eth.account.from_key(funder_key)
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)

    amount = usdc.functions.balanceOf(funder_cs).call()
    nonce = w3.eth.get_transaction_count(funder_cs)
    gas_price = w3.eth.gas_price

    tx = usdc.functions.transfer(wallet_cs, amount).build_transaction({
        'from': funder_cs,
        'nonce': nonce,
        'gas': 65000,
        'gasPrice': gas_price,
        'chainId': 137,
    })
    receipt = send_tx(w3, account, tx)
    return receipt['status'] == 1


def step2_approve_usdc(w3, status):
    """Approve USDC.e on both exchanges."""
    wallet_cs = Web3.to_checksum_address(WALLET)
    account = w3.eth.account.from_key(PRIVATE_KEY)
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)
    max_uint = 2**256 - 1
    nonce = w3.eth.get_transaction_count(wallet_cs)
    gas_price = w3.eth.gas_price

    if status["allowance_ctf"] < 1000:
        print("\n[Step 2a] Approving USDC.e for CTF Exchange...")
        tx = usdc.functions.approve(
            Web3.to_checksum_address(CTF_EXCHANGE), max_uint
        ).build_transaction({
            'from': wallet_cs, 'nonce': nonce, 'gas': 60000,
            'gasPrice': gas_price, 'chainId': 137,
        })
        send_tx(w3, account, tx)
        nonce += 1
    else:
        print("\n[Step 2a] SKIP - CTF Exchange already approved")

    if status["allowance_neg"] < 1000:
        print("[Step 2b] Approving USDC.e for Neg Risk Exchange...")
        tx = usdc.functions.approve(
            Web3.to_checksum_address(NEG_RISK_CTF_EXCHANGE), max_uint
        ).build_transaction({
            'from': wallet_cs, 'nonce': nonce, 'gas': 60000,
            'gasPrice': gas_price, 'chainId': 137,
        })
        send_tx(w3, account, tx)
        nonce += 1
    else:
        print("[Step 2b] SKIP - Neg Risk Exchange already approved")

    return nonce


def step3_approve_ct(w3, status, nonce=None):
    """Approve Conditional Tokens on both exchanges."""
    wallet_cs = Web3.to_checksum_address(WALLET)
    account = w3.eth.account.from_key(PRIVATE_KEY)
    ct = w3.eth.contract(address=Web3.to_checksum_address(CONDITIONAL_TOKENS), abi=ERC1155_ABI)
    if nonce is None:
        nonce = w3.eth.get_transaction_count(wallet_cs)
    gas_price = w3.eth.gas_price

    if not status["ct_approved_ctf"]:
        print("\n[Step 3a] Approving CT for CTF Exchange...")
        tx = ct.functions.setApprovalForAll(
            Web3.to_checksum_address(CTF_EXCHANGE), True
        ).build_transaction({
            'from': wallet_cs, 'nonce': nonce, 'gas': 60000,
            'gasPrice': gas_price, 'chainId': 137,
        })
        send_tx(w3, account, tx)
        nonce += 1
    else:
        print("\n[Step 3a] SKIP - CT already approved for CTF Exchange")

    if not status["ct_approved_neg"]:
        print("[Step 3b] Approving CT for Neg Risk Exchange...")
        tx = ct.functions.setApprovalForAll(
            Web3.to_checksum_address(NEG_RISK_CTF_EXCHANGE), True
        ).build_transaction({
            'from': wallet_cs, 'nonce': nonce, 'gas': 60000,
            'gasPrice': gas_price, 'chainId': 137,
        })
        send_tx(w3, account, tx)
    else:
        print("[Step 3b] SKIP - CT already approved for Neg Risk Exchange")


def main():
    print("=== Polymarket Bot Setup ===\n")
    w3 = get_web3()
    print(f"Connected to Polygon (block: {w3.eth.block_number})\n")

    status = check_status(w3)

    # Check if bot wallet has POL for approvals
    if status["bot_pol"] < 0.0001 and status["bot_usdc_e"] < 1:
        print(f"\n{'='*50}")
        print("ACTION REQUIRED: Send POL for gas fees")
        print(f"{'='*50}")
        print(f"\nOption 1: Send ~0.5 POL to funder wallet:")
        print(f"  {FUNDER}")
        print(f"\nOption 2: Send ~0.5 POL + 100 USDC.e directly to bot wallet:")
        print(f"  {WALLET}")
        print(f"\nPOL is very cheap (~$0.25 for 0.5 POL)")
        print("A single approval tx costs ~0.001 POL")
        return

    # Run setup steps
    step1_transfer_usdc(w3, status)
    status = check_status(w3)  # Refresh

    if status["bot_pol"] >= 0.0001 and status["bot_usdc_e"] > 0:
        nonce = step2_approve_usdc(w3, status)
        step3_approve_ct(w3, status, nonce)
        print("\n=== Final Status ===")
        check_status(w3)
        print("\nReady to trade!")
    else:
        print(f"\nNeed POL on bot wallet for approvals.")
        print(f"Send ~0.1 POL to: {WALLET}")


if __name__ == "__main__":
    main()
