# routers/control_bindings.py
import os
import uuid
import requests

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field

from database import get_db
from auth_utils import get_current_user  # ✅ FIX (was auth_routes)
from models import (
    ControlBinding,
    ZHC1921Device,
    ControlActionLock,
    GatewayDeviceSeen,  # ✅ NEW
)

router = APIRouter(prefix="/control-bindings", tags=["Control Bindings"])

# ✅ now supports DO + AO
ALLOWED_FIELDS = {"do1", "do2", "do3", "do4", "ao1", "ao2"}
ALLOWED_TYPES = {"toggle", "push_no", "push_nc", "display_output"}

# ✅ Frontend uses this to hold "Control Action in Progress" locally
ACTUATION_HOLD_MS = int(os.getenv("ACTUATION_HOLD_MS", "10000"))

# ✅ Node-RED endpoint that will perform the actual DO/AO write
# This is your MAIN Node-RED bridge endpoint
NODE_RED_DO_WRITE_URL = os.getenv(
    "NODE_RED_DO_WRITE_URL",
    "http://98.90.225.131:1880/coreflex/command",
).strip()

# ✅ Optional shared-key protection for backend -> Node-RED commands
NODE_RED_COMMAND_KEY = os.getenv(
    "NODE_RED_COMMAND_KEY",
    "CFX_k29sLx92Jd8slQp4NzT7MartinezVx93LwQa2",
).strip()


def get_current_user_optional(request: Request, db: Session = Depends(get_db)):
    try:
        return get_current_user(request=request, db=db)
    except Exception:
        return None


def _as_str(v) -> str:
    return (v or "").strip()


def _raise_node_red_not_configured():
    raise HTTPException(
        status_code=500,
        detail="NODE_RED_DO_WRITE_URL not configured on server",
    )


def _node_red_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if NODE_RED_COMMAND_KEY:
        headers["X-COMMAND-KEY"] = NODE_RED_COMMAND_KEY
    return headers


def _safe_json(res):
    try:
        return res.json()
    except Exception:
        return None


def _normalize_tenant_access(value: str) -> str:
    v = str(value or "").strip().lower()
    if not v:
        return ""

    v = v.replace("+", "_").replace("-", "_").replace(" ", "_")
    while "__" in v:
        v = v.replace("__", "_")

    if v in ("read_control", "readandcontrol", "read_and_control"):
        return "read_control"

    return "read_only"


def _check_tenant_control_access(request: Request):
    tenant_email = _as_str(request.headers.get("X-Tenant-Email"))
    tenant_access = _normalize_tenant_access(
        request.headers.get("X-Tenant-Access")
    )

    # ✅ Not a tenant/public request -> allow normal authenticated owner flow
    if not tenant_email:
        return

    # ✅ Tenant/public request -> only read_control can write
    if tenant_access != "read_control":
        raise HTTPException(
            status_code=403,
            detail="This tenant has read-only access.",
        )


def _post_to_node_red_wait(
    url: str,
    payload: dict,
    headers: dict,
    timeout_sec: float = 3.5,
):
    """
    Send control write to Node-RED and WAIT briefly for an ACK response.
    - Never blocks forever (separate connect/read timeouts)
    - If Node-RED is slow, returns pending=True
    """
    try:
        connect_t = min(1.5, max(0.5, timeout_sec / 2))
        read_t = max(0.5, timeout_sec - connect_t)

        r = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=(connect_t, read_t),
        )

        data = _safe_json(r)
        text_body = (r.text or "").strip()

        if 200 <= r.status_code < 300:
            return {
                "ok": True,
                "nodeRedOk": True,
                "pending": False,
                "status": r.status_code,
                "data": data if data is not None else {"raw": text_body},
            }

        return {
            "ok": False,
            "nodeRedOk": False,
            "pending": False,
            "status": r.status_code,
            "error": data if data is not None else (text_body or "Node-RED write failed"),
        }

    except requests.Timeout:
        return {
            "ok": True,
            "nodeRedOk": False,
            "pending": True,
            "status": 504,
            "warning": "Node-RED timeout (pending)",
        }
    except Exception as e:
        return {
            "ok": False,
            "nodeRedOk": False,
            "pending": False,
            "status": 502,
            "error": f"Node-RED unreachable: {repr(e)}",
        }


