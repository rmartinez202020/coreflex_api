# routers/radar_level_sensors.py
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
import os
import re

from database import get_db
from models import User
from auth_utils import get_current_user

router = APIRouter(prefix="/radar-level", tags=["Radar Level Sensors DF572"])

OWNER_EMAILS = {"roquemartinez_8@hotmail.com"}


def is_owner(user: User) -> bool:
    return (user.email or "").lower().strip() in OWNER_EMAILS


def normalize_imei(value: str) -> str:
    imei = re.sub(r"\D", "", str(value or "").strip())
    if not imei:
        raise HTTPException(status_code=400, detail="raw_imei_bytes is required")
    if len(imei) != 15:
        raise HTTPException(status_code=400, detail="IMEI must be exactly 15 digits")
    return imei


class AddSensorBody(BaseModel):
    raw_imei_bytes: str


class ClaimSensorBody(BaseModel):
    raw_imei_bytes: str


class TelemetryBody(BaseModel):
    raw_imei_bytes: str
    height_mm: int | None = None
    temperature_c: float | None = None
    battery_v: float | None = None


def row_to_dict(row):
    m = row._mapping
    return {
        "id": m.get("id"),
        "raw_imei_bytes": m.get("raw_imei_bytes"),
        "user_id": m.get("user_id"),
        "user_claimed_at": m.get("user_claimed_at"),

        # Current
        "height_mm": m.get("height_mm"),
        "received_at": m.get("received_at"),

        # Previous #1
        "height_2_mm": m.get("height_2_mm"),
        "received_at_2": m.get("received_at_2"),

        # Previous #2
        "height_3_mm": m.get("height_3_mm"),
        "received_at_3": m.get("received_at_3"),

        # Previous #3
        "height_4_mm": m.get("height_4_mm"),
        "received_at_4": m.get("received_at_4"),

        "temperature_c": float(m["temperature_c"])
        if m.get("temperature_c") is not None
        else None,

        "battery_v": float(m["battery_v"])
        if m.get("battery_v") is not None
        else None,

        "sensor_added_at": m.get("sensor_added_at"),
        "created_at": m.get("created_at"),
        "updated_at": m.get("updated_at"),
    }


