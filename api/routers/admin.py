"""
Admin REST API — donor management, signal monitoring.
Protected by a simple bearer token (admin only).
"""

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select

from core.config import settings
from core.db import AsyncSessionLocal
from core.db.models import DonorWallet, TradeSignal

router = APIRouter(prefix="/admin", tags=["admin"])
bearer = HTTPBearer()


def _verify_admin(creds: HTTPAuthorizationCredentials = Security(bearer)) -> None:
    if creds.credentials != settings.telegram_bot_token:
        raise HTTPException(status_code=403, detail="Forbidden")


# ── Donors ────────────────────────────────────────────────────────────────────

class DonorCreate(BaseModel):
    address: str
    label: str | None = None


class DonorOut(BaseModel):
    id: int
    address: str
    label: str | None
    win_rate_30d: float | None
    roi_30d: float | None
    active: bool


@router.get("/donors", response_model=list[DonorOut])
async def list_donors(_: None = Depends(_verify_admin)) -> list[DonorOut]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(DonorWallet).order_by(DonorWallet.id))
        donors = result.scalars().all()
    return [DonorOut.model_validate(d, from_attributes=True) for d in donors]


@router.post("/donors", response_model=DonorOut, status_code=201)
async def add_donor(body: DonorCreate, _: None = Depends(_verify_admin)) -> DonorOut:
    async with AsyncSessionLocal() as session:
        donor = DonorWallet(address=body.address.lower(), label=body.label)
        session.add(donor)
        await session.commit()
        await session.refresh(donor)
    return DonorOut.model_validate(donor, from_attributes=True)


@router.delete("/donors/{donor_id}", status_code=204)
async def deactivate_donor(donor_id: int, _: None = Depends(_verify_admin)) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(DonorWallet).where(DonorWallet.id == donor_id))
        donor = result.scalar_one_or_none()
        if not donor:
            raise HTTPException(status_code=404, detail="Donor not found")
        donor.active = False
        await session.commit()


# ── Signals ───────────────────────────────────────────────────────────────────

class SignalOut(BaseModel):
    id: int
    market_id: str
    side: str
    price: float
    size_usdc: float
    ai_score: int | None
    ai_reason: str | None


@router.get("/signals", response_model=list[SignalOut])
async def list_signals(limit: int = 50, _: None = Depends(_verify_admin)) -> list[SignalOut]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TradeSignal).order_by(TradeSignal.created_at.desc()).limit(limit)
        )
        signals = result.scalars().all()
    return [SignalOut.model_validate(s, from_attributes=True) for s in signals]
