"""
Polygon network utilities: USDC balance, MATIC balance, USDC transfer.
Uses Alchemy RPC via web3.py.
"""

import time

import structlog
from web3 import Web3

from core.config import settings
from core.wallet import decrypt_key

log = structlog.get_logger(__name__)

# Polygon token contracts
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # Circle native USDC (exchanges send this)
USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (wrappable into pUSD)
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # Polymarket USD (V2 collateral)
COLLATERAL_ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"   # wrap USDC.e -> pUSD
COLLATERAL_OFFRAMP = "0x2957922Eb93258b93368531d39fAcCA3B4dC5854"  # unwrap pUSD -> USDC.e

# Uniswap v3 — swap native USDC <-> USDC.e (deepest pool is the 0.01% fee tier).
UNISWAP_V3_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
USDC_POOL_FEE = 100        # 0.01%
SWAP_SLIPPAGE = 0.01       # 1% — generous for a $1/$1 stable pair
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

_SWAP_ROUTER_ABI = [
    {
        "inputs": [{"components": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "recipient", "type": "address"},
            {"name": "deadline", "type": "uint256"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMinimum", "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ], "name": "params", "type": "tuple"}],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
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


def _exec_tx(w3, private_key, addr, fn, gas: int, label: str) -> str:
    """Build, sign, send and CONFIRM a contract call. Raises if it reverts.
    Uses the 'pending' nonce so sequential txs don't collide on RPC lag."""
    tx = fn.build_transaction({
        "from": addr,
        "nonce": w3.eth.get_transaction_count(addr, "pending"),
        "gasPrice": w3.eth.gas_price,
        "gas": gas,
        "chainId": 137,
    })
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.get("status") != 1:
        raise RuntimeError(f"{label}_reverted tx={tx_hash.hex()}")
    return tx_hash.hex()


def _ensure_allowance(w3, token, owner, spender, amount: int, private_key) -> None:
    """Approve `spender` for at least `amount`, then verify the allowance actually took."""
    if token.functions.allowance(owner, spender).call() >= amount:
        return
    _exec_tx(w3, private_key, owner, token.functions.approve(spender, MAX_UINT), 90_000, "approve")
    if token.functions.allowance(owner, spender).call() < amount:
        raise RuntimeError(f"allowance_not_set spender={spender}")


def wrap_usdce_to_pusd(
    private_key_enc: str, wallet_address: str, amount_usdce: float | None = None
) -> str | None:
    """Wrap USDC.e into pUSD via the CollateralOnramp (1:1). Returns tx hash or None."""
    w3 = _w3()
    pk = decrypt_key(private_key_enc)
    addr = Web3.to_checksum_address(wallet_address)
    usdce = w3.eth.contract(address=Web3.to_checksum_address(USDC_BRIDGED), abi=_ERC20_ABI)

    bal = usdce.functions.balanceOf(addr).call()
    want = int(amount_usdce * 10**USDC_DECIMALS) if amount_usdce is not None else bal
    want = min(want, bal)
    if want <= 0:
        return None

    onramp = Web3.to_checksum_address(COLLATERAL_ONRAMP)
    _ensure_allowance(w3, usdce, addr, onramp, want, pk)
    ramp = w3.eth.contract(address=onramp, abi=_RAMP_ABI)
    tx_hash = _exec_tx(
        w3, pk, addr,
        ramp.functions.wrap(Web3.to_checksum_address(USDC_BRIDGED), addr, want),
        300_000, "wrap",
    )
    log.info("wrapped_usdce_to_pusd", wallet=wallet_address[:10], amount=want / 10**USDC_DECIMALS)
    return tx_hash


def unwrap_pusd_to_usdce(
    private_key_enc: str, wallet_address: str, amount_pusd: float
) -> str | None:
    """Unwrap pUSD back into USDC.e via the CollateralOfframp. Returns tx hash or None."""
    w3 = _w3()
    pk = decrypt_key(private_key_enc)
    addr = Web3.to_checksum_address(wallet_address)
    pusd = w3.eth.contract(address=Web3.to_checksum_address(PUSD_ADDRESS), abi=_ERC20_ABI)

    bal = pusd.functions.balanceOf(addr).call()
    want = min(int(amount_pusd * 10**USDC_DECIMALS), bal)
    if want <= 0:
        return None

    offramp = Web3.to_checksum_address(COLLATERAL_OFFRAMP)
    _ensure_allowance(w3, pusd, addr, offramp, want, pk)
    ramp = w3.eth.contract(address=offramp, abi=_RAMP_ABI)
    tx_hash = _exec_tx(
        w3, pk, addr,
        ramp.functions.unwrap(Web3.to_checksum_address(USDC_BRIDGED), addr, want),
        300_000, "unwrap",
    )
    log.info("unwrapped_pusd_to_usdce", wallet=wallet_address[:10], amount=want / 10**USDC_DECIMALS)
    return tx_hash


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


def _swap_exact_in(
    private_key_enc: str, wallet_address: str, token_in: str, token_out: str,
    amount_raw: int | None = None,
) -> str | None:
    """Swap token_in -> token_out via Uniswap v3 (0.01% pool). Stable 1:1 pair.
    amount_raw None -> swap full token_in balance. Returns tx hash or None."""
    w3 = _w3()
    pk = decrypt_key(private_key_enc)
    addr = Web3.to_checksum_address(wallet_address)
    tin = w3.eth.contract(address=Web3.to_checksum_address(token_in), abi=_ERC20_ABI)

    bal = tin.functions.balanceOf(addr).call()
    amt = bal if amount_raw is None else min(amount_raw, bal)
    if amt <= 0:
        return None

    router = Web3.to_checksum_address(UNISWAP_V3_ROUTER)
    _ensure_allowance(w3, tin, addr, router, amt, pk)

    min_out = int(amt * (1 - SWAP_SLIPPAGE))
    params = (
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        USDC_POOL_FEE,
        addr,
        int(time.time()) + 600,
        amt,
        min_out,
        0,
    )
    router_c = w3.eth.contract(address=router, abi=_SWAP_ROUTER_ABI)
    tx_hash = _exec_tx(w3, pk, addr, router_c.functions.exactInputSingle(params), 300_000, "swap")
    log.info("swap_done", token_in=token_in[:8], token_out=token_out[:8],
             amount=amt / 10**USDC_DECIMALS)
    return tx_hash


def swap_usdc_to_usdce(private_key_enc: str, wallet_address: str,
                       amount_usdc: float | None = None) -> str | None:
    raw = int(amount_usdc * 10**USDC_DECIMALS) if amount_usdc is not None else None
    return _swap_exact_in(private_key_enc, wallet_address, USDC_NATIVE, USDC_BRIDGED, raw)


def swap_usdce_to_usdc(private_key_enc: str, wallet_address: str,
                       amount_usdce: float) -> str | None:
    raw = int(amount_usdce * 10**USDC_DECIMALS)
    return _swap_exact_in(private_key_enc, wallet_address, USDC_BRIDGED, USDC_NATIVE, raw)


def convert_to_pusd(private_key_enc: str, wallet_address: str) -> float:
    """
    Full deposit conversion: native USDC -> USDC.e -> pUSD.
    Returns approximate amount (USDC) converted into pUSD. Requires POL for gas.
    """
    b = get_balances(wallet_address)
    if b.get("usdc", 0) >= 0.5:
        swap_usdc_to_usdce(private_key_enc, wallet_address)  # swap full native balance

    b2 = get_balances(wallet_address)
    converted = 0.0
    if b2.get("usdc_e", 0) >= 0.5:
        converted = b2["usdc_e"]
        wrap_usdce_to_pusd(private_key_enc, wallet_address)
    return converted


def is_valid_address(address: str) -> bool:
    """Check if string is a valid Ethereum/Polygon address."""
    try:
        Web3.to_checksum_address(address)
        return address.startswith("0x") and len(address) == 42
    except Exception:
        return False
