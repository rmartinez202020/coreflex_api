# routers/device_registry.py

import re
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from auth_utils import get_current_user
from models import DeviceRegistry, User


router = APIRouter(prefix="/device-registry", tags=["Device Registry"])


# =========================================================
# Helpers
# =========================================================

MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def normalize_mac(mac: str) -> str:
    s = str(mac or "").strip().lower()
    s = s.replace("-", ":")
    s = re.sub(r"\s+", "", s)
    return s


def ensure_valid_mac(mac: str) -> str:
    normalized = normalize_mac(mac)
    if not MAC_RE.match(normalized):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid MAC format. Use format aa:bb:cc:dd:ee:ff",
        )
    return normalized


def ensure_owner(current_user: User):
    """
    Owner-only protection.
    Adjust later if you want admins or staff to use this too.
    """
    # If your app has a different field name, change here.
    # Common examples: current_user.role, current_user.is_admin, etc.
    user_email = str(getattr(current_user, "email", "") or "").strip().lower()

    if not user_email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    return current_user


# =========================================================
# Pydantic Schemas
# =========================================================

class DeviceRegistryCreate(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=64)
    device_model: str = Field(..., min_length=1, max_length=64)
    device_mac: str = Field(..., min_length=11, max_length=32)
    is_claimed: bool = False


class DeviceRegistryUpdate(BaseModel):
    device_model: Optional[str] = Field(default=None, max_length=64)
    device_mac: Optional[str] = Field(default=None, max_length=32)
    is_claimed: Optional[bool] = None
    claimed_by_user_id: Optional[int] = None


class DeviceRegistryOut(BaseModel):
    id: int
    device_id: str
    device_model: str
    device_mac: str
    claimed_by_user_id: Optional[int] = None
    is_claimed: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# =========================================================
# Routes
# =========================================================

@router.post(
    "",
    response_model=DeviceRegistryOut,
    status_code=status.HTTP_201_CREATED,
)
def create_device_registry_row(
    payload: DeviceRegistryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Register a new device in the central registry.
    This is the table that links:
      - device_id (serial)
      - device_model
      - device_mac
    """
    ensure_owner(current_user)

    clean_device_id = str(payload.device_id or "").strip()
    clean_device_model = str(payload.device_model or "").strip().lower()
    clean_device_mac = ensure_valid_mac(payload.device_mac)

    if not clean_device_id:
        raise HTTPException(status_code=400, detail="device_id is required")

    if not clean_device_model:
        raise HTTPException(status_code=400, detail="device_model is required")

    existing_by_id = (
        db.query(DeviceRegistry)
        .filter(DeviceRegistry.device_id == clean_device_id)
        .first()
    )
    if existing_by_id:
        raise HTTPException(
            status_code=409,
            detail=f"device_id already exists: {clean_device_id}",
        )

    existing_by_mac = (
        db.query(DeviceRegistry)
        .filter(DeviceRegistry.device_mac == clean_device_mac)
        .first()
    )
    if existing_by_mac:
        raise HTTPException(
            status_code=409,
            detail=f"device_mac already exists: {clean_device_mac}",
        )

    row = DeviceRegistry(
        device_id=clean_device_id,
        device_model=clean_device_model,
        device_mac=clean_device_mac,
        claimed_by_user_id=current_user.id if payload.is_claimed else None,
        is_claimed=bool(payload.is_claimed),
    )

    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("", response_model=List[DeviceRegistryOut])
def list_device_registry(
    device_model: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List device registry rows.
    Optional filter by model.
    """
    ensure_owner(current_user)

    q = db.query(DeviceRegistry)

    if device_model:
        q = q.filter(
            DeviceRegistry.device_model == str(device_model).strip().lower()
        )

    rows = q.order_by(DeviceRegistry.id.asc()).all()
    return rows


@router.get("/by-mac", response_model=DeviceRegistryOut)
def get_device_registry_by_mac(
    device_mac: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Manual lookup endpoint.
    Useful for testing matching logic.
    """
    ensure_owner(current_user)

    clean_mac = ensure_valid_mac(device_mac)

    row = (
        db.query(DeviceRegistry)
        .filter(DeviceRegistry.device_mac == clean_mac)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Device not found")

    return row


@router.get("/{registry_id}", response_model=DeviceRegistryOut)
def get_device_registry_row(
    registry_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_owner(current_user)

    row = (
        db.query(DeviceRegistry)
        .filter(DeviceRegistry.id == registry_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Registry row not found")

    return row


@router.patch("/{registry_id}", response_model=DeviceRegistryOut)
def update_device_registry_row(
    registry_id: int,
    payload: DeviceRegistryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_owner(current_user)

    row = (
        db.query(DeviceRegistry)
        .filter(DeviceRegistry.id == registry_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Registry row not found")

    if payload.device_model is not None:
        clean_device_model = str(payload.device_model or "").strip().lower()
        if not clean_device_model:
            raise HTTPException(status_code=400, detail="device_model cannot be empty")
        row.device_model = clean_device_model

    if payload.device_mac is not None:
        clean_mac = ensure_valid_mac(payload.device_mac)

        existing_by_mac = (
            db.query(DeviceRegistry)
            .filter(
                DeviceRegistry.device_mac == clean_mac,
                DeviceRegistry.id != registry_id,
            )
            .first()
        )
        if existing_by_mac:
            raise HTTPException(
                status_code=409,
                detail=f"device_mac already exists: {clean_mac}",
            )

        row.device_mac = clean_mac

    if payload.is_claimed is not None:
        row.is_claimed = bool(payload.is_claimed)

        if not row.is_claimed:
            row.claimed_by_user_id = None
        elif payload.claimed_by_user_id is not None:
            row.claimed_by_user_id = payload.claimed_by_user_id
        elif row.claimed_by_user_id is None:
            row.claimed_by_user_id = current_user.id

    elif payload.claimed_by_user_id is not None:
        row.claimed_by_user_id = payload.claimed_by_user_id

    if hasattr(row, "updated_at"):
        row.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(row)
    return row


@router.delete("/{registry_id}")
def delete_device_registry_row(
    registry_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_owner(current_user)

    row = (
        db.query(DeviceRegistry)
        .filter(DeviceRegistry.id == registry_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Registry row not found")

    db.delete(row)
    db.commit()

    return {"ok": True, "deleted_id": registry_id}