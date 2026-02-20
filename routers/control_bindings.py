# routers/control_bindings.py

import os
import requests

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field

from database import get_db
from auth_utils import get_current_user  # ✅ FIX (was auth_routes)
from models import ControlBinding, ZHC1921Device

router = APIRouter(prefix="/control-bindings", tags=["Control Bindings"])

ALLOWED_FIELDS = {"do1", "do2", "do3", "do4"}
ALLOWED_TYPES = {"toggle", "push_no", "push_nc"}

# ✅ Node-RED endpoint that will perform the actual DO write
# Example: http://98.90.225.131:1880/coreflex/command
NODE_RED_DO_WRITE_URL = os.getenv("NODE_RED_DO_WRITE_URL", "").strip()

# ✅ Optional shared-key protection for backend -> Node-RED commands
# Node-RED should validate header: X-COMMAND-KEY
NODE_RED_COMMAND_KEY = os.getenv("NODE_RED_COMMAND_KEY", "").strip()


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
# Release DO so it can be reused.
#
# DELETE /control-bindings?dashboardId=...&widgetId=...
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
# Frontend sends: dashboardId, widgetId, value01 (0/1)
# Backend resolves deviceId + do# from ControlBinding
# Then forwards to Node-RED as: { device_id, do, value }
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

  # 5) forward to node-red
  if not NODE_RED_DO_WRITE_URL:
    _raise_node_red_not_configured()

  payload = {
    "device_id": device_id,
    "do": do_num,
    "value": value_bool,
    # helpful metadata (optional)
    "dashboard_id": dash_id,
    "widget_id": wid,
    "user_id": user.id,
  }

  try:
    r = requests.post(
      NODE_RED_DO_WRITE_URL,
      json=payload,
      headers=_node_red_headers(),
      timeout=4,
    )
  except Exception as e:
    raise HTTPException(status_code=502, detail=f"Node-RED unreachable: {e}")

  if r.status_code >= 400:
    txt = ""
    try:
      txt = r.text or ""
    except Exception:
      txt = ""
    raise HTTPException(
      status_code=502,
      detail=txt.strip() or f"Node-RED write failed ({r.status_code})",
    )

  # pass-through json if node-red returns it
  try:
    return r.json()
  except Exception:
    return {"ok": True, "deviceId": device_id, "field": field, "value01": req.value01}