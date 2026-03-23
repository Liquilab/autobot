"""Setup script: approve USDC.e on Polymarket exchange and prepare for trading."""
import os
import json
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET = os.getenv("WALLET_ADDRESS")
RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")

# Contract addresses
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# ERC20 ABI (minimal - just approve and allowance)
ERC20_ABI = json.loads('[{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},{"constant":true,"inputs":[{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]')

# ERC1155 ABI (minimal - just setApprovalForAll and isApprovedForAll)
ERC1155_ABI = json.loads('[{"constant":false,"inputs":[{"name":"_operator","type":"address"},{"name":"_approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"type":"function"},{"constant":true,"inputs":[{"name":"_owner","type":"address"},{"name":"_operator","type":"address"}],"name":"isApprovedForAll","outputs":[{"name":"","type":"bool"}],"type":"function"}]')


def get_web3():
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.is_connected(), "Failed to connect to Polygon RPC"
    return w3


def check_balances(w3):
    """Check POL and USDC.e balances."""
    pol_balance = w3.eth.get_balance(WALLET)
    usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)
    usdc_balance = usdc_contract.functions.balanceOf(Web3.to_checksum_address(WALLET)).call()

    print(f"POL balance: {w3.from_wei(pol_balance, 'ether'):.6f} POL")
    print(f"USDC.e balance: {usdc_balance / 1e6:.6f} USDC.e")
    return pol_balance, usdc_balance


def check_allowances(w3):
    """Check current allowances for Polymarket contracts."""
    usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)
    wallet_cs = Web3.to_checksum_address(WALLET)

    allowance_ctf = usdc_contract.functions.allowance(
        wallet_cs, Web3.to_checksum_address(CTF_EXCHANGE)
    ).call()
    allowance_neg = usdc_contract.functions.allowance(
        wallet_cs, Web3.to_checksum_address(NEG_RISK_CTF_EXCHANGE)
    ).call()

    print(f"\nUSDC.e allowance for CTF Exchange: {allowance_ctf / 1e6:.2f}")
    print(f"USDC.e allowance for Neg Risk Exchange: {allowance_neg / 1e6:.2f}")

    # Check ERC1155 approval
    ct_contract = w3.eth.contract(address=Web3.to_checksum_address(CONDITIONAL_TOKENS), abi=ERC1155_ABI)
    approved_ctf = ct_contract.functions.isApprovedForAll(
        wallet_cs, Web3.to_checksum_address(CTF_EXCHANGE)
    ).call()
    approved_neg = ct_contract.functions.isApprovedForAll(
        wallet_cs, Web3.to_checksum_address(NEG_RISK_CTF_EXCHANGE)
    ).call()

    print(f"CT approved for CTF Exchange: {approved_ctf}")
    print(f"CT approved for Neg Risk Exchange: {approved_neg}")

    return allowance_ctf, allowance_neg, approved_ctf, approved_neg


def approve_all(w3):
    """Approve USDC.e and CT tokens for both exchanges."""
    account = w3.eth.account.from_key(PRIVATE_KEY)
    wallet_cs = Web3.to_checksum_address(WALLET)
    max_uint = 2**256 - 1
    nonce = w3.eth.get_transaction_count(wallet_cs)

    txs = []

    # 1. Approve USDC.e for CTF Exchange
    usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)
    tx1 = usdc_contract.functions.approve(
        Web3.to_checksum_address(CTF_EXCHANGE), max_uint
    ).build_transaction({
        'from': wallet_cs,
        'nonce': nonce,
        'gas': 60000,
        'gasPrice': w3.to_wei(50, 'gwei'),
        'chainId': 137,
    })
    signed1 = account.sign_transaction(tx1)
    tx_hash1 = w3.eth.send_raw_transaction(signed1.raw_transaction)
    print(f"Approving USDC.e for CTF Exchange... tx: {tx_hash1.hex()}")
    txs.append(tx_hash1)
    nonce += 1

    # 2. Approve USDC.e for Neg Risk Exchange
    tx2 = usdc_contract.functions.approve(
        Web3.to_checksum_address(NEG_RISK_CTF_EXCHANGE), max_uint
    ).build_transaction({
        'from': wallet_cs,
        'nonce': nonce,
        'gas': 60000,
        'gasPrice': w3.to_wei(50, 'gwei'),
        'chainId': 137,
    })
    signed2 = account.sign_transaction(tx2)
    tx_hash2 = w3.eth.send_raw_transaction(signed2.raw_transaction)
    print(f"Approving USDC.e for Neg Risk Exchange... tx: {tx_hash2.hex()}")
    txs.append(tx_hash2)
    nonce += 1

    # 3. Approve CT for CTF Exchange
    ct_contract = w3.eth.contract(address=Web3.to_checksum_address(CONDITIONAL_TOKENS), abi=ERC1155_ABI)
    tx3 = ct_contract.functions.setApprovalForAll(
        Web3.to_checksum_address(CTF_EXCHANGE), True
    ).build_transaction({
        'from': wallet_cs,
        'nonce': nonce,
        'gas': 60000,
        'gasPrice': w3.to_wei(50, 'gwei'),
        'chainId': 137,
    })
    signed3 = account.sign_transaction(tx3)
    tx_hash3 = w3.eth.send_raw_transaction(signed3.raw_transaction)
    print(f"Approving CT for CTF Exchange... tx: {tx_hash3.hex()}")
    txs.append(tx_hash3)
    nonce += 1

    # 4. Approve CT for Neg Risk Exchange
    tx4 = ct_contract.functions.setApprovalForAll(
        Web3.to_checksum_address(NEG_RISK_CTF_EXCHANGE), True
    ).build_transaction({
        'from': wallet_cs,
        'nonce': nonce,
        'gas': 60000,
        'gasPrice': w3.to_wei(50, 'gwei'),
        'chainId': 137,
    })
    signed4 = account.sign_transaction(tx4)
    tx_hash4 = w3.eth.send_raw_transaction(signed4.raw_transaction)
    print(f"Approving CT for Neg Risk Exchange... tx: {tx_hash4.hex()}")
    txs.append(tx_hash4)

    # Wait for all txs
    print("\nWaiting for confirmations...")
    for tx_hash in txs:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        status = "SUCCESS" if receipt['status'] == 1 else "FAILED"
        print(f"  {tx_hash.hex()[:16]}... {status}")

    print("\nAll approvals done!")


def main():
    print("=== Polymarket Trading Setup ===\n")
    w3 = get_web3()
    print(f"Connected to Polygon (block: {w3.eth.block_number})")

    pol_balance, usdc_balance = check_balances(w3)

    if pol_balance == 0:
        print("\n⚠ No POL for gas! Send POL to the bot wallet first.")
        print(f"  Wallet: {WALLET}")
        return

    if usdc_balance == 0:
        print("\n⚠ No USDC.e! Send USDC.e to the bot wallet first.")
        print(f"  Wallet: {WALLET}")
        return

    allowance_ctf, allowance_neg, approved_ctf, approved_neg = check_allowances(w3)

    needs_approval = (
        allowance_ctf < usdc_balance or
        allowance_neg < usdc_balance or
        not approved_ctf or
        not approved_neg
    )

    if needs_approval:
        print("\nSetting up approvals...")
        approve_all(w3)
        check_allowances(w3)
    else:
        print("\nAll approvals already set! Ready to trade.")


if __name__ == "__main__":
    main()
