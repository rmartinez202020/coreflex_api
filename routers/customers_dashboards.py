from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func as sa_func
from typing import Optional, List, Any, Dict
from datetime import datetime
import os

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


def _norm(s: Optional[str]) -> str:
    return (s or "").strip()


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

    return CustomerDashboardOut(
        id=row.id,
        user_id=row.user_id,
        customer_name=customer_name,
        dashboard_name=dashboard_name,
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

    row = CustomerDashboard(
        user_id=current_user.id,
        customer_name=customer_name,
        dashboard_name=dashboard_name,
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
    return [_serialize_dashboard(r) for r in rows]


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

    return [_serialize_dashboard(r) for r in rows]


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