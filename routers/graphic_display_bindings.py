# routers/graphic_display_bindings.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from database import get_db
from auth_utils import get_current_user
from models import User

# ✅ Node-RED helper
from routers.node_red_graphics import start_graphic_stream

# ✅ Binding model mapped to public.graphic_display_bindings
from models import GraphicDisplayBinding

router = APIRouter(prefix="/graphic-display-bindings", tags=["Graphic Display Bindings"])


class UpsertBindingBody(BaseModel):
    dashboard_id: str = "main"
    widget_id: str

    # ✅ binding (matches your table)
    bind_model: str = "zhc1921"       # zhc1921 / zhc1661 / tp4000
    bind_device_id: str              # device id string
    bind_field: str = "ai1"          # ai1/ai2/...

    # ✅ display settings (optional but stored)
    title: str = "Graphic Display"
    time_unit: str = "seconds"
    window_size: int = 60
    sample_ms: int = 3000
    y_min: float = 0
    y_max: float = 100
    line_color: str = "#0c5ac8"
    graph_style: str = "line"

    # ✅ math
    math_formula: str = ""

    # ✅ totalizer
    totalizer_enabled: bool = False
    totalizer_unit: str = ""

    # ✅ single units
    single_units_enabled: bool = False
    single_unit: str = ""

    # ✅ retention
    retention_days: int = 35

    # ✅ soft control
    is_enabled: bool = True


def _clean_text(v, default=""):
    s = str(v or "").strip()
    return s if s else default


@router.post("/upsert")
def upsert_binding(
    body: UpsertBindingBody,
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

    # ✅ sanitize numeric fields
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

    y_min = float(body.y_min if body.y_min is not None else 0)
    y_max = float(body.y_max if body.y_max is not None else 100)

    # ✅ One binding per (user + dashboard + widget)
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

    # ✅ update binding
    row.bind_model = bind_model
    row.bind_device_id = bind_device_id
    row.bind_field = bind_field

    # ✅ update display settings
    row.title = _clean_text(body.title, "Graphic Display")
    row.time_unit = _clean_text(body.time_unit, "seconds")
    row.window_size = window_size
    row.sample_ms = sample_ms
    row.y_min = y_min
    row.y_max = y_max
    row.line_color = _clean_text(body.line_color, "#0c5ac8")
    row.graph_style = _clean_text(body.graph_style, "line")

    # ✅ math
    row.math_formula = str(body.math_formula or "")

    # ✅ totalizer
    row.totalizer_enabled = bool(body.totalizer_enabled)
    row.totalizer_unit = str(body.totalizer_unit or "")

    # ✅ single units
    row.single_units_enabled = bool(body.single_units_enabled)
    row.single_unit = str(body.single_unit or "")

    # ✅ retention
    row.retention_days = retention_days

    # ✅ enabled / deleted
    row.is_enabled = bool(body.is_enabled)
    if row.is_enabled:
        row.deleted_at = None

    row.updated_at = func.now()

    db.add(row)
    db.commit()
    db.refresh(row)

    # ✅ LIVE: tell Node-RED to start/update the stream (only if active)
    if row.is_enabled and row.deleted_at is None:
        start_graphic_stream(
            user_id=current_user.id,
            dash_id=row.dashboard_id,
            widget_id=row.widget_id,
            device_id=row.bind_device_id,
            field=row.bind_field,
            sample_ms=row.sample_ms,
        )

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


@router.get("/list")
def list_bindings(
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


class DeleteBindingBody(BaseModel):
    dashboard_id: str = "main"
    widget_id: str


@router.post("/delete")
def delete_binding(
    body: DeleteBindingBody,
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

    # ✅ soft delete (matches your table design)
    row.is_enabled = False
    row.deleted_at = func.now()
    row.updated_at = func.now()
    db.add(row)
    db.commit()

    # Optional later: call stop_graphic_stream() if you implement a stop endpoint.
    return {"ok": True, "deleted": True}