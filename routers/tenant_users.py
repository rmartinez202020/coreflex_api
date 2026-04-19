# routers/tenant_users.py
import os
from datetime import datetime, timezone
import secrets
import string
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from passlib.context import CryptContext

from database import get_db
from models import (
    TenantUser,
    TenantUserDashboardAccess,
    CustomerDashboard,
    CustomerLocation,
    User,
    UserSubscription,
)
from auth_utils import get_current_user
from utils.email_service import send_tenant_credentials_email

router = APIRouter(prefix="/tenant-users", tags=["Tenant Users"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

PUBLIC_DASHBOARD_BASE_URL = os.getenv(
    "PUBLIC_DASHBOARD_BASE_URL",
    "https://www.coreflexiiotsplatform.com/launchDashboard",
)

# =========================
# 📦 SCHEMAS
# =========================
class TenantUserCreate(BaseModel):
    name: str
    email: EmailStr
    access: str
    customer_name: str
    dashboard_ids: List[int]


class TenantUserUpdate(BaseModel):
    name: str
    email: str
    access: str
    customer_name: str
    dashboard_ids: List[int]


class TenantUserDashboardMini(BaseModel):
    id: int
    dashboard_name: str

    class Config:
        from_attributes = True


class TenantUserOut(BaseModel):
    id: int
    full_name: str
    email: str
    access_level: str
    customer_name: str
    is_active: bool
    must_change_password: bool
    dashboards: List[TenantUserDashboardMini] = []

    class Config:
        from_attributes = True


# =========================
# 🔧 HELPERS
# =========================
def _norm(value: Optional[str]) -> str:
    return str(value or "").strip()


def _now_utc():
    return datetime.now(timezone.utc)


def _validate_access(access: str) -> str:
    v = _norm(access).lower()
    if v not in {"read", "read_control"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid access level. Use 'read' or 'read_control'.",
        )
    return v


def _generate_password(length: int = 12) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def _hash_password(password: str) -> str:
    return pwd_context.hash(password)


def _serialize_tenant_user(row: TenantUser) -> TenantUserOut:
    dashboards = []
    for access_row in row.dashboard_access or []:
        dash = access_row.dashboard
        if dash:
            dashboards.append(
                TenantUserDashboardMini(
                    id=dash.id,
                    dashboard_name=_norm(dash.dashboard_name),
                )
            )

    return TenantUserOut(
        id=row.id,
        full_name=_norm(row.full_name),
        email=_norm(row.email),
        access_level=_norm(row.access_level),
        customer_name=_norm(row.customer_name),
        is_active=bool(row.is_active),
        must_change_password=bool(row.must_change_password),
        dashboards=dashboards,
    )


def _get_or_create_user_subscription(db: Session, user_id: int) -> UserSubscription:
    row = (
        db.query(UserSubscription)
        .filter(UserSubscription.user_id == user_id)
        .first()
    )
    if row:
        return row

    row = UserSubscription(
        user_id=user_id,
        plan_key="free",
        device_limit=1,
        tenants_users_limit=1,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _get_tenant_user_capacity(db: Session, owner_user_id: int) -> dict:
    subscription = _get_or_create_user_subscription(db, owner_user_id)

    used_count = (
        db.query(TenantUser)
        .filter(TenantUser.owner_user_id == owner_user_id)
        .count()
    )

    limit_count = int(subscription.tenants_users_limit or 0)
    available_count = max(0, limit_count - used_count)

    return {
        "limit": limit_count,
        "used": used_count,
        "available": available_count,
    }


def _ensure_tenant_user_slot_available(db: Session, owner_user_id: int) -> None:
    capacity = _get_tenant_user_capacity(db, owner_user_id)

    if capacity["available"] <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Tenant-user limit reached. "
                f"Used {capacity['used']} of {capacity['limit']}. "
                f"Please purchase an additional tenant-user slot."
            ),
        )


def _require_customer_owned_by_admin(
    db: Session,
    owner_user_id: int,
    customer_name: str,
):
    found = (
        db.query(CustomerLocation.id)
        .filter(CustomerLocation.user_id == owner_user_id)
        .filter(CustomerLocation.customer_name.ilike(customer_name))
        .first()
    )
    if not found:
        raise HTTPException(
            status_code=400,
            detail="Customer not found for this admin user.",
        )


def _validate_dashboards_owned_by_admin_and_customer(
    db: Session,
    owner_user_id: int,
    customer_name: str,
    dashboard_ids: List[int],
) -> List[CustomerDashboard]:
    if not dashboard_ids:
        raise HTTPException(
            status_code=400,
            detail="At least one dashboard must be selected.",
        )

    rows = (
        db.query(CustomerDashboard)
        .filter(CustomerDashboard.user_id == owner_user_id)
        .filter(CustomerDashboard.customer_name.ilike(customer_name))
        .filter(CustomerDashboard.id.in_(dashboard_ids))
        .all()
    )

    valid_ids = {row.id for row in rows}
    requested_ids = {int(x) for x in dashboard_ids}

    if valid_ids != requested_ids:
        raise HTTPException(
            status_code=400,
            detail="One or more dashboards are invalid or not owned by this admin for the selected customer.",
        )

    return rows


def _sync_dashboard_access(
    db: Session,
    tenant_user_id: int,
    dashboard_ids: List[int],
):
    db.query(TenantUserDashboardAccess).filter(
        TenantUserDashboardAccess.tenant_user_id == tenant_user_id
    ).delete()

    for dash_id in dashboard_ids:
        db.add(
            TenantUserDashboardAccess(
                tenant_user_id=tenant_user_id,
                dashboard_id=int(dash_id),
                created_at=_now_utc(),
            )
        )


def _get_tenant_user_owned_by_admin(
    db: Session,
    tenant_user_id: int,
    owner_user_id: int,
) -> TenantUser:
    row = (
        db.query(TenantUser)
        .filter(TenantUser.id == tenant_user_id)
        .filter(TenantUser.owner_user_id == owner_user_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Tenant user not found.")
    return row


def _build_dashboard_public_links(rows: List[CustomerDashboard]) -> List[dict]:
    links = []
    public_base = _norm(PUBLIC_DASHBOARD_BASE_URL).rstrip("/")

    for row in rows or []:
        dashboard_name = (
            _norm(getattr(row, "dashboard_name", "")) or f"Dashboard {row.id}"
        )
        dashboard_slug = _norm(getattr(row, "dashboard_slug", ""))
        public_launch_id = _norm(getattr(row, "public_launch_id", ""))

        if not dashboard_slug or not public_launch_id:
            continue

        url = f"{public_base}/{dashboard_slug}/{public_launch_id}"

        links.append(
            {
                "name": dashboard_name,
                "url": url,
            }
        )

    return links


# =========================
# ✅ OPTIONAL SUMMARY ENDPOINT
# =========================
@router.get("/summary")
def get_tenant_user_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    capacity = _get_tenant_user_capacity(db, current_user.id)
    return {
        "ok": True,
        "availableTenantUsers": capacity["available"],
        "usedTenantUsers": capacity["used"],
        "tenantUsersLimit": capacity["limit"],
    }


# =========================
# ✅ CREATE TENANT USER
# =========================
@router.post("", response_model=TenantUserOut)
@router.post("/", response_model=TenantUserOut, include_in_schema=False)
def create_tenant_user(
    body: TenantUserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    full_name = _norm(body.name)
    email = _norm(body.email).lower()
    customer_name = _norm(body.customer_name)
    access = _validate_access(body.access)
    dashboard_ids = [int(x) for x in (body.dashboard_ids or [])]

    if not full_name:
        raise HTTPException(status_code=400, detail="Name is required.")
    if not email:
        raise HTTPException(status_code=400, detail="Email is required.")
    if not customer_name:
        raise HTTPException(status_code=400, detail="Customer is required.")

    _ensure_tenant_user_slot_available(db, current_user.id)

    _require_customer_owned_by_admin(db, current_user.id, customer_name)
    dashboard_rows = _validate_dashboards_owned_by_admin_and_customer(
        db=db,
        owner_user_id=current_user.id,
        customer_name=customer_name,
        dashboard_ids=dashboard_ids,
    )

    existing = (
        db.query(TenantUser)
        .filter(TenantUser.owner_user_id == current_user.id)
        .filter(TenantUser.email.ilike(email))
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail="A tenant user with this email already exists under this admin user.",
        )

    plain_password = _generate_password()
    hashed_password = _hash_password(plain_password)

    new_user = TenantUser(
        owner_user_id=current_user.id,
        customer_name=customer_name,
        full_name=full_name,
        email=email,
        password_hash=hashed_password,
        access_level=access,
        is_active=True,
        must_change_password=True,
        created_at=_now_utc(),
        updated_at=_now_utc(),
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    _sync_dashboard_access(
        db=db,
        tenant_user_id=new_user.id,
        dashboard_ids=[row.id for row in dashboard_rows],
    )
    db.commit()
    db.refresh(new_user)

    dashboard_links = _build_dashboard_public_links(dashboard_rows)

    email_sent = send_tenant_credentials_email(
        to_email=new_user.email,
        temporary_password=plain_password,
        tenant_name=new_user.full_name,
        dashboard_links=dashboard_links,
    )
    if not email_sent:
        print("❌ Tenant credentials email failed to send")

    return _serialize_tenant_user(new_user)


# =========================
# ✅ LIST TENANT USERS
# =========================
@router.get("", response_model=List[TenantUserOut])
@router.get("/", response_model=List[TenantUserOut], include_in_schema=False)
def list_tenant_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(TenantUser)
        .filter(TenantUser.owner_user_id == current_user.id)
        .order_by(TenantUser.id.desc())
        .all()
    )
    return [_serialize_tenant_user(row) for row in rows]


# =========================
# ✅ GET ONE TENANT USER
# =========================
@router.get("/{tenant_user_id}", response_model=TenantUserOut)
def get_tenant_user(
    tenant_user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_tenant_user_owned_by_admin(db, tenant_user_id, current_user.id)
    return _serialize_tenant_user(row)


# =========================
# ✅ UPDATE TENANT USER
# ✅ Email is immutable after create
# =========================
@router.put("/{tenant_user_id}", response_model=TenantUserOut)
def update_tenant_user(
    tenant_user_id: int,
    body: TenantUserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_tenant_user_owned_by_admin(db, tenant_user_id, current_user.id)

    full_name = _norm(body.name)
    email = _norm(body.email).lower()
    customer_name = _norm(body.customer_name)
    access = _validate_access(body.access)
    dashboard_ids = [int(x) for x in (body.dashboard_ids or [])]

    if not full_name:
        raise HTTPException(status_code=400, detail="Name is required.")
    if not email:
        raise HTTPException(status_code=400, detail="Email is required.")
    if not customer_name:
        raise HTTPException(status_code=400, detail="Customer is required.")

    if email != _norm(row.email).lower():
        raise HTTPException(
            status_code=400,
            detail="Email cannot be modified after the tenant user is created.",
        )

    _require_customer_owned_by_admin(db, current_user.id, customer_name)
    dashboard_rows = _validate_dashboards_owned_by_admin_and_customer(
        db=db,
        owner_user_id=current_user.id,
        customer_name=customer_name,
        dashboard_ids=dashboard_ids,
    )

    row.full_name = full_name
    row.customer_name = customer_name
    row.access_level = access
    row.updated_at = _now_utc()

    _sync_dashboard_access(
        db=db,
        tenant_user_id=row.id,
        dashboard_ids=[dash.id for dash in dashboard_rows],
    )

    db.commit()
    db.refresh(row)

    return _serialize_tenant_user(row)


# =========================
# ✅ DELETE TENANT USER
# =========================
@router.delete("/{tenant_user_id}")
def delete_tenant_user(
    tenant_user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_tenant_user_owned_by_admin(db, tenant_user_id, current_user.id)

    db.query(TenantUserDashboardAccess).filter(
        TenantUserDashboardAccess.tenant_user_id == row.id
    ).delete()

    db.delete(row)
    db.commit()

    return {"ok": True, "detail": "Tenant user deleted successfully."}