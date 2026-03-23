from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from sqlalchemy import func as sa_func
from typing import Optional, List, Any, Dict
from datetime import datetime
import os
import re
import secrets

from database import get_db
from models import (
    User,
    CustomerLocation,
    CustomerDashboard,
    TenantUser,
    TenantUserDashboardAccess,
)
from auth_utils import get_current_user
from passlib.context import CryptContext

router = APIRouter(prefix="/customers-dashboards", tags=["Customer Dashboards"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


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


class TenantPublicLoginRequest(BaseModel):
    dashboard_slug: str
    public_launch_id: str
    email: EmailStr
    password: str


class TenantPublicSetPasswordRequest(BaseModel):
    dashboard_slug: str
    public_launch_id: str
    email: EmailStr
    temporary_password: str
    new_password: str


class TenantPublicAuthOut(BaseModel):
    ok: bool
    tenant_name: str
    access_level: str
    must_change_password: bool


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


def _get_public_dashboard_or_404(
    db: Session,
    dashboard_slug: str,
    public_launch_id: str,
) -> CustomerDashboard:
    row = (
        db.query(CustomerDashboard)
        .filter(CustomerDashboard.public_launch_id == public_launch_id)
        .filter(CustomerDashboard.is_public_launch_enabled.is_(True))
        .first()
    )

    if not row:
        raise HTTPException(status_code=404, detail="Public dashboard not found")

    row = _ensure_public_fields(row, db)

    if _is_main_dashboard_name(row.dashboard_name):
        raise HTTPException(status_code=404, detail="Public dashboard not found")

    actual_slug = _norm(getattr(row, "dashboard_slug", "")) or _slugify_dashboard_name(
        row.dashboard_name
    )

    if actual_slug != _norm(dashboard_slug):
        raise HTTPException(status_code=404, detail="Public dashboard not found")

    return row


def _get_tenant_for_public_dashboard(
    db: Session,
    dashboard: CustomerDashboard,
    email: str,
) -> TenantUser:
    clean_email = _norm(email).lower()

    tenant = (
        db.query(TenantUser)
        .filter(TenantUser.owner_user_id == dashboard.user_id)
        .filter(TenantUser.customer_name.ilike(dashboard.customer_name))
        .filter(TenantUser.email.ilike(clean_email))
        .filter(TenantUser.is_active.is_(True))
        .first()
    )

    if not tenant:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    has_access = (
        db.query(CustomerDashboard.id)
        .join(
            TenantUserDashboardAccess,
            TenantUserDashboardAccess.dashboard_id == CustomerDashboard.id,
        )
        .filter(TenantUserDashboardAccess.tenant_user_id == tenant.id)
        .filter(CustomerDashboard.id == dashboard.id)
        .first()
    )

    if not has_access:
        raise HTTPException(
            status_code=403,
            detail="This tenant user does not have access to this dashboard.",
        )

    return tenant


def _verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return pwd_context.verify(plain_password, password_hash)
    except Exception:
        return False


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
@router.get(
    "/public/{dashboard_slug}/{public_launch_id}",
    response_model=CustomerDashboardOut,
)
def get_public_customer_dashboard(
    dashboard_slug: str,
    public_launch_id: str,
    db: Session = Depends(get_db),
):
    row = _get_public_dashboard_or_404(db, dashboard_slug, public_launch_id)
    return _serialize_dashboard(row)


# =========================
# 🔐 TENANT LOGIN FOR PUBLIC DASHBOARD
# =========================
@router.post("/tenant-access/login", response_model=TenantPublicAuthOut)
def tenant_public_dashboard_login(
    body: TenantPublicLoginRequest,
    db: Session = Depends(get_db),
):
    dashboard = _get_public_dashboard_or_404(
        db,
        body.dashboard_slug,
        body.public_launch_id,
    )

    tenant = _get_tenant_for_public_dashboard(
        db,
        dashboard,
        body.email,
    )

    if not _verify_password(body.password, tenant.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    access_level = _norm(getattr(tenant, "access_level", "")) or "read"

    return TenantPublicAuthOut(
        ok=True,
        tenant_name=_norm(getattr(tenant, "full_name", "")) or _norm(body.email),
        access_level=access_level,
        must_change_password=bool(getattr(tenant, "must_change_password", False)),
    )


# =========================
# 🔐 TENANT SET NEW PASSWORD FOR PUBLIC DASHBOARD
# =========================
@router.post("/tenant-access/set-password", response_model=TenantPublicAuthOut)
def tenant_public_dashboard_set_password(
    body: TenantPublicSetPasswordRequest,
    db: Session = Depends(get_db),
):
    dashboard = _get_public_dashboard_or_404(
        db,
        body.dashboard_slug,
        body.public_launch_id,
    )

    tenant = _get_tenant_for_public_dashboard(
        db,
        dashboard,
        body.email,
    )

    if not _verify_password(body.temporary_password, tenant.password_hash):
        raise HTTPException(
            status_code=401,
            detail="Invalid temporary password.",
        )

    new_password = _norm(body.new_password)

    if len(new_password) < 8:
        raise HTTPException(
            status_code=400,
            detail="New password must be at least 8 characters.",
        )

    if new_password == _norm(body.temporary_password):
        raise HTTPException(
            status_code=400,
            detail="New password must be different from the temporary password.",
        )

    tenant.password_hash = pwd_context.hash(new_password)
    tenant.must_change_password = False
    tenant.updated_at = datetime.utcnow()

    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    access_level = _norm(getattr(tenant, "access_level", "")) or "read"

    return TenantPublicAuthOut(
        ok=True,
        tenant_name=_norm(getattr(tenant, "full_name", "")) or _norm(body.email),
        access_level=access_level,
        must_change_password=False,
    )


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