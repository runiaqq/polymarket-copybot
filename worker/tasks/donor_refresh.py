"""
Nightly task: refresh donor wallet stats from Dune Analytics.
Automatically deactivates underperforming donors.
"""

import httpx
import structlog

from core.config import settings
from worker.celery_app import celery_app

log = structlog.get_logger(__name__)

DUNE_API_URL = "https://api.dune.com/api/v1/query/{query_id}/results"

# Dune query that returns: address, win_rate_30d, roi_30d, total_volume
# Build this query at dune.com filtering Polymarket trades by wallet ROI
DUNE_QUERY_ID = "REPLACE_WITH_YOUR_DUNE_QUERY_ID"


@celery_app.task(name="worker.tasks.refresh_donor_stats", queue="periodic")
def refresh_donor_stats() -> dict:
    import asyncio

    return asyncio.get_event_loop().run_until_complete(_refresh())


async def _refresh() -> dict:
    from datetime import datetime, timezone

    from sqlalchemy import select

    from core.db import AsyncSessionLocal
    from core.db.models import DonorWallet

    if DUNE_QUERY_ID == "REPLACE_WITH_YOUR_DUNE_QUERY_ID":
        log.warning("dune_query_not_configured")
        return {"skipped": True}

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            DUNE_API_URL.format(query_id=DUNE_QUERY_ID),
            headers={"X-Dune-API-Key": settings.alchemy_api_key},  # use DUNE_API_KEY in .env
        )
        resp.raise_for_status()
        rows = resp.json().get("result", {}).get("rows", [])

    updated = 0
    async with AsyncSessionLocal() as session:
        for row in rows:
            address = row.get("address", "").lower()
            result = await session.execute(
                select(DonorWallet).where(DonorWallet.address == address)
            )
            donor = result.scalar_one_or_none()
            if not donor:
                continue

            donor.win_rate_30d = row.get("win_rate_30d")
            donor.roi_30d = row.get("roi_30d")
            donor.total_volume_usdc = row.get("total_volume")
            donor.last_seen_at = datetime.now(timezone.utc)
            updated += 1

        await session.commit()

    log.info("donor_stats_refreshed", updated=updated)
    return {"updated": updated}


@celery_app.task(
    name="worker.tasks.deactivate_underperforming_donors",
    queue="periodic",
)
def deactivate_underperforming_donors() -> dict:
    import asyncio

    return asyncio.get_event_loop().run_until_complete(_deactivate())


async def _deactivate() -> dict:
    from sqlalchemy import select

    from core.db import AsyncSessionLocal
    from core.db.models import DonorWallet

    deactivated = []
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DonorWallet).where(DonorWallet.active == True)  # noqa: E712
        )
        donors = result.scalars().all()

        for donor in donors:
            should_deactivate = (
                (donor.win_rate_30d is not None and donor.win_rate_30d < 0.45)
                or (donor.roi_30d is not None and donor.roi_30d < -0.20)
            )
            if should_deactivate:
                donor.active = False
                deactivated.append(donor.address)
                log.warning(
                    "donor_deactivated",
                    address=donor.address,
                    win_rate=donor.win_rate_30d,
                    roi=donor.roi_30d,
                )

        await session.commit()

    return {"deactivated": deactivated}
