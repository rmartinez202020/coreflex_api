# routers/control_bindings.py

import os
import uuid
import requests

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field

from database import get_db, SessionLocal
from auth_utils import get_current_user  # ✅ FIX (was auth_routes)
from models import ControlBinding, ZHC1921Device, ControlActionLock

router = APIRouter(prefix="/control-bindings", tags=["Control Bindings"])

ALLOWED_FIELDS = {"do1", "do2", "do3", "do4"}
ALLOWED_TYPES = {"toggle", "push_no", "push_nc"}

# ✅ Frontend uses this to hold "Control Action in Progress" locally
ACTUATION_HOLD_MS = int(os.getenv("ACTUATION_HOLD_MS", "10000"))

# ✅ Node-RED endpoint that will perform the actual DO write
NODE_RED_DO_WRITE_URL = os.getenv(
  "NODE_RED_DO_WRITE_URL",
  "http://98.90.225.131:1880/coreflex/command",
).strip()

# ✅ Optional shared-key protection for backend -> Node-RED commands
NODE_RED_COMMAND_KEY = os.getenv(
  "NODE_RED_COMMAND_KEY",
  "CFX_k29sLx92Jd8slQp4NzT7MartinezVx93LwQa2",
).strip()


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


def _post_to_node_red_wait(
  url: str,
  payload: dict,
  headers: dict,
  timeout_sec: float = 3.5,
):
  """
  Send DO write to Node-RED and WAIT briefly for an ACK response.
  - Never blocks forever (separate connect/read timeouts)
  - If Node-RED is slow, returns pending=True (frontend can confirm via DO polling)
  """
  try:
    connect_t = min(1.5, max(0.5, timeout_sec / 2))
    read_t = max(0.5, timeout_sec - connect_t)

    r = requests.post(url, json=payload, headers=headers, timeout=(connect_t, read_t))

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


# ===============================
# 🔒 Lock-table helpers (NO DB connection held during Node-RED)
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
# 📦 Request Schemas
# ===============================
class ControlBindRequest(BaseModel):
  dashboardId: str = Field(..., min_length=1)
  widgetId: str = Field(..., min_length=1)
  widgetType: str = Field(..., min_length=1)  # toggle | push_no | push_nc
  title: str | None = None

  deviceId: str = Field(..., min_length=1)
  field: str = Field(..., min_length=2)  # do1..do4


class ControlWriteRequest(BaseModel):
  dashboardId: str = Field(..., min_length=1)
  widgetId: str = Field(..., min_length=1)
  value01: int = Field(..., ge=0, le=1)  # 0 or 1


# ===============================
# 🔒 Bind Control to DO
# ===============================
@router.post("/bind")
def bind_control(
  req: ControlBindRequest,
  db: Session = Depends(get_db),
  user=Depends(get_current_user),
):
  dashboard_id = req.dashboardId.strip()
  widget_id = req.widgetId.strip()
  widget_type = req.widgetType.strip().lower()
  device_id = req.deviceId.strip()
  field = req.field.strip().lower()

  if widget_type not in ALLOWED_TYPES:
    raise HTTPException(status_code=400, detail="Invalid widgetType")

  if field not in ALLOWED_FIELDS:
    raise HTTPException(status_code=400, detail="Invalid DO field")

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

  row.widget_type = widget_type
  row.title = (req.title or "").strip() or None
  row.bind_device_id = device_id
  row.bind_field = field

  try:
    db.commit()
  except IntegrityError:
    db.rollback()

    # Find who is using it
    used = (
      db.query(ControlBinding)
      .filter(
        ControlBinding.user_id == user.id,
        ControlBinding.dashboard_id == dashboard_id,
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
      },
    )

  return {"ok": True}


# ===============================
# 📡 Get Used DOs for Dashboard+Device
# ===============================
@router.get("/used")
def get_used_dos(
  dashboardId: str = Query(...),
  deviceId: str = Query(...),
  db: Session = Depends(get_db),
  user=Depends(get_current_user),
):
  dash_id = dashboardId.strip()
  dev_id = deviceId.strip()

  rows = (
    db.query(ControlBinding)
    .filter(
      ControlBinding.user_id == user.id,
      ControlBinding.dashboard_id == dash_id,
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
    }
    for r in rows
    if r.bind_field
  ]


# ===============================
# 🗑️ Delete Control Binding Row
# ===============================
@router.delete("")
def delete_control_binding(
  dashboardId: str = Query(...),
  widgetId: str = Query(...),
  db: Session = Depends(get_db),
  user=Depends(get_current_user),
):
  dash_id = dashboardId.strip()
  wid = widgetId.strip()

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
    return {"ok": True, "deleted": 0}

  db.delete(row)
  db.commit()
  return {"ok": True, "deleted": 1}


# ===============================
# 🕹️ Write DO (PLAY MODE)
# ===============================
@router.post("/write")
def write_control_do(
  req: ControlWriteRequest,
  db: Session = Depends(get_db),
  user=Depends(get_current_user),
):
  dash_id = req.dashboardId.strip()
  wid = req.widgetId.strip()

  # 1) resolve binding
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

  if not device_id:
    raise HTTPException(status_code=400, detail="Binding missing deviceId")
  if field not in ALLOWED_FIELDS:
    raise HTTPException(status_code=400, detail="Invalid bound DO field")

  # 2) tenant isolation: device must still be claimed by user
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

  # 3) do1..do4 -> 1..4
  try:
    do_num = int(field.replace("do", ""))
  except Exception:
    raise HTTPException(status_code=400, detail="Invalid DO field format")

  if do_num not in (1, 2, 3, 4):
    raise HTTPException(status_code=400, detail="DO must be 1..4")

  # 4) value01 -> boolean (matches your Node-RED inject true/false)
  value_bool = True if int(req.value01) == 1 else False

  # 5) forward to node-red (WAIT for response, short)
  if not NODE_RED_DO_WRITE_URL:
    _raise_node_red_not_configured()

  request_id = str(uuid.uuid4())
  lk = _lock_key(device_id, field)
  expires_at = _utc_now() + timedelta(milliseconds=int(ACTUATION_HOLD_MS))

  # 1) Acquire lock (DB only for milliseconds)
  _cleanup_expired_locks(db)

  lock_row = ControlActionLock(
    lock_key=lk,
    device_id=device_id,
    field=field,
    user_id=user.id,
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

  # ✅ IMPORTANT: release DB connection BEFORE calling Node-RED
  try:
    db.close()
  except Exception:
    pass

  payload = {
    "request_id": request_id,
    "device_id": device_id,
    "do": do_num,
    "value": value_bool,
    "dashboard_id": dash_id,
    "widget_id": wid,
    "user_id": user.id,
  }

  try:
    # 2) Call Node-RED (NO DB held)
    result = _post_to_node_red_wait(
      NODE_RED_DO_WRITE_URL,
      payload,
      _node_red_headers(),
      timeout_sec=3.5,
    )

    return {
      "requestId": request_id,
      "deviceId": device_id,
      "field": field,
      "value01": int(req.value01),
      "actuationHoldMs": ACTUATION_HOLD_MS,
      **result,
    }

  finally:
    # 3) Release lock (new short DB session)
    try:
      db2 = SessionLocal()
      try:
        db2.query(ControlActionLock).filter(
          ControlActionLock.lock_key == lk
        ).delete(synchronize_session=False)
        db2.commit()
      finally:
        db2.close()
    except Exception:
      pass