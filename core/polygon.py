"""
Polygon network utilities: USDC balance, MATIC balance, USDC transfer.
Uses Alchemy RPC via web3.py.
"""

import structlog
from web3 import Web3

from core.config import settings
from core.wallet import decrypt_key

log = structlog.get_logger(__name__)

# Polygon token contracts
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # Circle native USDC
USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (wrappable into pUSD)
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # Polymarket USD (V2 collateral)
COLLATERAL_ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"   # wrap USDC.e -> pUSD
COLLATERAL_OFFRAMP = "0x2957922Eb93258b93368531d39fAcCA3B4dC5854"  # unwrap pUSD -> USDC.e
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
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# CollateralOnramp.wrap / CollateralOfframp.unwrap share the same signature.
_RAMP_ABI = [
    {
        "inputs": [
            {"name": "_asset", "type": "address"},
            {"name": "_to", "type": "address"},
            {"name": "_amount", "type": "uint256"},
        ],
        "name": "wrap",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "_asset", "type": "address"},
            {"name": "_to", "type": "address"},
            {"name": "_amount", "type": "uint256"},
        ],
        "name": "unwrap",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]
MAX_UINT = 2**256 - 1


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
    Returns balances on Polygon. In V2, the tradeable collateral is pUSD.
      {pusd, usdc_e, usdc (native), matic, tradeable_usdc (= pusd), total_usdc}
    `tradeable_usdc` is what the bot can actually trade with (pUSD).
    `usdc_e` is collateral that still needs to be wrapped into pUSD.
    """
    w3 = _w3()
    addr = Web3.to_checksum_address(wallet_address)

    pusd = _usdc_balance(w3, addr, PUSD_ADDRESS)
    usdc_e = _usdc_balance(w3, addr, USDC_BRIDGED)
    usdc = _usdc_balance(w3, addr, USDC_NATIVE)

    try:
        matic_wei = w3.eth.get_balance(addr)
        matic = matic_wei / 10**18
    except Exception:
        matic = 0.0

    return {
        "pusd": pusd,
        "usdc_e": usdc_e,
        "usdc": usdc,
        "matic": matic,
        "tradeable_usdc": pusd,
        "total_usdc": pusd + usdc_e + usdc,
    }


def wrap_usdce_to_pusd(
    private_key_enc: str, wallet_address: str, amount_usdce: float | None = None
) -> str | None:
    """
    Wrap USDC.e into pUSD via the CollateralOnramp (1:1).
    If amount is None, wraps the full USDC.e balance. Returns tx hash, or None if nothing to wrap.
    Requires POL for gas.
    """
    w3 = _w3()
    private_key = decrypt_key(private_key_enc)
    addr = Web3.to_checksum_address(wallet_address)
    usdce = w3.eth.contract(address=Web3.to_checksum_address(USDC_BRIDGED), abi=_ERC20_ABI)

    bal_raw = usdce.functions.balanceOf(addr).call()
    want_raw = int(amount_usdce * 10**USDC_DECIMALS) if amount_usdce is not None else bal_raw
    want_raw = min(want_raw, bal_raw)
    if want_raw <= 0:
        return None

    onramp = Web3.to_checksum_address(COLLATERAL_ONRAMP)
    nonce = w3.eth.get_transaction_count(addr)
    gas_price = w3.eth.gas_price

    # Approve the Onramp to pull USDC.e if needed.
    allowance = usdce.functions.allowance(addr, onramp).call()
    if allowance < want_raw:
        approve_tx = usdce.functions.approve(onramp, MAX_UINT).build_transaction({
            "from": addr, "nonce": nonce, "gasPrice": gas_price, "gas": 80_000, "chainId": 137,
        })
        signed = w3.eth.account.sign_transaction(approve_tx, private_key)
        w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(signed.hash, timeout=120)
        nonce += 1

    ramp = w3.eth.contract(address=onramp, abi=_RAMP_ABI)
    wrap_tx = ramp.functions.wrap(
        Web3.to_checksum_address(USDC_BRIDGED), addr, want_raw
    ).build_transaction({
        "from": addr, "nonce": nonce, "gasPrice": gas_price, "gas": 250_000, "chainId": 137,
    })
    signed = w3.eth.account.sign_transaction(wrap_tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    log.info("wrapped_usdce_to_pusd", wallet=wallet_address[:10], amount=want_raw / 10**USDC_DECIMALS)
    return tx_hash.hex()


def unwrap_pusd_to_usdce(
    private_key_enc: str, wallet_address: str, amount_pusd: float
) -> str | None:
    """Unwrap pUSD back into USDC.e via the CollateralOfframp. Returns tx hash."""
    w3 = _w3()
    private_key = decrypt_key(private_key_enc)
    addr = Web3.to_checksum_address(wallet_address)
    pusd = w3.eth.contract(address=Web3.to_checksum_address(PUSD_ADDRESS), abi=_ERC20_ABI)

    bal_raw = pusd.functions.balanceOf(addr).call()
    want_raw = min(int(amount_pusd * 10**USDC_DECIMALS), bal_raw)
    if want_raw <= 0:
        return None

    offramp = Web3.to_checksum_address(COLLATERAL_OFFRAMP)
    nonce = w3.eth.get_transaction_count(addr)
    gas_price = w3.eth.gas_price

    allowance = pusd.functions.allowance(addr, offramp).call()
    if allowance < want_raw:
        approve_tx = pusd.functions.approve(offramp, MAX_UINT).build_transaction({
            "from": addr, "nonce": nonce, "gasPrice": gas_price, "gas": 80_000, "chainId": 137,
        })
        signed = w3.eth.account.sign_transaction(approve_tx, private_key)
        w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(signed.hash, timeout=120)
        nonce += 1

    ramp = w3.eth.contract(address=offramp, abi=_RAMP_ABI)
    unwrap_tx = ramp.functions.unwrap(
        Web3.to_checksum_address(USDC_BRIDGED), addr, want_raw
    ).build_transaction({
        "from": addr, "nonce": nonce, "gasPrice": gas_price, "gas": 250_000, "chainId": 137,
    })
    signed = w3.eth.account.sign_transaction(unwrap_tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    log.info("unwrapped_pusd_to_usdce", wallet=wallet_address[:10], amount=want_raw / 10**USDC_DECIMALS)
    return tx_hash.hex()


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
