"""
Polygon network utilities: USDC balance, MATIC balance, USDC transfer.
Uses Alchemy RPC via web3.py.
"""

import structlog
from web3 import Web3

from core.config import settings
from core.wallet import decrypt_key

log = structlog.get_logger(__name__)

# Polygon USDC contracts
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # Circle native USDC
USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (Polymarket uses this)
USDC_DECIMALS = 6

_ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


def _w3() -> Web3:
    return Web3(Web3.HTTPProvider(settings.polygon_rpc_url))


def _usdc_balance(w3: Web3, wallet: str, contract_addr: str) -> float:
    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(contract_addr),
            abi=_ERC20_ABI,
        )
        raw = contract.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
        return raw / 10**USDC_DECIMALS
    except Exception:
        return 0.0


def get_balances(wallet_address: str) -> dict:
    """
    Returns {usdc: float, usdc_e: float, matic: float, total_usdc: float}
    """
    w3 = _w3()
    addr = Web3.to_checksum_address(wallet_address)

    usdc = _usdc_balance(w3, addr, USDC_NATIVE)
    usdc_e = _usdc_balance(w3, addr, USDC_BRIDGED)

    try:
        matic_wei = w3.eth.get_balance(addr)
        matic = matic_wei / 10**18
    except Exception:
        matic = 0.0

    total_usdc = usdc + usdc_e
    return {"usdc": usdc, "usdc_e": usdc_e, "matic": matic, "total_usdc": total_usdc}


def transfer_usdc(
    private_key_enc: str,
    wallet_address: str,
    to_address: str,
    amount_usdc: float,
    use_bridged: bool = True,
) -> str:
    """
    Transfer USDC from user wallet to any Polygon address.
    Returns tx_hash hex string.
    """
    w3 = _w3()
    private_key = decrypt_key(private_key_enc)
    contract_addr = USDC_BRIDGED if use_bridged else USDC_NATIVE

    from_addr = Web3.to_checksum_address(wallet_address)
    dest_addr = Web3.to_checksum_address(to_address)
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(contract_addr),
        abi=_ERC20_ABI,
    )

    amount_raw = int(amount_usdc * 10**USDC_DECIMALS)

    # Check balance
    balance_raw = contract.functions.balanceOf(from_addr).call()
    if balance_raw < amount_raw:
        available = balance_raw / 10**USDC_DECIMALS
        raise ValueError(f"Недостаточно средств: доступно ${available:.2f}, запрошено ${amount_usdc:.2f}")

    tx = contract.functions.transfer(dest_addr, amount_raw).build_transaction({
        "from": from_addr,
        "gas": 100_000,
        "gasPrice": w3.eth.gas_price,
        "nonce": w3.eth.get_transaction_count(from_addr),
        "chainId": 137,
    })

    signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    log.info("usdc_transfer", from_addr=wallet_address[:10], to=to_address[:10], amount=amount_usdc)
    return tx_hash.hex()


def is_valid_address(address: str) -> bool:
    """Check if string is a valid Ethereum/Polygon address."""
    try:
        Web3.to_checksum_address(address)
        return address.startswith("0x") and len(address) == 42
    except Exception:
        return False
