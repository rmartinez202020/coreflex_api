from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func as sa_func
from typing import Optional, List, Any, Dict
from datetime import datetime

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
def _norm(s: Optional[str]) -> str:
    return (s or "").strip()


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
        layout={"version": "1.0", "canvas": {"objects": []}, "meta": {"savedAt": None}},
    )

    db.add(row)
    db.commit()
    db.refresh(row)
    return row


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

    return q.all()


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
    return row


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
    return row


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