# =====================================================
# OWNER ROUTES
# =====================================================
@router.get("/sensors")
def list_df572_sensors(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    rows = db.execute(
        text("""
            SELECT *
            FROM radar_level_sensors_data
            ORDER BY id ASC
        """)
    ).fetchall()

    return [row_to_dict(r) for r in rows]


@router.post("/sensors")
def add_df572_sensor(
    body: AddSensorBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    imei = normalize_imei(body.raw_imei_bytes)

    exists = db.execute(
        text("""
            SELECT id
            FROM radar_level_sensors_data
            WHERE raw_imei_bytes = :imei
            LIMIT 1
        """),
        {"imei": imei},
    ).fetchone()

    if exists:
        raise HTTPException(status_code=409, detail="sensor IMEI already exists")

    db.execute(
        text("""
            INSERT INTO radar_level_sensors_data (
                raw_imei_bytes,
                sensor_added_at,
                created_at,
                updated_at,
                received_at
            )
            VALUES (
                :imei,
                NOW(),
                NOW(),
                NOW(),
                NOW()
            )
        """),
        {"imei": imei},
    )

    db.commit()

    return {"ok": True, "raw_imei_bytes": imei, "added": True}


@router.delete("/sensors/{imei}")
def delete_df572_sensor(
    imei: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    imei = normalize_imei(imei)

    row = db.execute(
        text("""
            SELECT id
            FROM radar_level_sensors_data
            WHERE raw_imei_bytes = :imei
            LIMIT 1
        """),
        {"imei": imei},
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="sensor IMEI not found")

    db.execute(
        text("""
            DELETE FROM radar_level_sensors_data
            WHERE raw_imei_bytes = :imei
        """),
        {"imei": imei},
    )

    db.commit()

    return {"ok": True, "raw_imei_bytes": imei, "deleted": True}


# =====================================================
# USER CLAIM ROUTES
# =====================================================
@router.get("/my-sensors")
def my_df572_sensors(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = db.execute(
        text("""
            SELECT *
            FROM radar_level_sensors_data
            WHERE user_id = :uid
            ORDER BY id ASC
        """),
        {"uid": current_user.id},
    ).fetchall()

    return [row_to_dict(r) for r in rows]


@router.post("/claim")
def claim_df572_sensor(
    body: ClaimSensorBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    imei = normalize_imei(body.raw_imei_bytes)

    row = db.execute(
        text("""
            SELECT id, user_id
            FROM radar_level_sensors_data
            WHERE raw_imei_bytes = :imei
            LIMIT 1
        """),
        {"imei": imei},
    ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Sensor IMEI not found. Contact administrator.",
        )

    m = row._mapping
    existing_user_id = m.get("user_id")

    if existing_user_id is not None and int(existing_user_id) != int(current_user.id):
        raise HTTPException(
            status_code=409,
            detail="Sensor already claimed by another user.",
        )

    db.execute(
        text("""
            UPDATE radar_level_sensors_data
            SET
                user_id = :uid,
                user_claimed_at = NOW(),
                updated_at = NOW()
            WHERE raw_imei_bytes = :imei
        """),
        {
            "uid": current_user.id,
            "imei": imei,
        },
    )

    db.commit()

    return {
        "ok": True,
        "claimed": True,
        "raw_imei_bytes": imei,
        "user_id": current_user.id,
    }


@router.delete("/unclaim/{imei}")
def unclaim_df572_sensor(
    imei: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    imei = normalize_imei(imei)

    row = db.execute(
        text("""
            SELECT id, user_id
            FROM radar_level_sensors_data
            WHERE raw_imei_bytes = :imei
            LIMIT 1
        """),
        {"imei": imei},
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Sensor not found")

    m = row._mapping

    if m.get("user_id") is None or int(m.get("user_id")) != int(current_user.id):
        raise HTTPException(
            status_code=403,
            detail="You do not own this sensor",
        )

    db.execute(
        text("""
            UPDATE radar_level_sensors_data
            SET
                user_id = NULL,
                user_claimed_at = NULL,
                updated_at = NOW()
            WHERE raw_imei_bytes = :imei
        """),
        {"imei": imei},
    )

    db.commit()

    return {
        "ok": True,
        "unclaimed": True,
        "raw_imei_bytes": imei,
    }


# =====================================================
# NODE-RED TELEMETRY ROUTE
# =====================================================
@router.post("/telemetry")
def ingest_df572_telemetry(
    body: TelemetryBody,
    db: Session = Depends(get_db),
    x_telemetry_key: str | None = Header(default=None, alias="X-TELEMETRY-KEY"),
):
    required_key = (os.getenv("COREFLEX_TELEMETRY_KEY") or "").strip()
    if required_key and (x_telemetry_key or "").strip() != required_key:
        raise HTTPException(status_code=401, detail="Invalid telemetry key")

    imei = normalize_imei(body.raw_imei_bytes)

    row = db.execute(
        text("""
            SELECT
                id,
                raw_imei_bytes,
                user_id,
                user_claimed_at
            FROM radar_level_sensors_data
            WHERE raw_imei_bytes = :imei
            LIMIT 1
        """),
        {"imei": imei},
    ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Sensor IMEI not authorized. Owner must add sensor first.",
        )

    m = row._mapping

    if m.get("user_id") is None or m.get("user_claimed_at") is None:
        raise HTTPException(
            status_code=403,
            detail="Sensor exists but is not claimed by a user.",
        )

    db.execute(
        text("""
            UPDATE radar_level_sensors_data
            SET
                -- Shift history down
                height_4_mm = height_3_mm,
                received_at_4 = received_at_3,

                height_3_mm = height_2_mm,
                received_at_3 = received_at_2,

                height_2_mm = height_mm,
                received_at_2 = received_at,

                -- Save newest telemetry
                height_mm = :height_mm,
                temperature_c = :temperature_c,
                battery_v = :battery_v,
                received_at = NOW(),
                updated_at = NOW()

            WHERE raw_imei_bytes = :imei
        """),
        {
            "imei": imei,
            "height_mm": body.height_mm,
            "temperature_c": body.temperature_c,
            "battery_v": body.battery_v,
        },
    )

    db.commit()

    return {
        "ok": True,
        "raw_imei_bytes": imei,
        "user_id": m.get("user_id"),
        "updated": True,
    }