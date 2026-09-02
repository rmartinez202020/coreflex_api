# routers/tp4000_devices.py

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from database import get_db
from models import TP4000Device, User
from auth_utils import get_current_user


router = APIRouter(prefix="/tp4000", tags=["TP-4000 Devices"])

OFFLINE_AFTER_SECONDS = int(
    os.getenv("COREFLEX_OFFLINE_AFTER_SECONDS") or "10"
)


class AddDeviceBody(BaseModel):
    device_id: str


class TelemetryBody(BaseModel):
    device_id: str

    status: str | None = "online"
    last_seen: str | None = None

    te101: float | None = None
    te102: float | None = None
    te103: float | None = None
    te104: float | None = None
    te105: float | None = None
    te106: float | None = None
    te107: float | None = None
    te108: float | None = None


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


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        return None


def _as_utc(dt: datetime | None) -> datetime | None:
    if not dt:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def _compute_online_status(last_seen: datetime | None) -> str:
    last_seen_utc = _as_utc(last_seen)

    if not last_seen_utc:
        return "offline"

    now = datetime.now(timezone.utc)
    age_seconds = (now - last_seen_utc).total_seconds()

    return "online" if age_seconds <= OFFLINE_AFTER_SECONDS else "offline"


def to_row_for_table(r: TP4000Device):
    status = _compute_online_status(r.last_seen)

    return {
        "deviceId": r.device_id,
        "addedAt": r.claimed_at.isoformat() if r.claimed_at else "—",
        "ownedBy": r.claimed_by_email or "—",
        "status": status,
        "lastSeen": r.last_seen.isoformat() if r.last_seen else "—",
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
# NODE-RED -> BACKEND TELEMETRY
# POST /tp4000/telemetry
# =========================================================

@router.post("/telemetry")
def ingest_tp4000_telemetry(
    body: TelemetryBody,
    db: Session = Depends(get_db),
    x_telemetry_key: str | None = Header(
        default=None,
        alias="X-TELEMETRY-KEY",
    ),
):
    required_key = (os.getenv("COREFLEX_TELEMETRY_KEY") or "").strip()

    if required_key:
        supplied_key = str(x_telemetry_key or "").strip()

        if supplied_key != required_key:
            raise HTTPException(status_code=401, detail="Invalid telemetry key")

    device_id = _normalize_device_id(body.device_id)

    row = (
        db.query(TP4000Device)
        .filter(TP4000Device.device_id == device_id)
        .first()
    )

    if not row:
        raise HTTPException(
            status_code=404,
            detail="device_id not found (not authorized yet)",
        )

    parsed_last_seen = _parse_iso_dt(body.last_seen)

    if parsed_last_seen is not None:
        row.last_seen = parsed_last_seen
    else:
        row.last_seen = func.now()

    row.status = "online"

    row.te101 = body.te101
    row.te102 = body.te102
    row.te103 = body.te103
    row.te104 = body.te104
    row.te105 = body.te105
    row.te106 = body.te106
    row.te107 = body.te107
    row.te108 = body.te108

    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "ok": True,
        "device_id": device_id,
        "updated": True,
    }


# =========================================================
# OWNER: list ALL devices
# =========================================================

@router.get("/devices")
def list_tp4000_devices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    rows = db.query(TP4000Device).order_by(TP4000Device.id.asc()).all()

    changed = False

    for row in rows:
        before = str(row.status or "").strip().lower()
        after = _compute_online_status(row.last_seen)

        if before != after:
            row.status = after
            db.add(row)
            changed = True

    if changed:
        db.commit()

    return [to_row_for_table(r) for r in rows]


# =========================================================
# OWNER: authorize/register device
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

    row = TP4000Device(
        device_id=device_id,
        status="offline",
    )

    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "ok": True,
        "device_id": row.device_id,
    }


# =========================================================
# OWNER: delete authorized device
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

    return {
        "ok": True,
        "device_id": device_id,
        "deleted": True,
    }


# =========================================================
# USER: claim
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
        raise HTTPException(
            status_code=404,
            detail="device_id not found (not authorized yet)",
        )

    if (
        row.claimed_by_user_id is not None
        and row.claimed_by_user_id != current_user.id
    ):
        raise HTTPException(
            status_code=409,
            detail="device already claimed by another user",
        )

    if row.claimed_by_user_id == current_user.id:
        return {
            "ok": True,
            "device_id": row.device_id,
            "claimed": True,
            "claimed_at": (
                row.claimed_at.isoformat()
                if row.claimed_at
                else None
            ),
        }

    row.claimed_by_user_id = current_user.id
    row.claimed_by_email = (current_user.email or "").lower().strip()
    row.claimed_at = func.now()

    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "ok": True,
        "device_id": row.device_id,
        "claimed": True,
        "claimed_at": (
            row.claimed_at.isoformat()
            if row.claimed_at
            else None
        ),
    }


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

    return {
        "ok": True,
        "device_id": device_id,
        "claimed": False,
    }


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

    changed = False

    for row in rows:
        before = str(row.status or "").strip().lower()
        after = _compute_online_status(row.last_seen)

        if before != after:
            row.status = after
            db.add(row)
            changed = True

    if changed:
        db.commit()

    return [to_row_for_table(r) for r in rows]
