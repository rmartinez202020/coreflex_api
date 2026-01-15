from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional, List
from datetime import datetime

from database import get_db
from models import CustomerLocation, User
from auth_utils import get_current_user

# ✅ backend geocoder helpers
from utils import geocode_address, build_address_string

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

    # Optional control:
    force_geocode: Optional[bool] = False


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

    geocode_status: Optional[str] = None
    geocoded_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# =========================
# 🔧 helpers
# =========================
def _norm(s: Optional[str]) -> str:
    return (s or "").strip()


def _address_changed(row: CustomerLocation, body: CustomerLocationCreate) -> bool:
    return any(
        [
            _norm(row.street) != _norm(body.street),
            _norm(row.city) != _norm(body.city),
            _norm(row.state) != _norm(body.state),
            _norm(row.zip) != _norm(body.zip),
            _norm(row.country) != _norm(body.country or "United States"),
        ]
    )


def _apply_body(row: CustomerLocation, body: CustomerLocationCreate) -> None:
    row.customer_name = _norm(body.customer_name)
    row.site_name = _norm(body.site_name)
    row.street = _norm(body.street)
    row.city = _norm(body.city)
    row.state = _norm(body.state)
    row.zip = _norm(body.zip)
    row.country = _norm(body.country or "United States")
    row.notes = _norm(body.notes) if body.notes is not None else None


def _maybe_geocode(row: CustomerLocation, force: bool = False) -> None:
    """
    Geocode using backend service and store results.
    - Never crash create/update if geocode fails
    - Only overwrite lat/lng if geocode succeeds
    - Always update geocode_status + geocoded_at
    """

    # If we already have coords and not forcing, skip
    if not force and row.lat is not None and row.lng is not None:
        return

    try:
        # ✅ build_address_string expects positional args (per your logs)
        addr = build_address_string(
            _norm(row.street),
            _norm(row.city),
            _norm(row.state),
            _norm(row.zip),          # zip_code
            _norm(row.country) or "United States",
        )
    except Exception as e:
        print("❌ build_address_string failed:", repr(e))
        row.geocode_status = "error"
        row.geocoded_at = datetime.utcnow()
        return

    if not addr or not str(addr).strip():
        row.geocode_status = "error"
        row.geocoded_at = datetime.utcnow()
        return

    try:
        lat, lng, status, display_name = geocode_address(addr)
        row.geocode_status = status
        row.geocoded_at = datetime.utcnow()

        if status == "ok" and lat is not None and lng is not None:
            row.lat = lat
            row.lng = lng
        # else: keep existing lat/lng if geocode fails

    except Exception as e:
        print("❌ geocode_address failed:", repr(e))
        row.geocode_status = "error"
        row.geocoded_at = datetime.utcnow()
        # keep any existing lat/lng


# =========================
# ✅ LIST (current user only)
# Support both /customer-locations and /customer-locations/
# =========================
@router.get("", response_model=List[CustomerLocationOut])
@router.get("/", response_model=List[CustomerLocationOut], include_in_schema=False)
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
# Support both /customer-locations and /customer-locations/
# =========================
@router.post("", response_model=CustomerLocationOut)
@router.post("/", response_model=CustomerLocationOut, include_in_schema=False)
def create_customer_location(
    body: CustomerLocationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = CustomerLocation(user_id=current_user.id)
    _apply_body(row, body)

    # ✅ Geocode on create (but non-blocking)
    _maybe_geocode(row, force=True)

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

    addr_changed = _address_changed(row, body)
    _apply_body(row, body)

    if body.force_geocode or addr_changed or row.lat is None or row.lng is None:
        _maybe_geocode(row, force=True)

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
