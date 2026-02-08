# routers/zhc1921_devices.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import ZHC1921Device, User

# ✅ IMPORTANT: your project uses auth_utils.get_current_user (see user_profile.py)
from auth_utils import get_current_user

router = APIRouter(prefix="/zhc1921", tags=["ZHC1921 Devices"])


class AddDeviceBody(BaseModel):
    device_id: str


def is_owner(user: User) -> bool:
    return (user.email or "").lower() in {
        "roquemartinez_8@hotmail.com",
    }


def to_row_for_table(r: ZHC1921Device):
    """
    ✅ Shape matches your frontend table columns.
    ✅ Date column MUST be "claimed_at" (when a user added/claimed the device),
       NOT "authorized_at" (when owner created it).
    """
    return {
        "deviceId": r.device_id,

        # ✅ THIS IS THE KEY CHANGE:
        # show date user claimed it (what you want)
        "addedAt": r.claimed_at.isoformat() if r.claimed_at else "—",

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


# =========================================================
# OWNER: list ALL devices (admin/device manager)
# =========================================================
@router.get("/devices")
def list_zhc1921_devices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    rows = db.query(ZHC1921Device).order_by(ZHC1921Device.id.asc()).all()
    return [to_row_for_table(r) for r in rows]


# =========================================================
# OWNER: authorize/register a device into the system
# (this creates the row in zhc1921_devices)
# =========================================================
@router.post("/devices")
def authorize_zhc1921_device(
    body: AddDeviceBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    device_id = (body.device_id or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")

    if not device_id.isdigit():
        raise HTTPException(status_code=400, detail="device_id must be numeric")

    exists = db.query(ZHC1921Device).filter(ZHC1921Device.device_id == device_id).first()
    if exists:
        raise HTTPException(status_code=409, detail="device already exists")

    row = ZHC1921Device(device_id=device_id)
    db.add(row)
    db.commit()
    db.refresh(row)

    return {"ok": True, "device_id": row.device_id}


# =========================================================
# USER: claim a device (the user "adds" it)
# - verifies device exists
# - verifies not already claimed by another user
# - assigns claimed_by_user_id + claimed_by_email
# - sets claimed_at = NOW()
# =========================================================
@router.post("/claim")
def claim_zhc1921_device(
    body: AddDeviceBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    device_id = (body.device_id or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")

    if not device_id.isdigit():
        raise HTTPException(status_code=400, detail="device_id must be numeric")

    row = db.query(ZHC1921Device).filter(ZHC1921Device.device_id == device_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="device_id not found (not authorized yet)")

    # already claimed by someone else
    if row.claimed_by_user_id and row.claimed_by_user_id != current_user.id:
        raise HTTPException(status_code=409, detail="device already claimed by another user")

    # idempotent: if same user claims again, just return OK
    if row.claimed_by_user_id == current_user.id:
        return {"ok": True, "device_id": row.device_id, "claimed": True}

    # claim now
    row.claimed_by_user_id = current_user.id
    row.claimed_by_email = (current_user.email or "").lower()
    # claimed_at is a DateTime column; set to current server time
    from sqlalchemy.sql import func
    row.claimed_at = func.now()

    db.add(row)
    db.commit()
    db.refresh(row)

    return {"ok": True, "device_id": row.device_id, "claimed": True}


# =========================================================
# USER: list MY claimed devices (for the user's "Registered Devices" page)
# =========================================================
@router.get("/my-devices")
def list_my_zhc1921_devices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(ZHC1921Device)
        .filter(ZHC1921Device.claimed_by_user_id == current_user.id)
        .order_by(ZHC1921Device.id.asc())
        .all()
    )
    return [to_row_for_table(r) for r in rows]
