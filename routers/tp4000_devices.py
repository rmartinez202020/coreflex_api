# routers/tp4000_devices.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from database import get_db
from models import TP4000Device, User
from auth_utils import get_current_user

router = APIRouter(prefix="/tp4000", tags=["TP-4000 Devices"])


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


def to_row_for_table(r: TP4000Device):
    """
    Shape matches your frontend table columns (TP-4000):
    DEVICE ID | Date | User | Status | last seen | TE-101..TE-108
    """
    return {
        "deviceId": r.device_id,

        # ✅ Match ZHC1921/ZHC1661 behavior: show date USER claimed it (not owner authorized date)
        "addedAt": r.claimed_at.isoformat() if r.claimed_at else "—",

        "ownedBy": r.claimed_by_email or "—",
        "status": r.status or "offline",
        "lastSeen": r.last_seen.isoformat() if r.last_seen else "—",

        # ✅ IMPORTANT: keys match your frontend columns exactly
        "te101": r.te101 if r.te101 is not None else "",
        "te102": r.te102 if r.te102 is not None else "",
        "te103": r.te103 if r.te103 is not None else "",
        "te104": r.te104 if r.te104 is not None else "",
        "te105": r.te105 if r.te105 is not None else "",
        "te106": r.te106 if r.te106 is not None else "",
        "te107": r.te107 if r.te107 is not None else "",
        "te108": r.te108 if r.te108 is not None else "",
    }


# =========================================================
# OWNER: list ALL devices (device manager)
# =========================================================
@router.get("/devices")
def list_tp4000_devices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    rows = db.query(TP4000Device).order_by(TP4000Device.id.asc()).all()
    return [to_row_for_table(r) for r in rows]


# =========================================================
# OWNER: authorize/register a device into the system
# =========================================================
@router.post("/devices")
def authorize_tp4000_device(
    body: AddDeviceBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    device_id = _normalize_device_id(body.device_id)

    exists = (
        db.query(TP4000Device)
        .filter(TP4000Device.device_id == device_id)
        .first()
    )
    if exists:
        raise HTTPException(status_code=409, detail="device already exists")

    row = TP4000Device(device_id=device_id)
    db.add(row)
    db.commit()
    db.refresh(row)

    return {"ok": True, "device_id": row.device_id}


# =========================================================
# OWNER: delete an authorized device row (Device Manager trash)
# =========================================================
@router.delete("/devices/{device_id}")
def delete_tp4000_device(
    device_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    device_id = _normalize_device_id(device_id)

    row = (
        db.query(TP4000Device)
        .filter(TP4000Device.device_id == device_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="device_id not found")

    db.delete(row)
    db.commit()

    return {"ok": True, "device_id": device_id, "deleted": True}


# =========================================================
# USER: claim (optional)
# =========================================================
@router.post("/claim")
def claim_tp4000_device(
    body: AddDeviceBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    device_id = _normalize_device_id(body.device_id)

    row = (
        db.query(TP4000Device)
        .filter(TP4000Device.device_id == device_id)
        .first()
    )
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


# =========================================================
# USER: unclaim
# =========================================================
@router.delete("/unclaim/{device_id}")
def unclaim_tp4000_device(
    device_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    device_id = _normalize_device_id(device_id)

    row = (
        db.query(TP4000Device)
        .filter(TP4000Device.device_id == device_id)
        .first()
    )
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


# =========================================================
# USER: list MY devices
# =========================================================
@router.get("/my-devices")
def list_my_tp4000_devices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(TP4000Device)
        .filter(TP4000Device.claimed_by_user_id == current_user.id)
        .order_by(TP4000Device.id.asc())
        .all()
    )
    return [to_row_for_table(r) for r in rows]
