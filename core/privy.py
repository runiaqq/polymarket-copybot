"""
Privy server-side wallet management.

Docs: https://docs.privy.io/guide/server/wallets
"""

import httpx
import structlog

from core.config import settings

log = structlog.get_logger(__name__)

PRIVY_BASE_URL = "https://auth.privy.io/api/v1"


class PrivyClient:
    def __init__(self) -> None:
        self._auth = (settings.privy_app_id, settings.privy_app_secret)
        self._headers = {
            "privy-app-id": settings.privy_app_id,
            "Content-Type": "application/json",
        }

    async def create_wallet(self, telegram_id: int) -> dict:
        """
        Create an embedded wallet for a user identified by their Telegram ID.
        Returns {"user_id": str, "wallet_address": str}.
        """
        async with httpx.AsyncClient() as client:
            # 1. Create (or find) a Privy user linked to this Telegram ID
            resp = await client.post(
                f"{PRIVY_BASE_URL}/users",
                auth=self._auth,
                headers=self._headers,
                json={
                    "linked_accounts": [
                        {
                            "type": "custom_auth",
                            "custom_user_id": f"tg:{telegram_id}",
                        }
                    ]
                },
            )
            resp.raise_for_status()
            user_data = resp.json()
            privy_user_id: str = user_data["id"]

            # 2. Create an embedded wallet for that user
            wallet_resp = await client.post(
                f"{PRIVY_BASE_URL}/wallets",
                auth=self._auth,
                headers=self._headers,
                json={
                    "user_id": privy_user_id,
                    "chain_type": "ethereum",  # EVM — covers Polygon
                },
            )
            wallet_resp.raise_for_status()
            wallet_data = wallet_resp.json()
            wallet_address: str = wallet_data["address"]

            log.info("privy_wallet_created", telegram_id=telegram_id, address=wallet_address)
            return {"privy_user_id": privy_user_id, "wallet_address": wallet_address}

    async def sign_and_send_transaction(
        self,
        privy_user_id: str,
        wallet_address: str,
        tx: dict,
    ) -> str:
        """
        Ask Privy to sign and broadcast a pre-built EVM transaction.
        Returns the tx hash.

        tx format: {"to": str, "data": str, "value": "0x0", "chainId": 137}
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{PRIVY_BASE_URL}/wallets/{wallet_address}/rpc",
                auth=self._auth,
                headers=self._headers,
                json={
                    "method": "eth_sendTransaction",
                    "params": {"transaction": tx},
                    "caip2": f"eip155:{settings.polymarket_chain_id}",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            tx_hash: str = data["data"]["hash"]
            log.info("tx_sent", wallet=wallet_address, tx_hash=tx_hash)
            return tx_hash


privy_client = PrivyClient()
