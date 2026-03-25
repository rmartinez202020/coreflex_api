# routers/graphic_display_bindings.py
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from database import get_db
from auth_utils import get_current_user
from models import (
    User,
    GraphicDisplayBinding,
    TenantUser,
    TenantUserDashboardAccess,
    CustomerDashboard,
)

router = APIRouter(prefix="/graphic-display-bindings", tags=["Graphic Display Bindings"])


# =========================
# DEBUG HELPERS
# =========================
def _dbg(label: str, **kwargs):
    try:
        print("\n========== GRAPHIC DISPLAY DEBUG ==========")
        print(label)
        for k, v in kwargs.items():
            print(f"{k} = {v}")
        print("==========================================\n")
    except Exception:
        pass


# =========================
# AUTH HELPERS
# =========================
def get_current_user_optional(request: Request, db: Session = Depends(get_db)):
    try:
        return get_current_user(request=request, db=db)
    except Exception:
        return None


def _clean_text(v, default=""):
    s = str(v or "").strip()
    return s if s else default


def _tenant_email_from_request(request: Request) -> str:
    return _clean_text(request.headers.get("X-Tenant-Email")).lower()


def _is_public_tenant_request(request: Request, current_user) -> bool:
    return current_user is None and bool(_tenant_email_from_request(request))


