from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func as sa_func
from typing import Optional, List, Any, Dict
from datetime import datetime
import os
import re
import secrets

from database import get_db
from models import User, CustomerLocation, CustomerDashboard
from auth_utils import get_current_user

router = APIRouter(prefix="/customers-dashboards", tags=["Customer Dashboards"])


# =========================
# 📦 Schemas
# =========================
class CustomerDashboardCreate(BaseModel):
    customer_name: str
    dashboard_name: str


class CustomerDashboardOut(BaseModel):
    id: int
    user_id: int
    customer_name: str
    dashboard_name: str
    dashboard_slug: Optional[str] = None
    public_launch_id: Optional[str] = None
    is_public_launch_enabled: bool = False
    public_launch_url: Optional[str] = None
    layout: Dict[str, Any]
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CustomerOut(BaseModel):
    customer_name: str


class CustomerDashboardSave(BaseModel):
    layout: Dict[str, Any]


# =========================
# ✅ Helpers
# =========================
DEFAULT_LAYOUT = {
    "version": "1.0",
    "canvas": {"objects": []},
    "meta": {"savedAt": None},
}

PUBLIC_DASHBOARD_BASE_URL = (
    os.getenv("PUBLIC_DASHBOARD_BASE_URL")
    or "https://www.coreflexiiotsplatform.com/launchDashboard"
)


def _norm(s: Optional[str]) -> str:
    return (s or "").strip()


def _slugify_dashboard_name(name: str) -> str:
    value = _norm(name).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "dashboard"


def _is_main_dashboard_name(name: str) -> bool:
    return _norm(name).lower() == "main dashboard"


def _generate_public_launch_id() -> str:
    return secrets.token_hex(16)


def _build_public_launch_url(row: CustomerDashboard) -> Optional[str]:
    if not row:
        return None

    enabled = bool(getattr(row, "is_public_launch_enabled", False))
    public_id = _norm(getattr(row, "public_launch_id", ""))
    slug = _norm(getattr(row, "dashboard_slug", "")) or _slugify_dashboard_name(
        getattr(row, "dashboard_name", "")
    )

    if not enabled or not public_id:
        return None

    return f"{PUBLIC_DASHBOARD_BASE_URL}/{slug}/{public_id}"


def _get_owner_emails() -> set[str]:
    """
    Supports one or more owner emails via env:
      PLATFORM_OWNER_EMAIL=roquemartinez_8@hotmail.com
    or
      PLATFORM_OWNER_EMAILS=a@x.com,b@y.com
    """
    single = _norm(os.getenv("PLATFORM_OWNER_EMAIL"))
    multi = _norm(os.getenv("PLATFORM_OWNER_EMAILS"))

    emails = set()

    if single:
        emails.add(single.lower())

    if multi:
        for item in multi.split(","):
            v = _norm(item).lower()
            if v:
                emails.add(v)

    if not emails:
        emails.add("roquemartinez_8@hotmail.com")

    return emails


def _require_customer_exists(db: Session, user_id: int, customer_name: str) -> None:
    """
    For now, "customers" come from distinct CustomerLocation.customer_name.
    This prevents creating dashboards for random names.
    """
    exists = (
        db.query(CustomerLocation.id)
        .filter(CustomerLocation.user_id == user_id)
        .filter(sa_func.lower(CustomerLocation.customer_name) == customer_name.lower())
        .first()
    )
    if not exists:
        raise HTTPException(
            status_code=400,
            detail="Customer not found. Create a Customer/Location first, then create dashboards for that customer.",
        )


