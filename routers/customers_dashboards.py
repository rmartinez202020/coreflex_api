from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from sqlalchemy import func as sa_func
from typing import Optional, List, Any, Dict
from datetime import datetime, timedelta
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
from routers.log_engine import (
    send_log,
    LOG_CATEGORY_SECURITY,
    LOG_CATEGORY_DASHBOARD,
    LOG_STATUS_SUCCESS,
    LOG_STATUS_FAILED,
    LOG_ACTOR_TENANT,
)

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


class TenantPublicSessionRequest(BaseModel):
    dashboard_slug: str
    public_launch_id: str
    email: EmailStr
    session_id: str


class TenantPublicAuthOut(BaseModel):
    ok: bool
    tenant_name: str
    access_level: str
    must_change_password: bool
    session_id: Optional[str] = None


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

TENANT_DASHBOARD_SESSION_TIMEOUT_SECONDS = int(
    os.getenv("TENANT_DASHBOARD_SESSION_TIMEOUT_SECONDS") or "120"
)


def _norm(s: Optional[str]) -> str:
    return (s or "").strip()


def _now_utc() -> datetime:
    return datetime.utcnow()


def _is_active_session_fresh(last_seen_at: Optional[datetime]) -> bool:
    if not last_seen_at:
        return False

    try:
        age = _now_utc() - last_seen_at.replace(tzinfo=None)
        return age <= timedelta(seconds=TENANT_DASHBOARD_SESSION_TIMEOUT_SECONDS)
    except Exception:
        return False


def _get_request_user_agent(request: Optional[Request]) -> str:
    try:
        return _norm(request.headers.get("user-agent", ""))[:500]
    except Exception:
        return ""


def _get_request_ip(request: Optional[Request]) -> str | None:
    try:
        forwarded = _norm(request.headers.get("x-forwarded-for", ""))
        if forwarded:
            return forwarded.split(",")[0].strip()

        if request.client and request.client.host:
            return _norm(str(request.client.host)) or None
    except Exception:
        pass

    return None


def _generate_session_id() -> str:
    return secrets.token_urlsafe(32)


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

    access_row = _get_tenant_dashboard_access_row(
        db=db,
        tenant_user_id=tenant.id,
        dashboard_id=dashboard.id,
    )

    if not access_row:
        raise HTTPException(
            status_code=403,
            detail="This tenant user does not have access to this dashboard.",
        )

    return tenant


def _get_tenant_dashboard_access_row(
    db: Session,
    tenant_user_id: int,
    dashboard_id: int,
) -> Optional[TenantUserDashboardAccess]:
    return (
        db.query(TenantUserDashboardAccess)
        .filter(TenantUserDashboardAccess.tenant_user_id == tenant_user_id)
        .filter(TenantUserDashboardAccess.dashboard_id == dashboard_id)
        .first()
    )


def _claim_tenant_dashboard_session(
    db: Session,
    *,
    tenant: TenantUser,
    dashboard: CustomerDashboard,
    request: Optional[Request] = None,
) -> str:
    access_row = _get_tenant_dashboard_access_row(
        db=db,
        tenant_user_id=tenant.id,
        dashboard_id=dashboard.id,
    )

    if not access_row:
        raise HTTPException(
            status_code=403,
            detail="This tenant user does not have access to this dashboard.",
        )

    existing_session_id = _norm(getattr(access_row, "active_session_id", ""))
    last_seen_at = getattr(access_row, "active_session_last_seen_at", None)

    if existing_session_id and _is_active_session_fresh(last_seen_at):
        raise HTTPException(
            status_code=409,
            detail="This tenant user is already logged in to this dashboard. Please logout from the other session first.",
        )

    session_id = _generate_session_id()
    now = _now_utc()

    access_row.active_session_id = session_id
    access_row.active_session_started_at = now
    access_row.active_session_last_seen_at = now
    access_row.active_session_user_agent = _get_request_user_agent(request)

    db.add(access_row)
    db.commit()
    db.refresh(access_row)

    return session_id


