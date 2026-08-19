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

# ✅ Logs & Activity
from routers.log_engine import (
    send_log,
    LOG_CATEGORY_USER,
    LOG_STATUS_SUCCESS,
)

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

    # ✅ NEW: Optional manual pin (free / reliable)
    lat: Optional[float] = None
    lng: Optional[float] = None

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

    # ✅ If user pinned manually, persist coords and mark as manual
    if body.lat is not None and body.lng is not None:
        row.lat = float(body.lat)
        row.lng = float(body.lng)
        row.geocode_status = "manual"
        row.geocoded_at = datetime.utcnow()


def _maybe_geocode(row: CustomerLocation, force: bool = False) -> None:
    """
    Geocode using backend service and store results.
    - Never crash create/update if geocode fails
    - Only overwrite lat/lng if geocode succeeds
    - Always update geocode_status + geocoded_at
    """

    # ✅ If user already pinned manually, never geocode over it
    if row.geocode_status == "manual" and row.lat is not None and row.lng is not None:
        return

    # If we already have coords and not forcing, skip
    if not force and row.lat is not None and row.lng is not None:
        return

    try:
        # ✅ build_address_string expects positional args
        addr = build_address_string(
            _norm(row.street),
            _norm(row.city),
            _norm(row.state),
            _norm(row.zip),  # zip_code
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


def _customer_log_value(row: CustomerLocation) -> dict:
    """
    Snapshot customer/location information for Logs & Activity.
    """
    return {
        "customer_location_id": row.id,
        "customer_name": _norm(row.customer_name),
        "site_name": _norm(row.site_name),
        "street": _norm(row.street),
        "city": _norm(row.city),
        "state": _norm(row.state),
        "zip": _norm(row.zip),
        "country": _norm(row.country),
        "notes": row.notes,
        "lat": row.lat,
        "lng": row.lng,
        "geocode_status": row.geocode_status,
    }


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

    # ✅ If user didn't pin manually, try geocoding (non-blocking)
    if row.lat is None or row.lng is None:
        _maybe_geocode(row, force=True)

    db.add(row)
    db.commit()
    db.refresh(row)

    # =========================
    # 📝 LOGS & ACTIVITY
    # USER -> CUSTOMER_CREATE
    # =========================
    send_log(
        user_id=current_user.id,
        user_email=current_user.email,
        category=LOG_CATEGORY_USER,
        action="CUSTOMER_CREATE",
        status=LOG_STATUS_SUCCESS,
        message=(
            f"Customer created: {_norm(row.customer_name)}"
            f" | Site: {_norm(row.site_name)}"
        ),
        customer_id=row.id,
        field="customer_location",
        new_value=_customer_log_value(row),
    )

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

    # Preserve full OLD customer/location information before changing anything.
    old_value = _customer_log_value(row)

    addr_changed = _address_changed(row, body)
    _apply_body(row, body)

    # ✅ If user pinned manually (lat/lng provided), DO NOT geocode
    # Otherwise geocode when requested/changed/missing coords
    if row.lat is None or row.lng is None:
        if body.force_geocode or addr_changed:
            _maybe_geocode(row, force=True)

    db.commit()
    db.refresh(row)

    # =========================
    # 📝 LOGS & ACTIVITY
    # USER -> CUSTOMER_UPDATE
    # =========================
    send_log(
        user_id=current_user.id,
        user_email=current_user.email,
        category=LOG_CATEGORY_USER,
        action="CUSTOMER_UPDATE",
        status=LOG_STATUS_SUCCESS,
        message=(
            f"Customer updated: {_norm(row.customer_name)}"
            f" | Site: {_norm(row.site_name)}"
        ),
        customer_id=row.id,
        field="customer_location",
        old_value=old_value,
        new_value=_customer_log_value(row),
    )

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

    # Preserve customer/location information BEFORE deleting the row.
    deleted_value = _customer_log_value(row)
    deleted_customer_name = _norm(row.customer_name)
    deleted_site_name = _norm(row.site_name)

    db.delete(row)
    db.commit()

    # =========================
    # 📝 LOGS & ACTIVITY
    # USER -> CUSTOMER_DELETE
    # =========================
    send_log(
        user_id=current_user.id,
        user_email=current_user.email,
        category=LOG_CATEGORY_USER,
        action="CUSTOMER_DELETE",
        status=LOG_STATUS_SUCCESS,
        message=(
            f"Customer deleted: {deleted_customer_name}"
            f" | Site: {deleted_site_name}"
        ),
        customer_id=location_id,
        field="customer_location",
        old_value=deleted_value,
        new_value=None,
    )

    return {"ok": True, "deleted_id": location_id}