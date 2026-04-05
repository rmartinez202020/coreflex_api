# routers/zhc1661_devices.py
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from datetime import datetime, timezone
import os

from database import get_db
from models import ZHC1661Device, User, TenantUser
from auth_utils import get_current_user

# ✅ NEW: in-memory live telemetry cache (create this util like zhc1921 version)
from utils.zhc1661_live_cache import set_latest, get_latest

router = APIRouter(prefix="/zhc1661", tags=["ZHC1661 Devices"])

# ✅ Offline timeout window (seconds)
OFFLINE_AFTER_SECONDS = int(os.getenv("COREFLEX_OFFLINE_AFTER_SECONDS") or "10")


class AddDeviceBody(BaseModel):
    device_id: str


# ✅ NODE-RED -> BACKEND TELEMETRY INGEST BODY
class TelemetryBody(BaseModel):
    device_id: str

    status: str | None = "online"
    last_seen: str | None = None  # ISO string from Node-RED (optional)

    # ✅ ZHC1661 telemetry
    # Based on your current table/flow:
    # AI-1..AI-4 and AO-1..AO-2
    ai1: float | None = None
    ai2: float | None = None
    ai3: float | None = None
    ai4: float | None = None

    ao1: float | None = None
    ao2: float | None = None


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


def _parse_iso_dt(s: str | None):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
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
    ls = _as_utc(last_seen)
    if not ls:
        return "offline"

    now = datetime.now(timezone.utc)
    age = (now - ls).total_seconds()
    return "online" if age <= OFFLINE_AFTER_SECONDS else "offline"


def to_row_for_table(r: ZHC1661Device):
    """
    Shape matches your frontend table columns (ZHC1661):
    DEVICE ID | Date | User | Status | last seen | AI-1..AI-4 | AO-1..AO-2

    ✅ Status is derived from last_seen age, like zhc1921.
    ✅ Live values prefer in-memory cache first.
    """
    cached = get_latest(r.device_id) or {}

    cache_ls = cached.get("last_seen")
    ls = cache_ls if isinstance(cache_ls, datetime) else r.last_seen

    status = _compute_online_status(ls)
    online = status == "online"

    def analog(name: str):
        v = cached.get(name, getattr(r, name, None))
        return v if v is not None else ""

    return {
        "deviceId": r.device_id,

        # ✅ common fields
        "online": online,
        "is_online": online,
        "status": status,
        "lastSeen": ls.isoformat() if ls else "—",

        "addedAt": r.claimed_at.isoformat() if r.claimed_at else "—",
        "ownedBy": r.claimed_by_email or "—",

        "ai1": analog("ai1"),
        "ai2": analog("ai2"),
        "ai3": analog("ai3"),
        "ai4": analog("ai4"),

        "ao1": analog("ao1"),
        "ao2": analog("ao2"),
    }


def serialize_zhc1661_device_row(r: ZHC1661Device):
    return to_row_for_table(r)


def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        return get_current_user(request=request, db=db)
    except Exception:
        return None


