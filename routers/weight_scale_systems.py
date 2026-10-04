# routers/weight_scale_systems.py

import os
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
    "roquemartinez@gmail.com",
    "roquemartinez_8@hotmail.com",
}


class WeightScaleTelemetryIn(BaseModel):
    device_ip: str
    device_port: int
    weight: float
    unit: str
    mode: Optional[str] = None


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

    Node-RED authenticates with X-TELEMETRY-KEY.
    Each accepted reading creates one row in weight_scale_systems.
    """

    required_key = (os.getenv("COREFLEX_TELEMETRY_KEY") or "").strip()

    if required_key:
        if (x_telemetry_key or "").strip() != required_key:
            raise HTTPException(
                status_code=401,
                detail="Invalid telemetry key",
            )

    device_ip = (payload.device_ip or "").strip()
    unit = (payload.unit or "").strip().lower()
    mode = (payload.mode or "").strip().lower() or None

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

    if not unit:
        raise HTTPException(
            status_code=400,
            detail="unit is required",
        )

    row = WeightScaleSystem(
        device_ip=device_ip,
        device_port=payload.device_port,
        weight=payload.weight,
        unit=unit,
        mode=mode,
    )

    try:
        db.add(row)
        db.commit()
        db.refresh(row)
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Failed to save weight scale telemetry",
        )

    return {
        "ok": True,
        "id": row.id,
        "device_ip": row.device_ip,
        "device_port": row.device_port,
        "weight": row.weight,
        "unit": row.unit,
        "mode": row.mode,
        "timestamp": row.timestamp,
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
    Read stored weight-scale telemetry.

    Access is restricted to the authorized user accounts.
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
        .order_by(WeightScaleSystem.timestamp.desc())
        .limit(limit)
        .all()
    )

    return {
        "ok": True,
        "count": len(rows),
        "readings": [
            {
                "id": row.id,
                "device_ip": row.device_ip,
                "device_port": row.device_port,
                "weight": row.weight,
                "unit": row.unit,
                "mode": row.mode,
                "timestamp": row.timestamp,
            }
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
    Return the latest stored reading for one scale connection.
    """

    row = (
        db.query(WeightScaleSystem)
        .filter(
            WeightScaleSystem.device_ip == device_ip.strip(),
            WeightScaleSystem.device_port == device_port,
        )
        .order_by(WeightScaleSystem.timestamp.desc())
        .first()
    )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="No weight scale readings found",
        )

    return {
        "ok": True,
        "reading": {
            "id": row.id,
            "device_ip": row.device_ip,
            "device_port": row.device_port,
            "weight": row.weight,
            "unit": row.unit,
            "mode": row.mode,
            "timestamp": row.timestamp,
        },
    }
