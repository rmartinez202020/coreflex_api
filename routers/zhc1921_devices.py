# routers/zhc1921_devices.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel
from typing import Optional, List
import os

from database import get_db
from models import ZHC1921Device, User
from auth_routes import get_current_user

router = APIRouter(prefix="/zhc1921", tags=["ZHC1921 Devices"])

# ✅ Owner email (same idea as frontend)
PLATFORM_OWNER_EMAIL = os.getenv("PLATFORM_OWNER_EMAIL", "roquemartinez_8@hotmail.com").strip().lower()


# -------------------
# Schemas
# -------------------
class AddDeviceReq(BaseModel):
    device_id: str


class Zhc1921RowOut(BaseModel):
    deviceId: str
    addedAt: Optional[str] = None
    ownedBy: Optional[str] = None
    status: str
    lastSeen: Optional[str] = None

    in1: int
    in2: int
    in3: int
    in4: int

    do1: int
    do2: int
    do3: int
    do4: int

    ai1: Optional[float] = None
    ai2: Optional[float] = None
    ai3: Optional[float] = None
    ai4: Optional[float] = None


# -------------------
# Helpers
# -------------------
def require_owner(current_user: User):
    email = (current_user.email or "").strip().lower()
    if email != PLATFORM_OWNER_EMAIL:
        raise HTTPException(status_code=403, detail="Owner only")


def dt_to_str(dt):
    if not dt:
        return None
    # ISO is best for frontend
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)


def row_to_out(r: ZHC1921Device) -> Zhc1921RowOut:
    return Zhc1921RowOut(
        deviceId=r.device_id,
        addedAt=dt_to_str(r.authorized_at),
        ownedBy=r.claimed_by_email or "—",
        status=r.status or "offline",
        lastSeen=dt_to_str(r.last_seen),

        in1=int(r.di1 or 0),
        in2=int(r.di2 or 0),
        in3=int(r.di3 or 0),
        in4=int(r.di4 or 0),

        do1=int(r.do1 or 0),
        do2=int(r.do2 or 0),
        do3=int(r.do3 or 0),
        do4=int(r.do4 or 0),

        ai1=r.ai1,
        ai2=r.ai2,
        ai3=r.ai3,
        ai4=r.ai4,
    )


# -------------------
# Routes
# -------------------

@router.get("/devices", response_model=List[Zhc1921RowOut])
def list_devices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # ✅ Owner-only (Device Manager)
    require_owner(current_user)

    rows = (
        db.query(ZHC1921Device)
        .order_by(desc(ZHC1921Device.authorized_at))
        .all()
    )
    return [row_to_out(r) for r in rows]


@router.post("/devices", response_model=Zhc1921RowOut)
def add_device(
    payload: AddDeviceReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # ✅ Owner-only
    require_owner(current_user)

    device_id = (payload.device_id or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")

    if not device_id.isdigit():
        raise HTTPException(status_code=400, detail="device_id must be numeric (digits only)")

    exists = (
        db.query(ZHC1921Device)
        .filter(ZHC1921Device.device_id == device_id)
        .first()
    )
    if exists:
        raise HTTPException(status_code=409, detail="That DEVICE ID already exists")

    row = ZHC1921Device(device_id=device_id)
    db.add(row)
    db.commit()
    db.refresh(row)

    return row_to_out(row)
