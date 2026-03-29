# routers/gateway_device_seen.py

from datetime import datetime, timezone
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import DeviceRegistry, GatewayDeviceSeen

router = APIRouter(prefix="/gateway", tags=["Gateway Device Seen"])

MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def normalize_mac(mac: str) -> str:
    s = str(mac or "").strip().lower()
    s = s.replace("-", ":")
    s = re.sub(r"\s+", "", s)
    return s


def as_utc(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class GatewayDeviceSeenIn(BaseModel):
    gateway_id: str = Field(..., min_length=1, max_length=120)
    gateway_hostname: str | None = Field(default=None, max_length=120)
    gateway_tailscale_ip: str = Field(..., min_length=1, max_length=64)
    gateway_interface: str | None = Field(default=None, max_length=50)

    device_mac: str = Field(..., min_length=11, max_length=32)
    device_local_ip: str | None = Field(default=None, max_length=64)
    neighbor_state: str | None = Field(default=None, max_length=32)

    first_seen: datetime | None = None
    last_seen: datetime | None = None
    status: str | None = Field(default="online", max_length=32)
    event_type: str | None = Field(default=None, max_length=40)


@router.post("/device-seen")
def gateway_device_seen(
    payload: GatewayDeviceSeenIn,
    db: Session = Depends(get_db),
):
    clean_mac = normalize_mac(payload.device_mac)
    if not MAC_RE.match(clean_mac):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid MAC format",
        )

    registry_row = (
        db.query(DeviceRegistry)
        .filter(DeviceRegistry.device_mac == clean_mac)
        .first()
    )

    # ✅ only accept/save if MAC exists in device_registry
    if not registry_row:
        return {
            "ok": False,
            "status": "unregistered_device",
            "detail": "MAC not found in device_registry",
        }

    incoming_first_seen = as_utc(payload.first_seen) or datetime.now(timezone.utc)
    incoming_last_seen = as_utc(payload.last_seen) or datetime.now(timezone.utc)
    incoming_status = str(payload.status or "online").strip().lower()

    row = (
        db.query(GatewayDeviceSeen)
        .filter(GatewayDeviceSeen.device_registry_id == registry_row.id)
        .filter(GatewayDeviceSeen.gateway_id == payload.gateway_id)
        .first()
    )

    if not row:
        row = GatewayDeviceSeen(
            device_registry_id=registry_row.id,
            device_id=registry_row.device_id,
            device_model=registry_row.device_model,
            device_mac=registry_row.device_mac,
            gateway_id=str(payload.gateway_id).strip(),
            gateway_hostname=str(payload.gateway_hostname or "").strip() or None,
            gateway_tailscale_ip=str(payload.gateway_tailscale_ip).strip(),
            gateway_interface=str(payload.gateway_interface or "").strip() or None,
            device_local_ip=str(payload.device_local_ip or "").strip() or None,
            neighbor_state=str(payload.neighbor_state or "").strip() or None,
            first_seen=incoming_first_seen,
            last_seen=incoming_last_seen,
            status=incoming_status,
            raw_payload=payload.model_dump(),
        )
        db.add(row)
    else:
        row.device_id = registry_row.device_id
        row.device_model = registry_row.device_model
        row.device_mac = registry_row.device_mac

        row.gateway_hostname = str(payload.gateway_hostname or "").strip() or None
        row.gateway_tailscale_ip = str(payload.gateway_tailscale_ip).strip()
        row.gateway_interface = str(payload.gateway_interface or "").strip() or None
        row.device_local_ip = str(payload.device_local_ip or "").strip() or None
        row.neighbor_state = str(payload.neighbor_state or "").strip() or None
        row.last_seen = incoming_last_seen
        row.status = incoming_status
        row.raw_payload = payload.model_dump()

        # keep earliest first_seen
        if not row.first_seen:
            row.first_seen = incoming_first_seen

        if hasattr(row, "updated_at"):
            row.updated_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(row)

    return {
        "ok": True,
        "status": "accepted",
        "device_registry_id": registry_row.id,
        "device_id": registry_row.device_id,
        "device_model": registry_row.device_model,
        "device_mac": registry_row.device_mac,
        "gateway_id": row.gateway_id,
        "last_seen": row.last_seen.isoformat() if row.last_seen else None,
    }