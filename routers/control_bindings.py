# routers/control_bindings.py

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field

from database import get_db
from auth_routes import get_current_user
from models.control_binding import ControlBinding
from models import ZHC1921Device  # expand later for other models


router = APIRouter(prefix="/control-bindings", tags=["Control Bindings"])

ALLOWED_FIELDS = {"do1", "do2", "do3", "do4"}

# All control widgets allowed to bind DO
ALLOWED_WIDGET_TYPES = {
    "toggle",
    "push_no",
    "push_nc",
}


# ===============================
# 📦 Request Schema
# ===============================
class ControlBindRequest(BaseModel):
    dashboardId: str = Field(..., min_length=1)
    widgetId: str = Field(..., min_length=1)

    widgetType: str = Field(..., min_length=1)

    title: str | None = None

    deviceId: str = Field(..., min_length=1)
    field: str = Field(..., min_length=2)


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
    title = (req.title or "").strip() or None

    device_id = req.deviceId.strip()
    field = req.field.strip().lower()

    if field not in ALLOWED_FIELDS:
        raise HTTPException(status_code=400, detail="Invalid DO field")

    if widget_type not in ALLOWED_WIDGET_TYPES:
        raise HTTPException(status_code=400, detail="Invalid widgetType")

    # ✅ Ensure user owns the device
    device = (
        db.query(ZHC1921Device)
        .filter(
            ZHC1921Device.deviceId == device_id,
            ZHC1921Device.owner_id == user.id,
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
            widget_type=widget_type,
        )
        db.add(row)

    row.widget_type = widget_type
    row.title = title
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
                "usedByWidgetType": used.widget_type if used else None,
            },
        )

    return {"ok": True}


# ===============================
# 📡 Get Used DOs
# ===============================
@router.get("/used")
def get_used(
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
# 🗑️ Delete Binding
# When widget is deleted from dashboard,
# free the DO so it can be reused.
#
# DELETE /control-bindings?dashboardId=...&widgetId=...
# ===============================
@router.delete("")
def delete_binding(
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