def _verify_tenant_dashboard_session(
    db: Session,
    *,
    dashboard_slug: str,
    public_launch_id: str,
    email: str,
    session_id: str,
) -> TenantUserDashboardAccess:
    dashboard = _get_public_dashboard_or_404(
        db=db,
        dashboard_slug=dashboard_slug,
        public_launch_id=public_launch_id,
    )

    tenant = _get_tenant_for_public_dashboard(
        db=db,
        dashboard=dashboard,
        email=email,
    )

    access_row = _get_tenant_dashboard_access_row(
        db=db,
        tenant_user_id=tenant.id,
        dashboard_id=dashboard.id,
    )

    if not access_row:
        raise HTTPException(status_code=403, detail="Access not found.")

    saved_session_id = _norm(getattr(access_row, "active_session_id", ""))
    incoming_session_id = _norm(session_id)

    if not incoming_session_id or saved_session_id != incoming_session_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")

    return access_row


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
    request: Request,
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

    # LOGS & ACTIVITY
    # DASHBOARD -> CREATE
    send_log(
        user_id=current_user.id,
        user_email=current_user.email,
        category=LOG_CATEGORY_DASHBOARD,
        action="DASHBOARD_CREATE",
        status=LOG_STATUS_SUCCESS,
        message=(
            f"Dashboard created: {row.dashboard_name} "
            f"| Customer: {row.customer_name}"
        ),
        dashboard_id=row.id,
        field="dashboard",
        new_value={
            "dashboard_id": row.id,
            "dashboard_name": row.dashboard_name,
            "customer_name": row.customer_name,
        },
        ip_address=_get_request_ip(request),
        user_agent=_get_request_user_agent(request),
    )

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
# ✅ Prevent same tenant-user from logging into same dashboard more than once
# =========================
@router.post("/tenant-access/login", response_model=TenantPublicAuthOut)
def tenant_public_dashboard_login(
    body: TenantPublicLoginRequest,
    request: Request,
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

    # Resolve the CoreFlex owner from the trusted dashboard ownership.
    # Tenant activity is always written under this owner's logs.
    owner = (
        db.query(User)
        .filter(User.id == dashboard.user_id)
        .first()
    )

    tenant_name = (
        _norm(getattr(tenant, "full_name", ""))
        or _norm(body.email)
    )

    if not _verify_password(body.password, tenant.password_hash):
        # LOGS & ACTIVITY
        # SECURITY -> TENANT LOGIN FAILED
        if owner:
            send_log(
                user_id=owner.id,
                user_email=owner.email,
                actor_type=LOG_ACTOR_TENANT,
                tenant_user_id=tenant.id,
                tenant_email=tenant.email,
                tenant_name=tenant_name,
                category=LOG_CATEGORY_SECURITY,
                action="LOGIN_FAILED",
                status=LOG_STATUS_FAILED,
                message="Tenant login failed: invalid credentials",
                dashboard_id=dashboard.id,
                ip_address=_get_request_ip(request),
                user_agent=_get_request_user_agent(request),
            )

        raise HTTPException(status_code=401, detail="Invalid email or password.")

    access_level = _norm(getattr(tenant, "access_level", "")) or "read"
    must_change_password = bool(getattr(tenant, "must_change_password", False))

    session_id = None

    # Do NOT claim a dashboard session until password is valid and usable.
    if not must_change_password:
        session_id = _claim_tenant_dashboard_session(
            db=db,
            tenant=tenant,
            dashboard=dashboard,
            request=request,
        )

    # LOGS & ACTIVITY
    # SECURITY -> TENANT LOGIN SUCCESS
    if owner:
        send_log(
            user_id=owner.id,
            user_email=owner.email,
            actor_type=LOG_ACTOR_TENANT,
            tenant_user_id=tenant.id,
            tenant_email=tenant.email,
            tenant_name=tenant_name,
            category=LOG_CATEGORY_SECURITY,
            action="LOGIN_SUCCESS",
            status=LOG_STATUS_SUCCESS,
            message="Tenant login successful",
            dashboard_id=dashboard.id,
            ip_address=_get_request_ip(request),
            user_agent=_get_request_user_agent(request),
        )

    return TenantPublicAuthOut(
        ok=True,
        tenant_name=tenant_name,
        access_level=access_level,
        must_change_password=must_change_password,
        session_id=session_id,
    )


# =========================
# 🔐 TENANT SET NEW PASSWORD FOR PUBLIC DASHBOARD
# ✅ After password change, claim dashboard session too
# =========================
@router.post("/tenant-access/set-password", response_model=TenantPublicAuthOut)
def tenant_public_dashboard_set_password(
    body: TenantPublicSetPasswordRequest,
    request: Request,
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
    tenant.updated_at = _now_utc()

    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    access_level = _norm(getattr(tenant, "access_level", "")) or "read"

    session_id = _claim_tenant_dashboard_session(
        db=db,
        tenant=tenant,
        dashboard=dashboard,
        request=request,
    )

    return TenantPublicAuthOut(
        ok=True,
        tenant_name=_norm(getattr(tenant, "full_name", "")) or _norm(body.email),
        access_level=access_level,
        must_change_password=False,
        session_id=session_id,
    )


# =========================
# 💓 TENANT DASHBOARD SESSION HEARTBEAT
# Frontend will call this every few seconds/minutes
# =========================
@router.post("/tenant-access/session/heartbeat")
def tenant_public_dashboard_session_heartbeat(
    body: TenantPublicSessionRequest,
    db: Session = Depends(get_db),
):
    access_row = _verify_tenant_dashboard_session(
        db=db,
        dashboard_slug=body.dashboard_slug,
        public_launch_id=body.public_launch_id,
        email=body.email,
        session_id=body.session_id,
    )

    access_row.active_session_last_seen_at = _now_utc()

    db.add(access_row)
    db.commit()

    return {"ok": True}


# =========================
# 🚪 TENANT DASHBOARD SESSION LOGOUT / RELEASE
# Frontend will call this on logout/window close
# =========================
@router.post("/tenant-access/session/logout")
def tenant_public_dashboard_session_logout(
    body: TenantPublicSessionRequest,
    db: Session = Depends(get_db),
):
    access_row = _verify_tenant_dashboard_session(
        db=db,
        dashboard_slug=body.dashboard_slug,
        public_launch_id=body.public_launch_id,
        email=body.email,
        session_id=body.session_id,
    )

    access_row.active_session_id = None
    access_row.active_session_started_at = None
    access_row.active_session_last_seen_at = None
    access_row.active_session_user_agent = None

    db.add(access_row)
    db.commit()

    return {"ok": True}


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
    request: Request,
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

    # LOGS & ACTIVITY
    # DASHBOARD -> SAVE
    send_log(
        user_id=current_user.id,
        user_email=current_user.email,
        category=LOG_CATEGORY_DASHBOARD,
        action="DASHBOARD_SAVE",
        status=LOG_STATUS_SUCCESS,
        message=(
            f"Dashboard saved: {row.dashboard_name} "
            f"| Customer: {row.customer_name}"
        ),
        dashboard_id=row.id,
        field="dashboard",
        new_value={
            "dashboard_id": row.id,
            "dashboard_name": row.dashboard_name,
            "customer_name": row.customer_name,
        },
        ip_address=_get_request_ip(request),
        user_agent=_get_request_user_agent(request),
    )

    return _serialize_dashboard(row)


# =========================
# 🗑 DELETE DASHBOARD
# =========================
@router.delete("/{dashboard_id}")
def delete_customer_dashboard(
    dashboard_id: int,
    request: Request,
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
    deleted_customer_name = row.customer_name

    db.delete(row)
    db.commit()

    # LOGS & ACTIVITY
    # DASHBOARD -> DELETE
    send_log(
        user_id=current_user.id,
        user_email=current_user.email,
        category=LOG_CATEGORY_DASHBOARD,
        action="DASHBOARD_DELETE",
        status=LOG_STATUS_SUCCESS,
        message=(
            f"Dashboard deleted: {deleted_name} "
            f"| Customer: {deleted_customer_name}"
        ),
        dashboard_id=dashboard_id,
        field="dashboard",
        old_value={
            "dashboard_id": dashboard_id,
            "dashboard_name": deleted_name,
            "customer_name": deleted_customer_name,
        },
        ip_address=_get_request_ip(request),
        user_agent=_get_request_user_agent(request),
    )

    return {
        "ok": True,
        "deleted_id": dashboard_id,
        "dashboard_name": deleted_name,
    }