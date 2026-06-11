"""
Per-user wallet generation and private key encryption.
Keys are stored AES-encrypted (Fernet) in Supabase.
"""

import secrets

import structlog
from cryptography.fernet import Fernet
from eth_account import Account

from core.config import settings

log = structlog.get_logger(__name__)


def _fernet() -> Fernet:
    return Fernet(settings.encryption_key.encode())


def generate_wallet() -> dict:
    """
    Generate a new Ethereum/Polygon wallet.
    Returns {"address": str, "private_key_enc": str}.
    The private key is encrypted before returning — never stored in plaintext.
    """
    raw_key = "0x" + secrets.token_hex(32)
    account = Account.from_key(raw_key)
    encrypted = _fernet().encrypt(raw_key.encode()).decode()

    log.info("wallet_generated", address=account.address)
    return {
        "address": account.address,
        "private_key_enc": encrypted,
    }


def decrypt_key(encrypted: str) -> str:
    """Decrypt a stored private key for signing."""
    return _fernet().decrypt(encrypted.encode()).decode()
