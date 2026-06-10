"""
Privy server-side wallet management (Server Wallets API).
Docs: https://docs.privy.io/wallets/server-wallets/api-reference
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
        Create a server-controlled wallet.
        Returns {"privy_user_id": str, "wallet_address": str}.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{PRIVY_BASE_URL}/wallets",
                auth=self._auth,
                headers=self._headers,
                json={"chain_type": "ethereum"},
            )
            if not resp.is_success:
                log.error("privy_wallet_create_failed",
                          status=resp.status_code, body=resp.text)
                resp.raise_for_status()

            wallet = resp.json()
            wallet_id: str = wallet["id"]
            wallet_address: str = wallet["address"]

            log.info("privy_wallet_created",
                     telegram_id=telegram_id,
                     wallet_id=wallet_id,
                     address=wallet_address)

            return {
                "privy_user_id": wallet_id,   # store wallet_id in privy_user_id column
                "wallet_address": wallet_address,
            }

    async def sign_and_send_transaction(
        self,
        privy_user_id: str,   # this is actually the wallet_id
        wallet_address: str,
        tx: dict,
    ) -> str:
        """
        Sign and broadcast a pre-built EVM transaction via Privy.
        Returns the tx hash.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{PRIVY_BASE_URL}/wallets/{privy_user_id}/rpc",
                auth=self._auth,
                headers=self._headers,
                json={
                    "method": "eth_sendTransaction",
                    "params": {"transaction": tx},
                    "caip2": f"eip155:{settings.polymarket_chain_id}",
                },
            )
            if not resp.is_success:
                log.error("privy_tx_failed",
                          status=resp.status_code, body=resp.text)
                resp.raise_for_status()

            data = resp.json()
            tx_hash: str = data["data"]["hash"]
            log.info("tx_sent", wallet=wallet_address, tx_hash=tx_hash)
            return tx_hash


privy_client = PrivyClient()