def _serialize_dashboard(row: CustomerDashboard) -> CustomerDashboardOut:
    """
    Safely normalize old/bad DB rows so response_model does not crash with 500.
    """
    raw_layout = row.layout if isinstance(row.layout, dict) else DEFAULT_LAYOUT

    customer_name = _norm(getattr(row, "customer_name", ""))
    dashboard_name = _norm(getattr(row, "dashboard_name", ""))
    dashboard_slug = _norm(getattr(row, "dashboard_slug", ""))
    public_launch_id = _norm(getattr(row, "public_launch_id", ""))

    return CustomerDashboardOut(
        id=row.id,
        user_id=row.user_id,
        customer_name=customer_name,
        dashboard_name=dashboard_name,
        dashboard_slug=dashboard_slug or None,
        public_launch_id=public_launch_id or None,
        is_public_launch_enabled=bool(
            getattr(row, "is_public_launch_enabled", False)
        ),
        public_launch_url=_build_public_launch_url(row),
        layout=raw_layout,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _assert_owner(current_user: User) -> None:
    user_email = _norm(getattr(current_user, "email", "")).lower()
    if not user_email:
        raise HTTPException(status_code=403, detail="Not authorized")

    if user_email not in _get_owner_emails():
        raise HTTPException(status_code=403, detail="Not authorized")


def _ensure_public_fields(row: CustomerDashboard, db: Session) -> CustomerDashboard:
    """
    Backfill old rows if needed:
    - non-main dashboards get slug/public id/enabled
    - main dashboard names are always disabled
    """
    changed = False
    dashboard_name = _norm(getattr(row, "dashboard_name", ""))

    if _is_main_dashboard_name(dashboard_name):
        if getattr(row, "is_public_launch_enabled", False):
            row.is_public_launch_enabled = False
            changed = True
        if getattr(row, "public_launch_id", None):
            row.public_launch_id = None
            changed = True
        if getattr(row, "dashboard_slug", None):
            row.dashboard_slug = None
            changed = True
    else:
        slug = _norm(getattr(row, "dashboard_slug", ""))
        public_id = _norm(getattr(row, "public_launch_id", ""))

        if not slug:
            row.dashboard_slug = _slugify_dashboard_name(dashboard_name)
            changed = True

        if not public_id:
            row.public_launch_id = _generate_public_launch_id()
            changed = True

        if not bool(getattr(row, "is_public_launch_enabled", False)):
            row.is_public_launch_enabled = True
            changed = True

    if changed:
        db.add(row)
        db.commit()
        db.refresh(row)

    return row


# =========================
# ✅ LIST CUSTOMERS (distinct names)
# =========================
@router.get("/customers", response_model=List[CustomerOut])
def list_customers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(CustomerLocation.customer_name)
        .filter(CustomerLocation.user_id == current_user.id)
        .group_by(CustomerLocation.customer_name)
        .order_by(CustomerLocation.customer_name.asc())
        .all()
    )
    return [{"customer_name": r[0]} for r in rows if r and r[0]]