def _is_do_field(field: str) -> bool:
    return _as_str(field).lower() in {"do1", "do2", "do3", "do4"}


def _is_ao_field(field: str) -> bool:
    return _as_str(field).lower() in {"ao1", "ao2"}


# ===============================
# 🔒 Lock-table helpers
# ===============================
def _utc_now():
    return datetime.now(timezone.utc)


def _lock_key(device_id: str, field: str) -> str:
    return f"dev:{device_id}:{field}".strip().lower()


def _cleanup_expired_locks(db: Session) -> None:
    # Prevent stale locks if server crashes mid-write
    try:
        db.query(ControlActionLock).filter(
            ControlActionLock.expires_at <= _utc_now()
        ).delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()


# ===============================
# 🌐 Gateway-device-seen helpers
# ===============================
def _pick_gateway_seen_row(
    db: Session,
    device_id: str,
) -> Optional[GatewayDeviceSeen]:
    """
    Pick the best current GatewayDeviceSeen row for a device:
    1) Prefer latest ONLINE row
    2) Otherwise latest row of any status
    """
    rows = (
        db.query(GatewayDeviceSeen)
        .filter(GatewayDeviceSeen.device_id == device_id)
        .order_by(GatewayDeviceSeen.last_seen.desc())
        .all()
    )

    if not rows:
        return None

    for row in rows:
        status = _as_str(getattr(row, "status", "")).lower()
        if status == "online":
            return row

    return rows[0]


def _serialize_gateway_seen_row(row: Optional[GatewayDeviceSeen]) -> dict:
    if not row:
        return {
            "gateway_id": None,
            "gateway_hostname": None,
            "gateway_tailscale_ip": None,
            "gateway_interface": None,
            "device_local_ip": None,
            "device_model": None,
            "gateway_status": None,
            "gateway_last_seen": None,
        }

    last_seen = getattr(row, "last_seen", None)
    return {
        "gateway_id": _as_str(getattr(row, "gateway_id", None)) or None,
        "gateway_hostname": _as_str(getattr(row, "gateway_hostname", None)) or None,
        "gateway_tailscale_ip": _as_str(getattr(row, "gateway_tailscale_ip", None)) or None,
        "gateway_interface": _as_str(getattr(row, "gateway_interface", None)) or None,
        "device_local_ip": _as_str(getattr(row, "device_local_ip", None)) or None,
        "device_model": _as_str(getattr(row, "device_model", None)) or None,
        "gateway_status": _as_str(getattr(row, "status", None)).lower() or None,
        "gateway_last_seen": last_seen.isoformat() if last_seen else None,
    }


# ===============================
# 📦 Request Schemas
# ===============================
class ControlBindRequest(BaseModel):
    dashboardId: str = Field(..., min_length=1)
    dashboardName: str | None = None  # ✅ NEW: human-readable dashboard name
    widgetId: str = Field(..., min_length=1)
    widgetType: str = Field(..., min_length=1)  # toggle | push_no | push_nc | display_output
    title: str | None = None

    deviceId: str = Field(..., min_length=1)
    field: str = Field(..., min_length=2)  # do1..do4 | ao1..ao2


class ControlWriteRequest(BaseModel):
    dashboardId: str = Field(..., min_length=1)
    widgetId: str = Field(..., min_length=1)
    field: Optional[str] = None  # optional frontend hint; binding row is source of truth
    value01: Optional[int] = Field(None, ge=0, le=1)  # ✅ DO
    value: Optional[float] = None  # ✅ AO