# =========================================================
# ✅ NODE-RED -> BACKEND TELEMETRY INGEST
# POST /zhc1661/telemetry
#
# Optional security:
#   Set env var COREFLEX_TELEMETRY_KEY to a shared secret.
#   Node-RED must send header: X-TELEMETRY-KEY: <secret>
# =========================================================
@router.post("/telemetry")
def ingest_zhc1661_telemetry(
    body: TelemetryBody,
    db: Session = Depends(get_db),
    x_telemetry_key: str | None = Header(default=None, alias="X-TELEMETRY-KEY"),
):
    required_key = (os.getenv("COREFLEX_TELEMETRY_KEY") or "").strip()
    if required_key:
        if (x_telemetry_key or "").strip() != required_key:
            raise HTTPException(status_code=401, detail="Invalid telemetry key")

    device_id = _normalize_device_id(body.device_id)

    row = (
        db.query(ZHC1661Device)
        .filter(ZHC1661Device.device_id == device_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="device_id not found (not authorized yet)",
        )

    parsed = _parse_iso_dt(body.last_seen)
    if parsed is not None:
        row.last_seen = parsed
    else:
        row.last_seen = func.now()

    row.status = (body.status or "online").strip().lower()

    row.ai1 = body.ai1
    row.ai2 = body.ai2
    row.ai3 = body.ai3
    row.ai4 = body.ai4

    row.ao1 = body.ao1
    row.ao2 = body.ao2

    # ✅ NEW: update in-memory cache
    set_latest(
        device_id,
        {
            "device_id": device_id,
            "status": (body.status or "online").strip().lower(),
            "last_seen": parsed,
            "ai1": body.ai1,
            "ai2": body.ai2,
            "ai3": body.ai3,
            "ai4": body.ai4,
            "ao1": body.ao1,
            "ao2": body.ao2,
        },
    )

    db.add(row)
    db.commit()

    return {"ok": True, "device_id": device_id, "updated": True}


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

    exists = (
        db.query(ZHC1661Device)
        .filter(ZHC1661Device.device_id == device_id)
        .first()
    )
    if exists:
        raise HTTPException(status_code=409, detail="device already exists")

    row = ZHC1661Device(device_id=device_id)
    db.add(row)
    db.commit()
    db.refresh(row)

    return {"ok": True, "device_id": row.device_id}


# =========================================================
# OWNER: delete an authorized device row
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

    row = (
        db.query(ZHC1661Device)
        .filter(ZHC1661Device.device_id == device_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="device_id not found")

    if row.claimed_by_user_id is not None:
        raise HTTPException(
            status_code=409,
            detail="device is claimed by a user; unclaim first before deleting",
        )

    db.delete(row)
    db.commit()

    return {"ok": True, "device_id": device_id, "deleted": True}


# =========================================================
# USER: claim
# =========================================================
@router.post("/claim")
def claim_zhc1661_device(
    body: AddDeviceBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    device_id = _normalize_device_id(body.device_id)

    row = (
        db.query(ZHC1661Device)
        .filter(ZHC1661Device.device_id == device_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="device_id not found (not authorized yet)",
        )

    if row.claimed_by_user_id is not None and row.claimed_by_user_id != current_user.id:
        raise HTTPException(
            status_code=409,
            detail="device already claimed by another user",
        )

    if row.claimed_by_user_id == current_user.id:
        return {
            "ok": True,
            "device_id": row.device_id,
            "claimed": True,
            "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
        }

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
# USER: unclaim
# =========================================================
@router.delete("/unclaim/{device_id}")
def unclaim_zhc1661_device(
    device_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    device_id = _normalize_device_id(device_id)

    row = (
        db.query(ZHC1661Device)
        .filter(ZHC1661Device.device_id == device_id)
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
# USER / TENANT: list devices for secure read path
# =========================================================
@router.get("/my-devices")
def list_my_zhc1661_devices(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
    x_tenant_email: str | None = Header(default=None, alias="X-Tenant-Email"),
    x_tenant_access: str | None = Header(default=None, alias="X-Tenant-Access"),
):
    if current_user is not None:
        rows = (
            db.query(ZHC1661Device)
            .filter(ZHC1661Device.claimed_by_user_id == current_user.id)
            .order_by(ZHC1661Device.id.asc())
            .all()
        )
        return [to_row_for_table(r) for r in rows]

    tenant_email_safe = str(x_tenant_email or "").strip().lower()
    if tenant_email_safe:
        tenant_user = (
            db.query(TenantUser)
            .filter(TenantUser.email.ilike(tenant_email_safe))
            .filter(TenantUser.is_active == True)
            .first()
        )
        if not tenant_user:
            raise HTTPException(status_code=404, detail="Tenant user not found")

        owner_user_id = tenant_user.owner_user_id
        if not owner_user_id:
            raise HTTPException(
                status_code=400,
                detail="Tenant user is not linked to an owner user",
            )

        rows = (
            db.query(ZHC1661Device)
            .filter(ZHC1661Device.claimed_by_user_id == owner_user_id)
            .order_by(ZHC1661Device.id.asc())
            .all()
        )
        return [to_row_for_table(r) for r in rows]

    raise HTTPException(status_code=401, detail="Unauthorized")