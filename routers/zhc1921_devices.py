# routers/zhc1921_devices.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import ZHC1921Device, User

# ✅ FIX: get_current_user lives in auth_utils.py in your project
from auth_utils import get_current_user

router = APIRouter(prefix="/zhc1921", tags=["ZHC1921 Devices"])


class AddDeviceBody(BaseModel):
    device_id: str


def is_owner(user: User) -> bool:
    # ✅ owner check: simplest rule for now (you can change later)
    # Put YOUR OWNER email here:
    return (user.email or "").lower() in {
        "roquemartinez_8@hotmail.com",
        # add more owner emails if needed
    }


@router.get("/devices")
def list_zhc1921_devices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Owner sees the full ZHC1921 device table.
    Non-owner can be restricted later (for now, owner-only).
    """
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    rows = db.query(ZHC1921Device).order_by(ZHC1921Device.id.asc()).all()

    # return in the exact shape the frontend table expects
    return [
        {
            "deviceId": r.device_id,
            "addedAt": r.authorized_at.isoformat() if r.authorized_at else None,
            "ownedBy": r.claimed_by_email or "—",
            "status": r.status or "offline",
            "lastSeen": r.last_seen.isoformat() if r.last_seen else "—",
            "in1": int(r.di1 or 0),
            "in2": int(r.di2 or 0),
            "in3": int(r.di3 or 0),
            "in4": int(r.di4 or 0),
            "do1": int(r.do1 or 0),
            "do2": int(r.do2 or 0),
            "do3": int(r.do3 or 0),
            "do4": int(r.do4 or 0),
            "ai1": r.ai1 if r.ai1 is not None else "",
            "ai2": r.ai2 if r.ai2 is not None else "",
            "ai3": r.ai3 if r.ai3 is not None else "",
            "ai4": r.ai4 if r.ai4 is not None else "",
        }
        for r in rows
    ]


@router.post("/devices")
def add_zhc1921_device(
    body: AddDeviceBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Owner-only: authorize a ZHC1921 device_id into the DB table.
    This is what your frontend "+ Add Device" should call.
    """
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    device_id = (body.device_id or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")

    # numeric-only (matches your frontend validation)
    if not device_id.isdigit():
        raise HTTPException(status_code=400, detail="device_id must be numeric")

    # prevent duplicates
    exists = (
        db.query(ZHC1921Device)
        .filter(ZHC1921Device.device_id == device_id)
        .first()
    )
    if exists:
        raise HTTPException(status_code=409, detail="device already exists")

    row = ZHC1921Device(device_id=device_id)
    db.add(row)
    db.commit()
    db.refresh(row)

    return {"ok": True, "device_id": row.device_id}
