# routers/gateway_device_seen.py

from datetime import datetime, timezone
import os
import re

import requests
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import DeviceRegistry, GatewayDeviceSeen

router = APIRouter(prefix="/gateway", tags=["Gateway Device Seen"])

MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")

NODE_RED_BASE_URL = (os.getenv("NODE_RED_BASE_URL") or "").rstrip("/")
NODE_RED_COMMAND_KEY = (os.getenv("NODE_RED_COMMAND_KEY") or "").strip()
DEFAULT_DEVICE_PORT = int(os.getenv("CORELFEX_DEVICE_PORT", "502"))
DEFAULT_UNIT_ID = int(os.getenv("CORELFEX_DEVICE_UNIT_ID", "1"))


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


def notify_nodered_register_device(registry_row, seen_row):
    if not NODE_RED_BASE_URL:
        print("⚠️ NODE_RED_BASE_URL is not configured. Skipping Node-RED notify.")
        return

    device_ip = str(seen_row.device_local_ip or "").strip()
    if not device_ip:
        print("⚠️ device_local_ip is missing. Skipping Node-RED notify.")
        return

    url = f"{NODE_RED_BASE_URL}/coreflex/register-device"

    payload = {
        "action": "register_or_update_poller",
        "device_registry_id": registry_row.id,
        "device_id": registry_row.device_id,
        "device_model": registry_row.device_model,
        "device_mac": registry_row.device_mac,
        "device_ip": device_ip,
        "device_port": DEFAULT_DEVICE_PORT,
        "unit_id": DEFAULT_UNIT_ID,
        "gateway_id": seen_row.gateway_id,
        "gateway_hostname": seen_row.gateway_hostname,
        "gateway_tailscale_ip": seen_row.gateway_tailscale_ip,
        "gateway_interface": seen_row.gateway_interface,
        "neighbor_state": seen_row.neighbor_state,
        "status": seen_row.status,
        "poll_enabled": True,
    }

    headers = {"Content-Type": "application/json"}
    if NODE_RED_COMMAND_KEY:
        headers["x-coreflex-key"] = NODE_RED_COMMAND_KEY

    print("\n========== NODE-RED REGISTER DEVICE ==========")
    print("URL =", url)
    print("PAYLOAD =", payload)
    print("=============================================\n")

    resp = requests.post(url, json=payload, headers=headers, timeout=5)

    print("\n========== NODE-RED RESPONSE ==========")
    print("status_code =", resp.status_code)
    print("response =", resp.text)
    print("======================================\n")

    resp.raise_for_status()


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
    incoming_neighbor_state = str(payload.neighbor_state or "").strip() or None
    incoming_device_local_ip = str(payload.device_local_ip or "").strip() or None

    row = (
        db.query(GatewayDeviceSeen)
        .filter(GatewayDeviceSeen.device_registry_id == registry_row.id)
        .filter(GatewayDeviceSeen.gateway_id == payload.gateway_id)
        .first()
    )

    is_new_row = row is None
    old_device_local_ip = row.device_local_ip if row else None
    old_status = str(row.status or "").strip().lower() if row else None
    old_neighbor_state = str(row.neighbor_state or "").strip() if row else None
    old_gateway_tailscale_ip = row.gateway_tailscale_ip if row else None

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
            device_local_ip=incoming_device_local_ip,
            neighbor_state=incoming_neighbor_state,
            first_seen=incoming_first_seen,
            last_seen=incoming_last_seen,
            status=incoming_status,
            raw_payload=payload.model_dump(mode="json"),
        )
        db.add(row)
    else:
        row.device_id = registry_row.device_id
        row.device_model = registry_row.device_model
        row.device_mac = registry_row.device_mac

        row.gateway_hostname = str(payload.gateway_hostname or "").strip() or None
        row.gateway_tailscale_ip = str(payload.gateway_tailscale_ip).strip()
        row.gateway_interface = str(payload.gateway_interface or "").strip() or None
        row.device_local_ip = incoming_device_local_ip
        row.neighbor_state = incoming_neighbor_state
        row.last_seen = incoming_last_seen
        row.status = incoming_status
        row.raw_payload = payload.model_dump(mode="json")

        # keep earliest first_seen
        if not row.first_seen:
            row.first_seen = incoming_first_seen

        if hasattr(row, "updated_at"):
            row.updated_at = datetime.now(timezone.utc)

    new_device_local_ip = str(row.device_local_ip or "").strip() or None
    new_status = str(row.status or "").strip().lower()
    new_neighbor_state = str(row.neighbor_state or "").strip() or None
    new_gateway_tailscale_ip = str(row.gateway_tailscale_ip or "").strip() or None

    should_notify_nodered = (
        is_new_row
        or old_device_local_ip != new_device_local_ip
        or old_status != new_status
        or old_neighbor_state != new_neighbor_state
        or old_gateway_tailscale_ip != new_gateway_tailscale_ip
    )

    db.commit()
    db.refresh(row)

    if should_notify_nodered:
        try:
            notify_nodered_register_device(registry_row, row)
        except Exception as e:
            print(f"❌ Failed to notify Node-RED: {e}")

    return {
        "ok": True,
        "status": "accepted",
        "device_registry_id": registry_row.id,
        "device_id": registry_row.device_id,
        "device_model": registry_row.device_model,
        "device_mac": registry_row.device_mac,
        "gateway_id": row.gateway_id,
        "last_seen": row.last_seen.isoformat() if row.last_seen else None,
        "nodered_notified": should_notify_nodered,
    }