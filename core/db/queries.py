from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import DonorWallet, SubTier, User


async def get_active_donor_addresses(session: AsyncSession) -> set[str]:
    result = await session.execute(
        select(DonorWallet.address).where(DonorWallet.active == True)  # noqa: E712
    )
    return {row[0].lower() for row in result.all()}


async def get_donor_by_address(session: AsyncSession, address: str) -> DonorWallet | None:
    result = await session.execute(
        select(DonorWallet).where(DonorWallet.address == address.lower())
    )
    return result.scalar_one_or_none()


async def get_active_subscribers(session: AsyncSession) -> list[User]:
    """Return users with an active paid subscription and copy_active=True."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(User).where(
            User.sub_tier != SubTier.FREE,
            User.sub_expires_at > now,
            User.copy_active == True,  # noqa: E712
            User.wallet_address != None,  # noqa: E711
        )
    )
    return list(result.scalars().all())


async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def upsert_user(session: AsyncSession, telegram_id: int) -> User:
    user = await get_user_by_telegram_id(session, telegram_id)
    if not user:
        user = User(telegram_id=telegram_id)
        session.add(user)
        await session.flush()
    return user