def _resolve_tenant_owner_user_id(
    *,
    db: Session,
    request: Request,
    dashboard_id: str,
):
    """
    Resolve the OWNER user_id for a public tenant request using:
    - X-Tenant-Email
    - dashboard_id

    Security:
    - tenant must exist
    - tenant must have dashboard access
    - dashboard must exist

    ✅ IMPORTANT:
    Public Graphic Display history/visibility may still use dashboard_id="main"
    because historian files and bindings are stored under dash_main.
    In that case, we cannot validate access from "main" directly, so we resolve
    the tenant's accessible dashboard row first and use its owner user_id.
    """
    tenant_email = _tenant_email_from_request(request)
    dash = _clean_text(dashboard_id, "main")

    if not tenant_email:
        raise HTTPException(status_code=401, detail="Unauthorized")

    tenant = (
        db.query(TenantUser)
        .filter(func.lower(TenantUser.email) == tenant_email)
        .first()
    )
    if not tenant:
        _dbg(
            "TENANT NOT FOUND FOR GRAPHIC DISPLAY REQUEST",
            tenant_email=tenant_email,
            dashboard_id=dash,
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    dashboard = None
    access_row = None

    # ✅ Normal case: numeric launched dashboard id
    if dash and str(dash).isdigit():
        dash_int = int(str(dash))

        dashboard = (
            db.query(CustomerDashboard)
            .filter(CustomerDashboard.id == dash_int)
            .first()
        )
        if not dashboard:
            _dbg(
                "CUSTOMER DASHBOARD NOT FOUND FOR GRAPHIC DISPLAY REQUEST",
                tenant_email=tenant_email,
                dashboard_id=dash,
            )
            raise HTTPException(status_code=404, detail="Dashboard not found")

        access_row = (
            db.query(TenantUserDashboardAccess)
            .filter(
                TenantUserDashboardAccess.tenant_user_id == tenant.id,
                TenantUserDashboardAccess.dashboard_id == dashboard.id,
            )
            .first()
        )
        if not access_row:
            _dbg(
                "TENANT HAS NO ACCESS TO DASHBOARD",
                tenant_email=tenant_email,
                tenant_user_id=tenant.id,
                dashboard_id=dash,
            )
            raise HTTPException(
                status_code=403,
                detail="Tenant does not have access to this dashboard",
            )

    else:
        # ✅ Special case:
        # public launch is requesting dashboard_id="main" because historian files
        # are stored in dash_main. We still need to verify tenant access securely.
        # We do that by requiring the tenant to have at least one dashboard access row.
        access_row = (
            db.query(TenantUserDashboardAccess)
            .filter(TenantUserDashboardAccess.tenant_user_id == tenant.id)
            .order_by(TenantUserDashboardAccess.id.asc())
            .first()
        )
        if not access_row:
            _dbg(
                "TENANT HAS NO DASHBOARD ACCESS ROWS FOR MAIN GRAPHIC REQUEST",
                tenant_email=tenant_email,
                tenant_user_id=tenant.id,
                dashboard_id=dash,
            )
            raise HTTPException(
                status_code=403,
                detail="Tenant does not have access to this dashboard",
            )

        dashboard = (
            db.query(CustomerDashboard)
            .filter(CustomerDashboard.id == access_row.dashboard_id)
            .first()
        )
        if not dashboard:
            _dbg(
                "CUSTOMER DASHBOARD NOT FOUND FROM TENANT ACCESS ROW",
                tenant_email=tenant_email,
                tenant_user_id=tenant.id,
                dashboard_id=dash,
                access_dashboard_id=access_row.dashboard_id,
            )
            raise HTTPException(status_code=404, detail="Dashboard not found")

    owner_user_id = getattr(dashboard, "user_id", None)
    if not owner_user_id:
        _dbg(
            "CUSTOMER DASHBOARD OWNER USER ID MISSING",
            tenant_email=tenant_email,
            dashboard_id=dash,
            dashboard_obj=str(dashboard),
        )
        raise HTTPException(status_code=500, detail="Dashboard owner not found")

    _dbg(
        "RESOLVED TENANT OWNER USER ID",
        tenant_email=tenant_email,
        tenant_user_id=tenant.id,
        dashboard_id=dash,
        resolved_customer_dashboard_id=getattr(dashboard, "id", None),
        owner_user_id=owner_user_id,
    )

    return owner_user_id


def _resolve_public_binding_dashboard_id(
    *,
    db: Session,
    owner_user_id: int,
    requested_dashboard_id: str,
    widget_id: str,
) -> str:
    """
    ✅ For public tenant flow, bindings/historian may be stored under "main"
    even when the launched customer dashboard id is numeric.

    Priority:
    1) exact requested dashboard_id
    2) "main"
    """
    requested = _clean_text(requested_dashboard_id, "main")
    wid = _clean_text(widget_id)

    if requested:
        exact = (
            db.query(GraphicDisplayBinding)
            .filter(
                GraphicDisplayBinding.user_id == owner_user_id,
                GraphicDisplayBinding.dashboard_id == requested,
                GraphicDisplayBinding.widget_id == wid,
            )
            .first()
        )
        if exact:
            return requested

    fallback = "main"
    main_row = (
        db.query(GraphicDisplayBinding)
        .filter(
            GraphicDisplayBinding.user_id == owner_user_id,
            GraphicDisplayBinding.dashboard_id == fallback,
            GraphicDisplayBinding.widget_id == wid,
        )
        .first()
    )
    if main_row:
        return fallback

    return requested


def _resolve_graphic_binding_for_request(
    *,
    db: Session,
    request: Request,
    current_user,
    dashboard_id: str,
    widget_id: str,
):
    dash = _clean_text(dashboard_id, "main")
    wid = _clean_text(widget_id)

    if not wid:
        raise HTTPException(status_code=400, detail="widget_id is required")

    # ✅ Private / owner flow
    if current_user:
        row = (
            db.query(GraphicDisplayBinding)
            .filter(
                GraphicDisplayBinding.user_id == current_user.id,
                GraphicDisplayBinding.dashboard_id == dash,
                GraphicDisplayBinding.widget_id == wid,
            )
            .first()
        )
        owner_user_id = current_user.id

        _dbg(
            "RESOLVE GRAPHIC BINDING - OWNER FLOW",
            current_user_id=current_user.id,
            dashboard_id=dash,
            widget_id=wid,
            found=bool(row),
        )
        return row, owner_user_id, dash

    # ✅ Public tenant flow
    tenant_email = _tenant_email_from_request(request)
    if not tenant_email:
        raise HTTPException(status_code=401, detail="Unauthorized")

    owner_user_id = _resolve_tenant_owner_user_id(
        db=db,
        request=request,
        dashboard_id=dash,
    )

    binding_dash = _resolve_public_binding_dashboard_id(
        db=db,
        owner_user_id=owner_user_id,
        requested_dashboard_id=dash,
        widget_id=wid,
    )

    row = (
        db.query(GraphicDisplayBinding)
        .filter(
            GraphicDisplayBinding.user_id == owner_user_id,
            GraphicDisplayBinding.dashboard_id == binding_dash,
            GraphicDisplayBinding.widget_id == wid,
        )
        .first()
    )

    _dbg(
        "RESOLVE GRAPHIC BINDING - TENANT FLOW",
        tenant_email=tenant_email,
        owner_user_id=owner_user_id,
        requested_dashboard_id=dash,
        binding_dashboard_id=binding_dash,
        widget_id=wid,
        found=bool(row),
    )

    return row, owner_user_id, binding_dash


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


# =========================
# BODY: VISIBILITY
# =========================
class GraphicVisibilityBody(BaseModel):
    dashboard_id: str = "main"
    widget_id: str
    is_visible: bool


# =========================
# BODY: soft delete
# =========================
class SoftDeleteBody(BaseModel):
    dashboard_id: str = "main"
    widget_id: str


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

    if row.is_enabled:
        row.deleted_at = None

    row.updated_at = func.now()

    db.add(row)
    db.commit()
    db.refresh(row)

    _dbg(
        "UPSERT GRAPHIC DISPLAY BINDING",
        user_id=current_user.id,
        dashboard_id=row.dashboard_id,
        widget_id=row.widget_id,
        bind_model=row.bind_model,
        bind_device_id=row.bind_device_id,
        bind_field=row.bind_field,
        title=row.title,
        time_unit=row.time_unit,
        window_size=row.window_size,
        sample_ms=row.sample_ms,
        retention_days=row.retention_days,
        is_enabled=row.is_enabled,
    )

    if row.is_enabled and row.deleted_at is None:
        try:
            from routers.node_red_graphics import start_graphic_stream

            start_graphic_stream(
                user_id=current_user.id,
                dash_id=row.dashboard_id,
                widget_id=row.widget_id,
                bind_model=row.bind_model,
                device_id=row.bind_device_id,
                field=row.bind_field,
                title=row.title,
                time_unit=row.time_unit,
                window_size=row.window_size,
                sample_ms=row.sample_ms,
                y_min=row.y_min,
                y_max=row.y_max,
                line_color=row.line_color,
                graph_style=row.graph_style,
                math_formula=row.math_formula,
                totalizer_enabled=row.totalizer_enabled,
                totalizer_unit=row.totalizer_unit,
                single_units_enabled=row.single_units_enabled,
                single_unit=row.single_unit,
                retention_days=row.retention_days,
            )
        except Exception as e:
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
# POST: visibility update
# =========================
@router.post("/visibility")
def set_graphic_display_visibility(
    body: GraphicVisibilityBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional),
):
    dash = _clean_text(body.dashboard_id, "main")
    wid = _clean_text(body.widget_id)

    row, owner_user_id, binding_dash = _resolve_graphic_binding_for_request(
        db=db,
        request=request,
        current_user=current_user,
        dashboard_id=dash,
        widget_id=wid,
    )

    if not row:
        _dbg(
            "VISIBILITY ROW NOT FOUND",
            requested_dashboard_id=dash,
            binding_dashboard_id=binding_dash,
            widget_id=wid,
            is_visible=bool(body.is_visible),
            current_user_id=getattr(current_user, "id", None),
            tenant_email=_tenant_email_from_request(request),
        )
        raise HTTPException(status_code=404, detail="Graphic display binding not found")

    try:
        from routers.node_red_graphics import set_graphic_stream_visibility

        ok = set_graphic_stream_visibility(
            user_id=owner_user_id,
            dash_id=binding_dash,
            widget_id=wid,
            is_visible=bool(body.is_visible),
        )

        _dbg(
            "VISIBILITY UPDATE",
            owner_user_id=owner_user_id,
            requested_dashboard_id=dash,
            binding_dashboard_id=binding_dash,
            widget_id=wid,
            is_visible=bool(body.is_visible),
            ok=ok,
            current_user_id=getattr(current_user, "id", None),
            tenant_email=_tenant_email_from_request(request),
        )

        return {
            "ok": ok,
            "dashboard_id": binding_dash,
            "requested_dashboard_id": dash,
            "widget_id": wid,
            "is_visible": bool(body.is_visible),
        }
    except Exception as e:
        print(f"[graphic-display-bindings] set visibility failed: {e}")
        return {
            "ok": False,
            "dashboard_id": binding_dash,
            "requested_dashboard_id": dash,
            "widget_id": wid,
            "is_visible": bool(body.is_visible),
            "error": str(e),
        }