# =========================
# ✅ CREATE DASHBOARD
# =========================
@router.post("", response_model=CustomerDashboardOut)
@router.post("/", response_model=CustomerDashboardOut, include_in_schema=False)
def create_customer_dashboard(
    body: CustomerDashboardCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    customer_name = _norm(body.customer_name)
    dashboard_name = _norm(body.dashboard_name)

    if not customer_name:
        raise HTTPException(status_code=400, detail="customer_name is required")
    if not dashboard_name:
        raise HTTPException(status_code=400, detail="dashboard_name is required")

    _require_customer_exists(db, current_user.id, customer_name)

    is_main = _is_main_dashboard_name(dashboard_name)

    row = CustomerDashboard(
        user_id=current_user.id,
        customer_name=customer_name,
        dashboard_name=dashboard_name,
        dashboard_slug=None if is_main else _slugify_dashboard_name(dashboard_name),
        public_launch_id=None if is_main else _generate_public_launch_id(),
        is_public_launch_enabled=False if is_main else True,
        layout=DEFAULT_LAYOUT,
    )

    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_dashboard(row)


# =========================
# ✅ LIST DASHBOARDS
# Optional filter: ?customer_name=
# =========================
@router.get("", response_model=List[CustomerDashboardOut])
@router.get("/", response_model=List[CustomerDashboardOut], include_in_schema=False)
def list_customer_dashboards(
    customer_name: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = (
        db.query(CustomerDashboard)
        .filter(CustomerDashboard.user_id == current_user.id)
        .order_by(CustomerDashboard.updated_at.desc())
    )

    if customer_name:
        q = q.filter(
            sa_func.lower(CustomerDashboard.customer_name) == customer_name.lower()
        )

    rows = q.all()

    normalized_rows = []
    for r in rows:
        normalized_rows.append(_ensure_public_fields(r, db))

    return [_serialize_dashboard(r) for r in normalized_rows]


# =========================
# 📊 OWNER: ALL DASHBOARDS REPORT
# =========================
@router.get("/admin/all", response_model=List[CustomerDashboardOut])
def list_all_dashboards_admin(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_owner(current_user)

    rows = (
        db.query(CustomerDashboard)
        .order_by(CustomerDashboard.updated_at.desc())
        .all()
    )

    normalized_rows = []
    for r in rows:
        normalized_rows.append(_ensure_public_fields(r, db))

    return [_serialize_dashboard(r) for r in normalized_rows]


# =========================
# 🌐 PUBLIC GET ONE DASHBOARD (NO LOGIN)
# Used by public launch route:
# /launchDashboard/{dashboard_slug}/{public_launch_id}
# =========================
@router.get("/public/{dashboard_slug}/{public_launch_id}", response_model=CustomerDashboardOut)
def get_public_customer_dashboard(
    dashboard_slug: str,
    public_launch_id: str,
    db: Session = Depends(get_db),
):
    row = (
        db.query(CustomerDashboard)
        .filter(CustomerDashboard.public_launch_id == public_launch_id)
        .filter(CustomerDashboard.is_public_launch_enabled.is_(True))
        .first()
    )

    if not row:
        raise HTTPException(status_code=404, detail="Public dashboard not found")

    row = _ensure_public_fields(row, db)

    # extra protection: main dashboard should never be public
    if _is_main_dashboard_name(row.dashboard_name):
        raise HTTPException(status_code=404, detail="Public dashboard not found")

    actual_slug = _norm(getattr(row, "dashboard_slug", "")) or _slugify_dashboard_name(
        row.dashboard_name
    )

    # slug mismatch -> treat as not found to keep URL strict
    if actual_slug != _norm(dashboard_slug):
        raise HTTPException(status_code=404, detail="Public dashboard not found")

    return _serialize_dashboard(row)


# =========================
# ✅ GET ONE DASHBOARD
# =========================
@router.get("/{dashboard_id}", response_model=CustomerDashboardOut)
def get_customer_dashboard(
    dashboard_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(CustomerDashboard)
        .filter(CustomerDashboard.id == dashboard_id)
        .filter(CustomerDashboard.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Dashboard not found")

    row = _ensure_public_fields(row, db)
    return _serialize_dashboard(row)


# =========================
# ✅ SAVE (update layout)
# App will POST full payload into `layout`
# =========================
@router.post("/{dashboard_id}", response_model=CustomerDashboardOut)
def save_customer_dashboard(
    dashboard_id: int,
    body: CustomerDashboardSave,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(CustomerDashboard)
        .filter(CustomerDashboard.id == dashboard_id)
        .filter(CustomerDashboard.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Dashboard not found")

    if not body.layout or not isinstance(body.layout, dict):
        raise HTTPException(status_code=400, detail="layout must be an object")

    row.layout = body.layout

    # keep public launch rules consistent
    if _is_main_dashboard_name(row.dashboard_name):
        row.dashboard_slug = None
        row.public_launch_id = None
        row.is_public_launch_enabled = False
    else:
        if not _norm(row.dashboard_slug):
            row.dashboard_slug = _slugify_dashboard_name(row.dashboard_name)
        if not _norm(row.public_launch_id):
            row.public_launch_id = _generate_public_launch_id()
        row.is_public_launch_enabled = True

    db.commit()
    db.refresh(row)
    return _serialize_dashboard(row)


# =========================
# 🗑 DELETE DASHBOARD
# =========================
@router.delete("/{dashboard_id}")
def delete_customer_dashboard(
    dashboard_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(CustomerDashboard)
        .filter(CustomerDashboard.id == dashboard_id)
        .filter(CustomerDashboard.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Dashboard not found or not owned by user",
        )

    deleted_name = row.dashboard_name

    db.delete(row)
    db.commit()

    return {
        "ok": True,
        "deleted_id": dashboard_id,
        "dashboard_name": deleted_name,
    }