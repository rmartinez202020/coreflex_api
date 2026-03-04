# routers/graphic_display_bindings.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from database import get_db
from auth_utils import get_current_user
from models import User, GraphicDisplayBinding

router = APIRouter(prefix="/graphic-display-bindings", tags=["Graphic Display Bindings"])


# =========================
# BODY: UPSERT (Apply button)
# =========================
class UpsertGraphicBindingBody(BaseModel):
    dashboard_id: str = "main"
    widget_id: str

    # binding
    bind_model: str = "zhc1921"
    bind_device_id: str
    bind_field: str = "ai1"

    # display settings (optional but we persist them)
    title: str = "Graphic Display"
    time_unit: str = "seconds"
    window_size: int = 60
    sample_ms: int = 3000
    y_min: float = 0
    y_max: float = 100
    line_color: str = "#0c5ac8"
    graph_style: str = "line"

    # math
    math_formula: str = ""

    # totalizer
    totalizer_enabled: bool = False
    totalizer_unit: str = ""

    # single units
    single_units_enabled: bool = False
    single_unit: str = ""

    # retention
    retention_days: int = 35

    # control
    is_enabled: bool = True


def _clean_text(v, default=""):
    s = str(v or "").strip()
    return s if s else default


@router.post("/upsert")
def upsert_graphic_display_binding(
    body: UpsertGraphicBindingBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    dashboard_id = _clean_text(body.dashboard_id, "main")
    widget_id = _clean_text(body.widget_id)
    bind_model = _clean_text(body.bind_model, "zhc1921").lower()
    bind_device_id = _clean_text(body.bind_device_id)
    bind_field = _clean_text(body.bind_field, "ai1")

    if not widget_id:
        raise HTTPException(status_code=400, detail="widget_id is required")
    if not bind_device_id:
        raise HTTPException(status_code=400, detail="bind_device_id is required")
    if not bind_field:
        raise HTTPException(status_code=400, detail="bind_field is required")

    # keep in a safe range
    sample_ms = int(body.sample_ms or 3000)
    if sample_ms < 1000:
        sample_ms = 1000

    window_size = int(body.window_size or 60)
    if window_size < 5:
        window_size = 5

    retention_days = int(body.retention_days or 35)
    if retention_days < 1:
        retention_days = 1
    if retention_days > 366:
        retention_days = 366

    # ✅ find existing row (unique per user+dashboard+widget)
    row = (
        db.query(GraphicDisplayBinding)
        .filter(
            GraphicDisplayBinding.user_id == current_user.id,
            GraphicDisplayBinding.dashboard_id == dashboard_id,
            GraphicDisplayBinding.widget_id == widget_id,
        )
        .first()
    )

    if not row:
        row = GraphicDisplayBinding(
            user_id=current_user.id,
            dashboard_id=dashboard_id,
            widget_id=widget_id,
            created_at=func.now(),
        )
        db.add(row)

    # ✅ update fields
    row.bind_model = bind_model
    row.bind_device_id = bind_device_id
    row.bind_field = bind_field

    row.title = _clean_text(body.title, "Graphic Display")
    row.time_unit = _clean_text(body.time_unit, "seconds")
    row.window_size = window_size
    row.sample_ms = sample_ms
    row.y_min = float(body.y_min if body.y_min is not None else 0)
    row.y_max = float(body.y_max if body.y_max is not None else 100)
    row.line_color = _clean_text(body.line_color, "#0c5ac8")
    row.graph_style = _clean_text(body.graph_style, "line")

    row.math_formula = str(body.math_formula or "")

    row.totalizer_enabled = bool(body.totalizer_enabled)
    row.totalizer_unit = str(body.totalizer_unit or "")

    row.single_units_enabled = bool(body.single_units_enabled)
    row.single_unit = str(body.single_unit or "")

    row.retention_days = retention_days
    row.is_enabled = bool(body.is_enabled)

    # if re-enabling, clear deleted_at
    if row.is_enabled:
        row.deleted_at = None

    row.updated_at = func.now()

    db.add(row)
    db.commit()
    db.refresh(row)

    # ✅ start/update node-red stream ONLY if enabled and not deleted
    if row.is_enabled and row.deleted_at is None:
        try:
            # ✅ Lazy import prevents startup crash if Node-RED helper changes
            from routers.node_red_graphics import start_graphic_stream

            start_graphic_stream(
                user_id=current_user.id,
                dash_id=row.dashboard_id,
                widget_id=row.widget_id,
                device_id=row.bind_device_id,
                field=row.bind_field,
                sample_ms=row.sample_ms,
            )
        except Exception as e:
            # never crash backend if node-red is down or helper missing
            print(f"[graphic-display-bindings] start_graphic_stream failed: {e}")

    return {
        "ok": True,
        "binding": {
            "id": row.id,
            "user_id": row.user_id,
            "dashboard_id": row.dashboard_id,
            "widget_id": row.widget_id,
            "bind_model": row.bind_model,
            "bind_device_id": row.bind_device_id,
            "bind_field": row.bind_field,
            "title": row.title,
            "time_unit": row.time_unit,
            "window_size": row.window_size,
            "sample_ms": row.sample_ms,
            "y_min": row.y_min,
            "y_max": row.y_max,
            "line_color": row.line_color,
            "graph_style": row.graph_style,
            "math_formula": row.math_formula,
            "totalizer_enabled": row.totalizer_enabled,
            "totalizer_unit": row.totalizer_unit,
            "single_units_enabled": row.single_units_enabled,
            "single_unit": row.single_unit,
            "retention_days": row.retention_days,
            "is_enabled": row.is_enabled,
            "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        },
    }


# =========================
# GET: list current user's bindings for a dashboard
# =========================
@router.get("/list")
def list_graphic_display_bindings(
    dashboard_id: str = "main",
    include_disabled: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    dash = _clean_text(dashboard_id, "main")

    q = (
        db.query(GraphicDisplayBinding)
        .filter(
            GraphicDisplayBinding.user_id == current_user.id,
            GraphicDisplayBinding.dashboard_id == dash,
        )
        .order_by(GraphicDisplayBinding.id.asc())
    )

    if not include_disabled:
        q = q.filter(
            GraphicDisplayBinding.is_enabled.is_(True),
            GraphicDisplayBinding.deleted_at.is_(None),
        )

    rows = q.all()

    return [
        {
            "id": r.id,
            "dashboard_id": r.dashboard_id,
            "widget_id": r.widget_id,
            "bind_model": r.bind_model,
            "bind_device_id": r.bind_device_id,
            "bind_field": r.bind_field,
            "title": r.title,
            "time_unit": r.time_unit,
            "window_size": r.window_size,
            "sample_ms": r.sample_ms,
            "y_min": r.y_min,
            "y_max": r.y_max,
            "line_color": r.line_color,
            "graph_style": r.graph_style,
            "math_formula": r.math_formula,
            "totalizer_enabled": r.totalizer_enabled,
            "totalizer_unit": r.totalizer_unit,
            "single_units_enabled": r.single_units_enabled,
            "single_unit": r.single_unit,
            "retention_days": r.retention_days,
            "is_enabled": r.is_enabled,
            "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


# =========================
# POST: soft delete (keeps history)
# ✅ NOW ALSO tells Node-RED to stop the stream
# =========================
class SoftDeleteBody(BaseModel):
    dashboard_id: str = "main"
    widget_id: str


@router.post("/soft-delete")
def soft_delete_graphic_display_binding(
    body: SoftDeleteBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    dash = _clean_text(body.dashboard_id, "main")
    wid = _clean_text(body.widget_id)

    if not wid:
        raise HTTPException(status_code=400, detail="widget_id is required")

    row = (
        db.query(GraphicDisplayBinding)
        .filter(
            GraphicDisplayBinding.user_id == current_user.id,
            GraphicDisplayBinding.dashboard_id == dash,
            GraphicDisplayBinding.widget_id == wid,
        )
        .first()
    )

    if not row:
        return {"ok": True, "deleted": False}

    # ✅ soft-delete in DB
    row.is_enabled = False
    row.deleted_at = func.now()
    row.updated_at = func.now()
    db.add(row)
    db.commit()
    db.refresh(row)

    # ✅ ALSO stop node-red stream (so file writing stops immediately)
    try:
        from routers.node_red_graphics import stop_graphic_stream

        stop_graphic_stream(
            user_id=current_user.id,
            dash_id=row.dashboard_id,
            widget_id=row.widget_id,
        )
    except Exception as e:
        # never crash backend if node-red is down or helper missing
        print(f"[graphic-display-bindings] stop_graphic_stream failed: {e}")

    return {"ok": True, "deleted": True}