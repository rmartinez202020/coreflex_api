# routers/customer_locations.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional

from database import get_db
from models import CustomerLocation
from auth_utils import get_current_user


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
        from_attributes = True  # ✅ pydantic v2


# =========================
# ✅ GET: list current user's customers/locations
# =========================
@router.get("", response_model=List[CustomerLocationOut])
def list_customer_locations(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    rows = (
        db.query(CustomerLocation)
        .filter(CustomerLocation.user_id == current_user.id)
        .order_by(CustomerLocation.id.desc())
        .all()
    )
    return rows


# =========================
# ✅ POST: create a customer/location for current user
# =========================
@router.post("", response_model=CustomerLocationOut)
def create_customer_location(
    payload: CustomerLocationCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    # Basic validation (backend)
    if not payload.customer_name.strip():
        raise HTTPException(status_code=400, detail="customer_name is required")
    if not payload.site_name.strip():
        raise HTTPException(status_code=400, detail="site_name is required")
    if not payload.street.strip():
        raise HTTPException(status_code=400, detail="street is required")
    if not payload.city.strip():
        raise HTTPException(status_code=400, detail="city is required")
    if not payload.state.strip():
        raise HTTPException(status_code=400, detail="state is required")
    if not payload.zip.strip():
        raise HTTPException(status_code=400, detail="zip is required")

    row = CustomerLocation(
        user_id=current_user.id,
        customer_name=payload.customer_name.strip(),
        site_name=payload.site_name.strip(),
        street=payload.street.strip(),
        city=payload.city.strip(),
        state=payload.state.strip(),
        zip=payload.zip.strip(),
        country=(payload.country or "United States").strip(),
        notes=payload.notes.strip() if payload.notes else None,
        lat=payload.lat,
        lng=payload.lng,
    )

    db.add(row)
    db.commit()
    db.refresh(row)
    return row
