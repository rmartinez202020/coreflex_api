# routers/toggle_widget.py

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from database import get_db
from auth_routes import get_current_user  # adjust if needed
from models.toggle_widget import ToggleWidget
from models import ZHC1921Device  # adjust import if needed

from pydantic import BaseModel, Field


router = APIRouter(prefix="/toggle-widgets", tags=["Toggle Widgets"])

ALLOWED_FIELDS = {"do1", "do2", "do3", "do4"}


# ===============================
# 📦 Request Schema
# ===============================
class ToggleBindRequest(BaseModel):
    dashboardId: str = Field(..., min_length=1)
    widgetId: str = Field(..., min_length=1)
    title: str | None = None

    deviceId: str = Field(..., min_length=1)
    field: str = Field(..., min_length=2)


# ===============================
# 🔒 Bind Toggle to DO
# ===============================
@router.post("/bind")
def bind_toggle(
    req: ToggleBindRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    dashboard_id = req.dashboardId.strip()
    widget_id = req.widgetId.strip()
    device_id = req.deviceId.strip()
    field = req.field.strip().lower()

    if field not in ALLOWED_FIELDS:
        raise HTTPException(status_code=400, detail="Invalid DO field")

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
        db.query(ToggleWidget)
        .filter(
            ToggleWidget.user_id == user.id,
            ToggleWidget.dashboard_id == dashboard_id,
            ToggleWidget.widget_id == widget_id,
        )
        .first()
    )

    if not row:
        row = ToggleWidget(
            user_id=user.id,
            dashboard_id=dashboard_id,
            widget_id=widget_id,
        )
        db.add(row)

    row.title = (req.title or "").strip() or None
    row.bind_device_id = device_id
    row.bind_field = field

    try:
        db.commit()
    except IntegrityError:
        db.rollback()

        used = (
            db.query(ToggleWidget)
            .filter(
                ToggleWidget.user_id == user.id,
                ToggleWidget.dashboard_id == dashboard_id,
                ToggleWidget.bind_device_id == device_id,
                ToggleWidget.bind_field == field,
                ToggleWidget.widget_id != widget_id,
            )
            .first()
        )

        raise HTTPException(
            status_code=409,
            detail={
                "error": f"{field.upper()} already used",
                "usedByWidgetId": used.widget_id if used else None,
                "usedByTitle": used.title if used else None,
            },
        )

    return {"ok": True}


# ===============================
# 📡 Get Used DOs for Dashboard
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
        db.query(ToggleWidget)
        .filter(
            ToggleWidget.user_id == user.id,
            ToggleWidget.dashboard_id == dash_id,
            ToggleWidget.bind_device_id == dev_id,
            ToggleWidget.bind_field.isnot(None),
        )
        .all()
    )

    return [
        {
            "field": r.bind_field,
            "widgetId": r.widget_id,
            "title": r.title,
        }
        for r in rows
        if r.bind_field
    ]


# ===============================
# 🗑️ Delete Toggle Widget Row
# When a toggle is deleted from the dashboard, delete its DB row
# so its DO can be reused.
#
# DELETE /toggle-widgets?dashboardId=...&widgetId=...
# ===============================
@router.delete("")
def delete_toggle_widget(
    dashboardId: str = Query(...),
    widgetId: str = Query(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    dash_id = dashboardId.strip()
    wid = widgetId.strip()

    row = (
        db.query(ToggleWidget)
        .filter(
            ToggleWidget.user_id == user.id,
            ToggleWidget.dashboard_id == dash_id,
            ToggleWidget.widget_id == wid,
        )
        .first()
    )

    if not row:
        # idempotent delete
        return {"ok": True, "deleted": 0}

    db.delete(row)
    db.commit()
    return {"ok": True, "deleted": 1}