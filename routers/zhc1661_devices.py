# routers/zhc1661_devices.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from database import get_db
from models import ZHC1661Device, User
from auth_utils import get_current_user

router = APIRouter(prefix="/zhc1661", tags=["ZHC1661 Devices"])


class AddDeviceBody(BaseModel):
    device_id: str


def is_owner(user: User) -> bool:
    return (user.email or "").lower().strip() in {
        "roquemartinez_8@hotmail.com",
    }


def _normalize_device_id(device_id: str) -> str:
    device_id = (device_id or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")
    if not device_id.isdigit():
        raise HTTPException(status_code=400, detail="device_id must be numeric")
    return device_id


def to_row_for_table(r: ZHC1661Device):
    """
    Shape matches your frontend table columns (ZHC1661):
    DEVICE ID | Date | User | Status | last seen | AI-1..AI-4 | AO-1..AO-2
    """
    return {
        "deviceId": r.device_id,

        # For Device Manager (owner list) you can show authorized_at OR claimed_at.
        # If you want the SAME behavior as your ZHC1921 device manager table, keep claimed_at.
        # If you want "date added by owner", use authorized_at instead.
        "addedAt": r.claimed_at.isoformat() if r.claimed_at else (r.authorized_at.isoformat() if r.authorized_at else "—"),

        "ownedBy": r.claimed_by_email or "—",
        "status": r.status or "offline",
        "lastSeen": r.last_seen.isoformat() if r.last_seen else "—",

        "ai1": r.ai1 if r.ai1 is not None else "",
        "ai2": r.ai2 if r.ai2 is not None else "",
        "ai3": r.ai3 if r.ai3 is not None else "",
        "ai4": r.ai4 if r.ai4 is not None else "",

        "ao1": r.ao1 if r.ao1 is not None else "",
        "ao2": r.ao2 if r.ao2 is not None else "",
    }


# =========================================================
# OWNER: list ALL devices (device manager)
# =========================================================
@router.get("/devices")
def list_zhc1661_devices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    rows = db.query(ZHC1661Device).order_by(ZHC1661Device.id.asc()).all()
    return [to_row_for_table(r) for r in rows]


# =========================================================
# OWNER: authorize/register a device into the system
# =========================================================
@router.post("/devices")
def authorize_zhc1661_device(
    body: AddDeviceBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    device_id = _normalize_device_id(body.device_id)

    exists = db.query(ZHC1661Device).filter(ZHC1661Device.device_id == device_id).first()
    if exists:
        raise HTTPException(status_code=409, detail="device already exists")

    row = ZHC1661Device(device_id=device_id)
    db.add(row)
    db.commit()
    db.refresh(row)

    return {"ok": True, "device_id": row.device_id}


# =========================================================
# OWNER: delete an authorized device row (Device Manager trash)
# =========================================================
@router.delete("/devices/{device_id}")
def delete_zhc1661_device(
    device_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    device_id = _normalize_device_id(device_id)

    row = db.query(ZHC1661Device).filter(ZHC1661Device.device_id == device_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="device_id not found")

    db.delete(row)
    db.commit()

    return {"ok": True, "device_id": device_id, "deleted": True}


# =========================================================
# OPTIONAL (recommended): USER claim/unclaim/my-devices
# (so your Register Devices — CF-1600 page can work later)
# =========================================================
@router.post("/claim")
def claim_zhc1661_device(
    body: AddDeviceBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    device_id = _normalize_device_id(body.device_id)

    row = db.query(ZHC1661Device).filter(ZHC1661Device.device_id == device_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="device_id not found (not authorized yet)")

    if row.claimed_by_user_id is not None and row.claimed_by_user_id != current_user.id:
        raise HTTPException(status_code=409, detail="device already claimed by another user")

    if row.claimed_by_user_id == current_user.id:
        return {"ok": True, "device_id": row.device_id, "claimed": True}

    row.claimed_by_user_id = current_user.id
    row.claimed_by_email = (current_user.email or "").lower().strip()
    row.claimed_at = func.now()

    db.add(row)
    db.commit()
    db.refresh(row)

    return {"ok": True, "device_id": row.device_id, "claimed": True}


@router.delete("/unclaim/{device_id}")
def unclaim_zhc1661_device(
    device_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    device_id = _normalize_device_id(device_id)

    row = db.query(ZHC1661Device).filter(ZHC1661Device.device_id == device_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="device_id not found")

    if row.claimed_by_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You do not own this device")

    row.claimed_by_user_id = None
    row.claimed_by_email = None
    row.claimed_at = None

    db.add(row)
    db.commit()
    db.refresh(row)

    return {"ok": True, "device_id": device_id, "claimed": False}


@router.get("/my-devices")
def list_my_zhc1661_devices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(ZHC1661Device)
        .filter(ZHC1661Device.claimed_by_user_id == current_user.id)
        .order_by(ZHC1661Device.id.asc())
        .all()
    )
    return [to_row_for_table(r) for r in rows]
