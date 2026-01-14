from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional, List

from database import get_db
from models import CustomerLocation
from auth_utils import get_current_user
from models import User

router = APIRouter(prefix="/customer-locations", tags=["Customer Locations"])


# =========================
# 📦 Schemas
# =========================
class CustomerLocationCreate(BaseModel):
    customer_name: str
    site_name: str
    street: str
    city: str
    state: str
    zip: str
    country: Optional[str] = "United States"
    notes: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


class CustomerLocationOut(BaseModel):
    id: int
    user_id: int
    customer_name: str
    site_name: str
    street: str
    city: str
    state: str
    zip: str
    country: str
    notes: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None

    class Config:
        from_attributes = True


# =========================
# ✅ LIST (current user only)
# =========================
@router.get("", response_model=List[CustomerLocationOut])
def list_customer_locations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(CustomerLocation)
        .filter(CustomerLocation.user_id == current_user.id)
        .order_by(CustomerLocation.id.desc())
        .all()
    )


# =========================
# ✅ CREATE (current user)
# =========================
@router.post("", response_model=CustomerLocationOut)
def create_customer_location(
    body: CustomerLocationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = CustomerLocation(
        user_id=current_user.id,
        customer_name=body.customer_name,
        site_name=body.site_name,
        street=body.street,
        city=body.city,
        state=body.state,
        zip=body.zip,
        country=body.country or "United States",
        notes=body.notes,
        lat=body.lat,
        lng=body.lng,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# =========================
# ✅ UPDATE (current user only)
# =========================
@router.put("/{location_id}", response_model=CustomerLocationOut)
def update_customer_location(
    location_id: int,
    body: CustomerLocationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(CustomerLocation)
        .filter(CustomerLocation.id == location_id)
        .filter(CustomerLocation.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Customer location not found")

    row.customer_name = body.customer_name
    row.site_name = body.site_name
    row.street = body.street
    row.city = body.city
    row.state = body.state
    row.zip = body.zip
    row.country = body.country or "United States"
    row.notes = body.notes
    row.lat = body.lat
    row.lng = body.lng

    db.commit()
    db.refresh(row)
    return row


# =========================
# ✅ DELETE (current user only)
# =========================
@router.delete("/{location_id}")
def delete_customer_location(
    location_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(CustomerLocation)
        .filter(CustomerLocation.id == location_id)
        .filter(CustomerLocation.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Customer location not found")

    db.delete(row)
    db.commit()
    return {"ok": True, "deleted_id": location_id}
