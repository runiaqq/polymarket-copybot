from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from core.config import settings
from core.db import get_supabase

router = APIRouter(prefix="/admin", tags=["admin"])
bearer = HTTPBearer()


def _verify_admin(creds: HTTPAuthorizationCredentials = Security(bearer)) -> None:
    if creds.credentials != settings.telegram_bot_token:
        raise HTTPException(status_code=403, detail="Forbidden")


class DonorCreate(BaseModel):
    address: str
    label: str | None = None


@router.get("/donors")
async def list_donors(_: None = Depends(_verify_admin)) -> list[dict]:
    sb = get_supabase()
    res = sb.table("donor_wallets").select("*").order("id").execute()
    return res.data


@router.post("/donors", status_code=201)
async def add_donor(body: DonorCreate, _: None = Depends(_verify_admin)) -> dict:
    sb = get_supabase()
    res = sb.table("donor_wallets").insert({
        "address": body.address.lower(),
        "label": body.label,
    }).execute()
    return res.data[0]


@router.delete("/donors/{donor_id}", status_code=204)
async def deactivate_donor(donor_id: int, _: None = Depends(_verify_admin)) -> None:
    sb = get_supabase()
    res = sb.table("donor_wallets").select("id").eq("id", donor_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Donor not found")
    sb.table("donor_wallets").update({"active": False}).eq("id", donor_id).execute()


@router.get("/signals")
async def list_signals(limit: int = 50, _: None = Depends(_verify_admin)) -> list[dict]:
    sb = get_supabase()
    res = (
        sb.table("trade_signals")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data