# =========================
# GET: historian for current user/dashboard/widget
# ✅ NOW reads via Node-RED instead of local Render disk
# =========================
@router.get("/history")
def get_graphic_display_history(
    request: Request,
    dashboard_id: str = Query("main"),
    widget_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional),
):
    dash = _clean_text(dashboard_id, "main")
    wid = _clean_text(widget_id)

    _dbg(
        "HISTORY REQUEST START",
        current_user_id=getattr(current_user, "id", None),
        tenant_email=_tenant_email_from_request(request),
        dashboard_id_raw=dashboard_id,
        dashboard_id_clean=dash,
        widget_id_raw=widget_id,
        widget_id_clean=wid,
    )

    row, owner_user_id, binding_dash = _resolve_graphic_binding_for_request(
        db=db,
        request=request,
        current_user=current_user,
        dashboard_id=dash,
        widget_id=wid,
    )

    if not row:
        _dbg(
            "HISTORY DB ROW NOT FOUND",
            owner_user_id=owner_user_id,
            requested_dashboard_id=dash,
            binding_dashboard_id=binding_dash,
            widget_id=wid,
            current_user_id=getattr(current_user, "id", None),
            tenant_email=_tenant_email_from_request(request),
        )
        raise HTTPException(status_code=404, detail="Graphic display binding not found")

    try:
        from routers.node_red_graphics import get_graphic_history

        data = get_graphic_history(
            user_id=owner_user_id,
            dash_id=binding_dash,
            widget_id=wid,
        )

        _dbg(
            "HISTORY NODE-RED RESPONSE",
            owner_user_id=owner_user_id,
            requested_dashboard_id=dash,
            binding_dashboard_id=binding_dash,
            widget_id=wid,
            ok=data.get("ok"),
            error=data.get("error"),
            history_dir=data.get("historyDir"),
            prefix=data.get("prefix"),
            all_names_count=len(data.get("allNames") or []),
            matched_files=data.get("files") or [],
            count=data.get("count"),
        )

        return {
            "ok": bool(data.get("ok", True)),
            "dashboard_id": binding_dash,
            "requested_dashboard_id": dash,
            "widget_id": wid,
            "history_dir": data.get("historyDir"),
            "prefix": data.get("prefix"),
            "all_names": data.get("allNames", []),
            "files": data.get("files", []),
            "file_point_counts": data.get("filePointCounts", {}),
            "points": data.get("points", []),
            "count": int(data.get("count", 0) or 0),
            **({"error": data.get("error")} if data.get("error") else {}),
        }
    except Exception as e:
        _dbg(
            "HISTORY NODE-RED CALL FAILED",
            owner_user_id=owner_user_id,
            requested_dashboard_id=dash,
            binding_dashboard_id=binding_dash,
            widget_id=wid,
            error=str(e),
        )
        return {
            "ok": False,
            "dashboard_id": binding_dash,
            "requested_dashboard_id": dash,
            "widget_id": wid,
            "files": [],
            "points": [],
            "count": 0,
            "error": str(e),
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

    _dbg(
        "LIST GRAPHIC DISPLAY BINDINGS",
        current_user_id=current_user.id,
        dashboard_id=dash,
        include_disabled=include_disabled,
        count=len(rows),
        widget_ids=[r.widget_id for r in rows],
    )

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
# =========================
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
        _dbg(
            "SOFT DELETE ROW NOT FOUND",
            current_user_id=current_user.id,
            dashboard_id=dash,
            widget_id=wid,
        )
        return {"ok": True, "deleted": False}

    row.is_enabled = False
    row.deleted_at = func.now()
    row.updated_at = func.now()
    db.add(row)
    db.commit()
    db.refresh(row)

    _dbg(
        "SOFT DELETE GRAPHIC DISPLAY",
        current_user_id=current_user.id,
        dashboard_id=row.dashboard_id,
        widget_id=row.widget_id,
        deleted=True,
    )

    try:
        from routers.node_red_graphics import stop_graphic_stream

        stop_graphic_stream(
            user_id=current_user.id,
            dash_id=row.dashboard_id,
            widget_id=row.widget_id,
        )
    except Exception as e:
        print(f"[graphic-display-bindings] stop_graphic_stream failed: {e}")

    return {"ok": True, "deleted": True}