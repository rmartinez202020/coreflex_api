# routers/weight_scale_systems.py
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from auth_utils import get_current_user
from database import get_db
from models import User, WeightScaleSystem
router = APIRouter(
    prefix="/weight-scale-systems",
    tags=["Weight Scale Systems"],
)
# Only these users are allowed to read weight-scale data.
AUTHORIZED_EMAILS = {
    "roquemartinezpolanco@gmail.com",
    "roquemartinez_8@hotmail.com",
}
# Historical snapshot interval.
HISTORY_INTERVAL = timedelta(hours=6)
class WeightScaleTelemetryIn(BaseModel):
    device_ip: str
    device_port: int
    weight: float
def require_authorized_user(
    current_user: User = Depends(get_current_user),
) -> User:
    email = (current_user.email or "").strip().lower()
    if email not in AUTHORIZED_EMAILS:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to access weight scale systems.",
        )
    return current_user
def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """
    Normalize a database datetime to timezone-aware UTC.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
def row_to_dict(row: WeightScaleSystem) -> dict:
    """
    Convert one scale row to the API response format.
    One database row represents one physical scale connection:
    device_ip + device_port.
    """
    return {
        "id": row.id,
        "device_ip": row.device_ip,
        "device_port": row.device_port,

        # Current / latest reading
        "weight": row.weight,
        "received_at": row.received_at,

        # Approximately 6 hours ago
        "weight_2": row.weight_2,
        "received_at_2": row.received_at_2,

        # Approximately 12 hours ago
        "weight_3": row.weight_3,
        "received_at_3": row.received_at_3,

        # Approximately 18 hours ago
        "weight_4": row.weight_4,
        "received_at_4": row.received_at_4,

        # Approximately 24 hours ago
        "weight_5": row.weight_5,
        "received_at_5": row.received_at_5,

        "last_history_at": row.last_history_at,
    }


@router.post("/telemetry")
def receive_weight_scale_telemetry(
    payload: WeightScaleTelemetryIn,
    x_telemetry_key: Optional[str] = Header(
        default=None,
        alias="X-TELEMETRY-KEY",
    ),
    db: Session = Depends(get_db),
):
    """
    Receive a weight reading from Node-RED.

    Behavior:
    - One database row per device_ip + device_port.
    - Current weight is updated on every accepted telemetry message.
    - Every 6 hours, the previous current reading is moved into history.
    - Four historical snapshots are retained: approximately 6, 12, 18, and 24 hours.
    - No new database row is created for every reading.
    """
    required_key = (os.getenv("COREFLEX_TELEMETRY_KEY") or "").strip()

    if required_key:
        if (x_telemetry_key or "").strip() != required_key:
            raise HTTPException(
                status_code=401,
                detail="Invalid telemetry key",
            )

    device_ip = (payload.device_ip or "").strip()

    if not device_ip:
        raise HTTPException(
            status_code=400,
            detail="device_ip is required",
        )

    if payload.device_port < 1 or payload.device_port > 65535:
        raise HTTPException(
            status_code=400,
            detail="device_port must be between 1 and 65535",
        )

    now = datetime.now(timezone.utc)

    try:
        row = (
            db.query(WeightScaleSystem)
            .filter(
                WeightScaleSystem.device_ip == device_ip,
                WeightScaleSystem.device_port == payload.device_port,
            )
            .with_for_update()
            .first()
        )

        # ==========================================================
        # FIRST READING FOR THIS SCALE
        # ==========================================================
        if row is None:
            row = WeightScaleSystem(
                device_ip=device_ip,
                device_port=payload.device_port,
                weight=payload.weight,
                received_at=now,
                last_history_at=now,
            )

            db.add(row)
            db.commit()
            db.refresh(row)

            return {
                "ok": True,
                "created": True,
                "history_saved": False,
                "reading": row_to_dict(row),
            }

        # ==========================================================
        # EXISTING SCALE
        # ==========================================================
        last_history_at = as_utc(row.last_history_at)

        if last_history_at is None:
            last_history_at = as_utc(row.received_at) or now
            row.last_history_at = last_history_at

        history_due = (now - last_history_at) >= HISTORY_INTERVAL

        # ==========================================================
        # EVERY 6 HOURS:
        # current -> ~6 hours ago
        # ~6 hours -> ~12 hours
        # ~12 hours -> ~18 hours
        # ~18 hours -> ~24 hours
        # ==========================================================
        if history_due:
            row.weight_5 = row.weight_4
            row.received_at_5 = row.received_at_4

            row.weight_4 = row.weight_3
            row.received_at_4 = row.received_at_3

            row.weight_3 = row.weight_2
            row.received_at_3 = row.received_at_2

            row.weight_2 = row.weight
            row.received_at_2 = row.received_at

            row.last_history_at = now

        # Always update current reading.
        row.weight = payload.weight
        row.received_at = now

        db.commit()
        db.refresh(row)

    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        print("Weight scale telemetry save failed:", repr(exc))
        raise HTTPException(
            status_code=500,
            detail="Failed to save weight scale telemetry",
        )

    return {
        "ok": True,
        "created": False,
        "history_saved": history_due,
        "reading": row_to_dict(row),
    }


@router.get("/readings")
def get_weight_scale_readings(
    device_ip: Optional[str] = Query(default=None),
    device_port: Optional[int] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_authorized_user),
):
    """
    Return scale rows.
    Optional filters:
    - device_ip
    - device_port
    Because there is only one row per device_ip + device_port,
    this endpoint does not return an unlimited telemetry history.
    """
    query = db.query(WeightScaleSystem)
    if device_ip:
        query = query.filter(
            WeightScaleSystem.device_ip == device_ip.strip()
        )
    if device_port is not None:
        query = query.filter(
            WeightScaleSystem.device_port == device_port
        )
    rows = (
        query
        .order_by(WeightScaleSystem.received_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "ok": True,
        "count": len(rows),
        "readings": [
            row_to_dict(row)
            for row in rows
        ],
    }
@router.get("/latest")
def get_latest_weight_scale_reading(
    device_ip: str = Query(...),
    device_port: int = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_authorized_user),
):
    """
    Return the current reading and the four previous
    6-hour snapshots for one scale connection.
    """
    clean_ip = (device_ip or "").strip()
    if not clean_ip:
        raise HTTPException(
            status_code=400,
            detail="device_ip is required",
        )
    if device_port < 1 or device_port > 65535:
        raise HTTPException(
            status_code=400,
            detail="device_port must be between 1 and 65535",
        )
    row = (
        db.query(WeightScaleSystem)
        .filter(
            WeightScaleSystem.device_ip == clean_ip,
            WeightScaleSystem.device_port == device_port,
        )
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="No weight scale found for this device_ip and device_port",
        )
    return {
        "ok": True,
        "reading": row_to_dict(row),
    }
