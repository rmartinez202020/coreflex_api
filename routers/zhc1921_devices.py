# routers/zhc1921_devices.py
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from datetime import datetime, timezone
import os

from database import get_db
from models import ZHC1921Device, User

# ✅ IMPORTANT: your project uses auth_utils.get_current_user (see user_profile.py)
from auth_utils import get_current_user

# ✅ NEW: in-memory live telemetry cache
from utils.zhc1921_live_cache import set_latest, get_latest

router = APIRouter(prefix="/zhc1921", tags=["ZHC1921 Devices"])

# ✅ Offline timeout window (seconds)
# If the backend hasn't received telemetry within this window, UI will show offline.
OFFLINE_AFTER_SECONDS = int(os.getenv("COREFLEX_OFFLINE_AFTER_SECONDS") or "10")


class AddDeviceBody(BaseModel):
    device_id: str


# ✅ PC-66/67: Telemetry ingest body (Node-RED -> Backend)
class TelemetryBody(BaseModel):
    device_id: str

    status: str | None = "online"
    last_seen: str | None = None  # ISO string from Node-RED (optional)

    # ✅ ZHC1921 has 6 digital inputs
    di1: int | None = 0
    di2: int | None = 0
    di3: int | None = 0
    di4: int | None = 0
    di5: int | None = 0
    di6: int | None = 0

    do1: int | None = 0
    do2: int | None = 0
    do3: int | None = 0
    do4: int | None = 0

    ai1: float | None = None
    ai2: float | None = None
    ai3: float | None = None
    ai4: float | None = None


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


def _coerce_bit(v) -> int:
    # Accept 0/1, True/False, "0"/"1"
    if v is True or v == 1 or v == "1":
        return 1
    return 0