# ===============================
# 🔒 Bind Control to DO/AO
# ===============================
@router.post("/bind")
def bind_control(
    req: ControlBindRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    dashboard_id = req.dashboardId.strip()
    dashboard_name = (req.dashboardName or "").strip() or None
    widget_id = req.widgetId.strip()
    widget_type = req.widgetType.strip().lower()
    device_id = req.deviceId.strip()
    field = req.field.strip().lower()

    # ✅ Normalize frontend widget names to backend canonical types
    if widget_type == "pushbuttonno":
        widget_type = "push_no"
    elif widget_type == "pushbuttonnc":
        widget_type = "push_nc"

    if widget_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Invalid widgetType")

    if field not in ALLOWED_FIELDS:
        raise HTTPException(status_code=400, detail="Invalid control field")

    # ✅ Ensure user has this device CLAIMED (tenant isolation)
    device = (
        db.query(ZHC1921Device)
        .filter(
            ZHC1921Device.device_id == device_id,
            ZHC1921Device.claimed_by_user_id == user.id,
        )
        .first()
    )
    if not device:
        raise HTTPException(status_code=403, detail="Device not authorized")

    # ✅ GLOBAL uniqueness check across ALL dashboards
    # Same user + same device + same field cannot be used by another widget,
    # no matter which dashboard it is on.
    used = (
        db.query(ControlBinding)
        .filter(
            ControlBinding.user_id == user.id,
            ControlBinding.bind_device_id == device_id,
            ControlBinding.bind_field == field,
            ControlBinding.widget_id != widget_id,
        )
        .first()
    )
    if used:
        raise HTTPException(
            status_code=409,
            detail={
                "error": f"{field.upper()} already used",
                "usedByWidgetId": used.widget_id,
                "usedByTitle": used.title,
                "usedByType": used.widget_type,
                "usedByDashboardId": used.dashboard_id,
                "usedByDashboardName": used.dashboard_name,
            },
        )

    # ✅ Upsert by (user, dashboard, widget)
    row = (
        db.query(ControlBinding)
        .filter(
            ControlBinding.user_id == user.id,
            ControlBinding.dashboard_id == dashboard_id,
            ControlBinding.widget_id == widget_id,
        )
        .first()
    )

    if not row:
        row = ControlBinding(
            user_id=user.id,
            dashboard_id=dashboard_id,
            widget_id=widget_id,
        )
        db.add(row)

    row.dashboard_name = dashboard_name
    row.widget_type = widget_type
    row.title = (req.title or "").strip() or None
    row.bind_device_id = device_id
    row.bind_field = field

    try:
        db.commit()
    except IntegrityError:
        db.rollback()

        used = (
            db.query(ControlBinding)
            .filter(
                ControlBinding.user_id == user.id,
                ControlBinding.bind_device_id == device_id,
                ControlBinding.bind_field == field,
                ControlBinding.widget_id != widget_id,
            )
            .first()
        )

        raise HTTPException(
            status_code=409,
            detail={
                "error": f"{field.upper()} already used",
                "usedByWidgetId": used.widget_id if used else None,
                "usedByTitle": used.title if used else None,
                "usedByType": used.widget_type if used else None,
                "usedByDashboardId": used.dashboard_id if used else None,
                "usedByDashboardName": used.dashboard_name if used else None,
            },
        )

    return {
        "ok": True,
        "dashboardId": row.dashboard_id,
        "dashboardName": row.dashboard_name,
        "field": row.bind_field,
        "widgetType": row.widget_type,
    }


# ===============================
# 📡 Get Used Control Fields for Device (ALL dashboards)
# ===============================
@router.get("/used")
def get_used_dos(
    deviceId: str = Query(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    dev_id = deviceId.strip()

    rows = (
        db.query(ControlBinding)
        .filter(
            ControlBinding.user_id == user.id,
            ControlBinding.bind_device_id == dev_id,
            ControlBinding.bind_field.isnot(None),
        )
        .all()
    )

    return [
        {
            "field": r.bind_field,
            "widgetId": r.widget_id,
            "title": r.title,
            "widgetType": r.widget_type,
            "dashboardId": r.dashboard_id,
            "dashboardName": r.dashboard_name,
        }
        for r in rows
        if r.bind_field
    ]


# ===============================
# 🗑️ Delete Control Binding Row
# ===============================
# ✅ IMPORTANT CHANGE:
# - route is now "/" (explicit) instead of ""
# - dashboardId is OPTIONAL
#   - If dashboardId provided: delete that exact (user + dashboard + widget) row
#   - If dashboardId omitted: delete ANY binding rows for that widgetId for this user
@router.delete("/")
def delete_control_binding(
    widgetId: str = Query(..., min_length=1),
    dashboardId: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    wid = widgetId.strip()
    dash_id = (dashboardId or "").strip() or None

    q = db.query(ControlBinding).filter(
        ControlBinding.user_id == user.id,
        ControlBinding.widget_id == wid,
    )

    if dash_id:
        q = q.filter(ControlBinding.dashboard_id == dash_id)

    rows = q.all()
    if not rows:
        return {"ok": True, "deleted": 0, "dashboardId": dash_id, "widgetId": wid}

    deleted = 0
    try:
        for r in rows:
            db.delete(r)
            deleted += 1
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to delete control binding")

    return {"ok": True, "deleted": deleted, "dashboardId": dash_id, "widgetId": wid}


# ===============================
# 🕹️ Write DO / AO (PLAY MODE)
# ===============================
@router.post("/write")
def write_control_do(
    req: ControlWriteRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_optional),
):
    # 🔒 Tenant permission check
    _check_tenant_control_access(request)

    dash_id = req.dashboardId.strip()
    wid = req.widgetId.strip()
    tenant_email = _as_str(request.headers.get("X-Tenant-Email"))

    # 1) resolve binding
    if tenant_email:
        row = (
            db.query(ControlBinding)
            .filter(
                ControlBinding.dashboard_id == dash_id,
                ControlBinding.widget_id == wid,
            )
            .first()
        )
    else:
        if not user:
            raise HTTPException(status_code=401, detail="Unauthorized")

        row = (
            db.query(ControlBinding)
            .filter(
                ControlBinding.user_id == user.id,
                ControlBinding.dashboard_id == dash_id,
                ControlBinding.widget_id == wid,
            )
            .first()
        )

    if not row:
        raise HTTPException(status_code=404, detail="Control binding not found")

    device_id = _as_str(row.bind_device_id)
    field = _as_str(row.bind_field).lower()
    req_field = _as_str(req.field).lower()

    if not device_id:
        raise HTTPException(status_code=400, detail="Binding missing deviceId")
    if field not in ALLOWED_FIELDS:
        raise HTTPException(status_code=400, detail="Invalid bound control field")

    # ✅ optional frontend field check for consistency
    if req_field and req_field != field:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Write field mismatch",
                "boundField": field,
                "requestedField": req_field,
            },
        )

    # 2) tenant isolation / owner isolation
    if tenant_email:
        device = (
            db.query(ZHC1921Device)
            .filter(ZHC1921Device.device_id == device_id)
            .first()
        )
    else:
        device = (
            db.query(ZHC1921Device)
            .filter(
                ZHC1921Device.device_id == device_id,
                ZHC1921Device.claimed_by_user_id == user.id,
            )
            .first()
        )

    if not device:
        raise HTTPException(status_code=403, detail="Device not authorized")

    # 2.5) ✅ NEW: find latest gateway/device-seen route info by serial/device_id
    gw_seen = _pick_gateway_seen_row(db, device_id)
    if not gw_seen:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "Device route not found",
                "deviceId": device_id,
                "message": "Device has not been seen by any gateway yet.",
            },
        )

    gw_info = _serialize_gateway_seen_row(gw_seen)

    # Strong safety checks for bridge forwarding
    if not gw_info["gateway_tailscale_ip"]:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "Missing gateway Tailscale IP",
                "deviceId": device_id,
                "message": "Gateway route exists but gateway_tailscale_ip is empty.",
            },
        )

    if not gw_info["device_local_ip"]:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "Missing device local IP",
                "deviceId": device_id,
                "message": "Gateway route exists but device_local_ip is empty.",
            },
        )

    # 3) parse field + value by control type
    do_num = None
    ao_num = None
    value_bool = None
    value_num = None

    if _is_do_field(field):
        try:
            do_num = int(field.replace("do", ""))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid DO field format")

        if do_num not in (1, 2, 3, 4):
            raise HTTPException(status_code=400, detail="DO must be 1..4")

        if req.value01 is None:
            raise HTTPException(status_code=400, detail="value01 is required for DO writes")

        value_bool = True if int(req.value01) == 1 else False

    elif _is_ao_field(field):
        try:
            ao_num = int(field.replace("ao", ""))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid AO field format")

        if ao_num not in (1, 2):
            raise HTTPException(status_code=400, detail="AO must be 1..2")

        if req.value is None:
            raise HTTPException(status_code=400, detail="value is required for AO writes")

        try:
            value_num = float(req.value)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid AO numeric value")

    else:
        raise HTTPException(status_code=400, detail="Unsupported control field")

    # 4) forward to node-red bridge (WAIT for response, short)
    if not NODE_RED_DO_WRITE_URL:
        _raise_node_red_not_configured()

    request_id = str(uuid.uuid4())
    lk = _lock_key(device_id, field)
    expires_at = _utc_now() + timedelta(milliseconds=int(ACTUATION_HOLD_MS))

    # ✅ cleanup stale locks first
    _cleanup_expired_locks(db)

    # ✅ Try to acquire lock (DB for milliseconds)
    lock_row = ControlActionLock(
        lock_key=lk,
        device_id=device_id,
        field=field,
        user_id=user.id if user else None,
        expires_at=expires_at,
    )

    try:
        db.add(lock_row)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "error": "Control Action in Progress",
                "deviceId": device_id,
                "field": field,
                "actuationHoldMs": ACTUATION_HOLD_MS,
            },
        )

    # ✅ Backend already knows the route from gateway_device_seen,
    # so include the remaining bridge-forwarding information here.
    payload = {
        "request_id": request_id,
        "device_id": device_id,
        "device_model": gw_info["device_model"] or _as_str(getattr(device, "device_model", None)) or "zhc1921",
        "gateway_id": gw_info["gateway_id"],
        "gateway_hostname": gw_info["gateway_hostname"],
        "gateway_tailscale_ip": gw_info["gateway_tailscale_ip"],
        "gateway_interface": gw_info["gateway_interface"],
        "device_local_ip": gw_info["device_local_ip"],
        "gateway_status": gw_info["gateway_status"],
        "gateway_last_seen": gw_info["gateway_last_seen"],
        "field": field,
        "dashboard_id": dash_id,
        "widget_id": wid,
        "user_id": user.id if user else None,
        "tenant_email": tenant_email or None,
    }

    # ✅ branch by type for Node-RED
    if do_num is not None:
        payload.update(
            {
                "command_type": "do_write",
                "do": do_num,
                "value": value_bool,
                "value01": 1 if value_bool else 0,
            }
        )
    elif ao_num is not None:
        payload.update(
            {
                "command_type": "ao_write",
                "ao": ao_num,
                "value": value_num,
            }
        )

    # ✅ IMPORTANT:
    # We do NOT delete the lock here anymore.
    # The lock remains until expires_at, so backend truly blocks rapid double writes for ACTUATION_HOLD_MS.
    # Expired locks are cleaned up on the next request by _cleanup_expired_locks().
    result = _post_to_node_red_wait(
        NODE_RED_DO_WRITE_URL,
        payload,
        _node_red_headers(),
        timeout_sec=3.5,
    )

    response = {
        "requestId": request_id,
        "deviceId": device_id,
        "field": field,
        "actuationHoldMs": ACTUATION_HOLD_MS,
        "gatewayId": gw_info["gateway_id"],
        "gatewayHostname": gw_info["gateway_hostname"],
        "gatewayTailscaleIp": gw_info["gateway_tailscale_ip"],
        "gatewayInterface": gw_info["gateway_interface"],
        "deviceLocalIp": gw_info["device_local_ip"],
        "deviceModel": gw_info["device_model"] or _as_str(getattr(device, "device_model", None)) or "zhc1921",
        "gatewayStatus": gw_info["gateway_status"],
        "gatewayLastSeen": gw_info["gateway_last_seen"],
        **result,
    }

    if do_num is not None:
        response.update(
            {
                "do": do_num,
                "value01": int(req.value01) if req.value01 is not None else None,
            }
        )
    elif ao_num is not None:
        response.update(
            {
                "ao": ao_num,
                "value": value_num,
            }
        )

    return response