def _parse_iso_dt(s: str | None):
    if not s:
        return None
    try:
        # Handles "2026-02-09T21:58:18.062Z" (Node-RED)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        # Ensure timezone-aware
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _as_utc(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _compute_online_status(last_seen: datetime | None) -> str:
    """
    ✅ Single source of truth:
    - Online if last_seen is within OFFLINE_AFTER_SECONDS
    - Offline otherwise
    """
    ls = _as_utc(last_seen)
    if not ls:
        return "offline"

    now = datetime.now(timezone.utc)
    age = (now - ls).total_seconds()
    return "online" if age <= OFFLINE_AFTER_SECONDS else "offline"


def to_row_for_table(r: ZHC1921Device):
    """
    ✅ Shape matches your frontend table columns.
    ✅ Date column MUST be "claimed_at" (when a user added/claimed the device),
       NOT "authorized_at" (when owner created it).

    ✅ Status is derived from last_seen (truth), not from the stored r.status field.

    ✅ NEW: Live DI/DO/AI prefer in-memory cache (fast, avoids DB telemetry reads on polling).
    """
    cached = get_latest(r.device_id) or {}

    # prefer cache last_seen if present, else DB last_seen
    cache_ls = cached.get("last_seen")
    ls = cache_ls if isinstance(cache_ls, datetime) else r.last_seen

    status = _compute_online_status(ls)

    def bit(name: str, fallback: int = 0) -> int:
        v = cached.get(name, getattr(r, name, fallback))
        return int(v or 0)

    def ai(name: str):
        v = cached.get(name, getattr(r, name, None))
        return v if v is not None else ""

    return {
        "deviceId": r.device_id,

        # ✅ show date user claimed it (what you want)
        "addedAt": r.claimed_at.isoformat() if r.claimed_at else "—",

        "ownedBy": r.claimed_by_email or "—",
        "status": status,
        "lastSeen": ls.isoformat() if ls else "—",

        # ✅ 6 DIs
        "in1": bit("di1"),
        "in2": bit("di2"),
        "in3": bit("di3"),
        "in4": bit("di4"),
        "in5": bit("di5"),
        "in6": bit("di6"),

        "do1": bit("do1"),
        "do2": bit("do2"),
        "do3": bit("do3"),
        "do4": bit("do4"),

        "ai1": ai("ai1"),
        "ai2": ai("ai2"),
        "ai3": ai("ai3"),
        "ai4": ai("ai4"),
    }


# =========================================================
# ✅ PC-66/67: NODE-RED -> BACKEND TELEMETRY INGEST
# POST /zhc1921/telemetry
#
# Optional security:
#   Set env var COREFLEX_TELEMETRY_KEY to a shared secret.
#   Node-RED must send header: X-TELEMETRY-KEY: <secret>
# =========================================================
@router.post("/telemetry")
def ingest_zhc1921_telemetry(
    body: TelemetryBody,
    db: Session = Depends(get_db),
    x_telemetry_key: str | None = Header(default=None, alias="X-TELEMETRY-KEY"),
):
    # ✅ Optional shared-key protection
    required_key = (os.getenv("COREFLEX_TELEMETRY_KEY") or "").strip()
    if required_key:
        if (x_telemetry_key or "").strip() != required_key:
            raise HTTPException(status_code=401, detail="Invalid telemetry key")

    device_id = _normalize_device_id(body.device_id)

    row = (
        db.query(ZHC1921Device)
        .filter(ZHC1921Device.device_id == device_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="device_id not found (not authorized yet)",
        )

    # ✅ last_seen (authoritative signal)
    parsed = _parse_iso_dt(body.last_seen)
    if parsed is not None:
        row.last_seen = parsed
    else:
        # DB-time; still fine because status is derived from last_seen age later
        row.last_seen = func.now()

    # ✅ Store status too (optional). UI will still derive status from last_seen age.
    row.status = (body.status or "online").strip().lower()

    # ✅ DI / DO bits (ZHC1921 DI=6)
    row.di1 = _coerce_bit(body.di1)
    row.di2 = _coerce_bit(body.di2)
    row.di3 = _coerce_bit(body.di3)
    row.di4 = _coerce_bit(body.di4)

    # These columns exist after your ALTER TABLE (di5/di6)
    row.di5 = _coerce_bit(body.di5)
    row.di6 = _coerce_bit(body.di6)

    row.do1 = _coerce_bit(body.do1)
    row.do2 = _coerce_bit(body.do2)
    row.do3 = _coerce_bit(body.do3)
    row.do4 = _coerce_bit(body.do4)

    # ✅ AI values
    row.ai1 = body.ai1
    row.ai2 = body.ai2
    row.ai3 = body.ai3
    row.ai4 = body.ai4

    # ✅ NEW: update in-memory cache (fast read path)
    # We use parsed last_seen when available; if missing, cache last_seen stays None
    # (status on UI still works because DB last_seen exists; next telemetry will populate cache timestamp too).
    set_latest(
        device_id,
        {
            "device_id": device_id,
            "status": (body.status or "online").strip().lower(),
            "last_seen": parsed,
            "di1": _coerce_bit(body.di1),
            "di2": _coerce_bit(body.di2),
            "di3": _coerce_bit(body.di3),
            "di4": _coerce_bit(body.di4),
            "di5": _coerce_bit(body.di5),
            "di6": _coerce_bit(body.di6),
            "do1": _coerce_bit(body.do1),
            "do2": _coerce_bit(body.do2),
            "do3": _coerce_bit(body.do3),
            "do4": _coerce_bit(body.do4),
            "ai1": body.ai1,
            "ai2": body.ai2,
            "ai3": body.ai3,
            "ai4": body.ai4,
        },
    )

    db.add(row)
    db.commit()

    return {"ok": True, "device_id": device_id, "updated": True}


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

    device_id = _normalize_device_id(body.device_id)

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


# =========================================================
# OWNER: delete an authorized device row (Device Manager button)
# - removes the row from backend table
# - safety: only owner can do it
# - safety: you can choose whether to block deletes if claimed
# =========================================================
@router.delete("/devices/{device_id}")
def delete_zhc1921_device(
    device_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    device_id = _normalize_device_id(device_id)

    row = (
        db.query(ZHC1921Device)
        .filter(ZHC1921Device.device_id == device_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="device_id not found")

    # ✅ SAFETY RULE:
    # If you want to allow deleting even when claimed, comment this block.
    if row.claimed_by_user_id is not None:
        raise HTTPException(
            status_code=409,
            detail="device is claimed by a user; unclaim first before deleting",
        )

    db.delete(row)
    db.commit()

    return {"ok": True, "device_id": device_id, "deleted": True}


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
    device_id = _normalize_device_id(body.device_id)

    row = (
        db.query(ZHC1921Device)
        .filter(ZHC1921Device.device_id == device_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="device_id not found (not authorized yet)",
        )

    # already claimed by someone else
    if row.claimed_by_user_id is not None and row.claimed_by_user_id != current_user.id:
        raise HTTPException(
            status_code=409,
            detail="device already claimed by another user",
        )

    # idempotent: if same user claims again, return OK
    if row.claimed_by_user_id == current_user.id:
        return {
            "ok": True,
            "device_id": row.device_id,
            "claimed": True,
            "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
        }

    # claim now
    row.claimed_by_user_id = current_user.id
    row.claimed_by_email = (current_user.email or "").lower().strip()
    row.claimed_at = func.now()

    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "ok": True,
        "device_id": device_id,
        "claimed": True,
        "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
    }


# =========================================================
# USER: unclaim (release) a device from MY account
# - verifies device exists
# - verifies it is claimed by THIS user
# - clears claimed_by_user_id + claimed_by_email + claimed_at
# =========================================================
@router.delete("/unclaim/{device_id}")
def unclaim_zhc1921_device(
    device_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    device_id = _normalize_device_id(device_id)

    row = (
        db.query(ZHC1921Device)
        .filter(ZHC1921Device.device_id == device_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="device_id not found")

    # must be claimed by THIS user
    if row.claimed_by_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You do not own this device")

    # unclaim
    row.claimed_by_user_id = None
    row.claimed_by_email = None
    row.claimed_at = None

    db.add(row)
    db.commit()
    db.refresh(row)

    return {"ok": True, "device_id": device_id, "claimed": False}


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