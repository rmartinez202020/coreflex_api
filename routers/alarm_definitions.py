from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from database import get_db
from auth_utils import get_current_user
import models
from datetime import datetime

router = APIRouter(prefix="/alarm-definitions", tags=["Alarm Definitions"])


# ==========================================
# SMALL HELPER
# ==========================================
def StringOrNone(value):
    s = str(value).strip() if value is not None else ""
    return s or None


# ==========================================
# CREATE ALARM DEFINITION
# ==========================================
@router.post("/")
def create_alarm_definition(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    alarm_type = StringOrNone(payload.get("alarm_type")) or ""
    alarm_type = alarm_type.upper()

    contact_type = StringOrNone(payload.get("contact_type"))
    if alarm_type == "DI":
        contact_type = (contact_type or "NO").upper()
        if contact_type not in {"NO", "NC"}:
            contact_type = "NO"
    else:
        contact_type = None

    math_formula = StringOrNone(payload.get("math_formula"))
    if not math_formula:
        math_formula = None

    operator = StringOrNone(payload.get("operator"))
    threshold = payload.get("threshold")

    # 🔥 CRITICAL FIX: enforce alarm_log_key
    alarm_log_key = StringOrNone(payload.get("alarm_log_key")) or "alarmLog"

    if alarm_type == "DI":
        operator = None

    alarm = models.AlarmDefinition(
        user_id=current_user.id,
        device_id=payload["device_id"],
        model=payload.get("model"),
        tag=payload["tag"],
        alarm_type=alarm_type,
        contact_type=contact_type,
        operator=operator,
        threshold=threshold,
        math_formula=math_formula,
        group_name=payload.get("group_name"),
        severity=payload.get("severity"),
        message=payload["message"],
        enabled=payload.get("enabled", True),

        # 🔥 IMPORTANT
        alarm_log_key=alarm_log_key,

        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )

    db.add(alarm)
    db.commit()
    db.refresh(alarm)

    return {
        "success": True,
        "alarm_id": alarm.id,
        "alarm_log_key": alarm.alarm_log_key,
    }


# ==========================================
# GET USER ALARMS (FIXED 🔥)
# ==========================================
@router.get("/")
def get_user_alarm_definitions(
    alarm_log_key: str = Query("alarmLog"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    alarms = (
        db.query(models.AlarmDefinition)
        .filter(models.AlarmDefinition.user_id == current_user.id)
        .filter(models.AlarmDefinition.alarm_log_key == alarm_log_key)  # 🔥 FIX
        .order_by(models.AlarmDefinition.id.asc())
        .all()
    )

    return alarms


# ==========================================
# UPDATE ONE USER ALARM DEFINITION
# ==========================================
@router.patch("/{alarm_id}")
def update_alarm_definition(
    alarm_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    alarm = (
        db.query(models.AlarmDefinition)
        .filter(models.AlarmDefinition.id == alarm_id)
        .filter(models.AlarmDefinition.user_id == current_user.id)
        .first()
    )

    if not alarm:
        raise HTTPException(status_code=404, detail="Alarm definition not found")

    alarm_type = StringOrNone(payload.get("alarm_type")) or StringOrNone(
        alarm.alarm_type
    ) or ""
    alarm_type = alarm_type.upper()

    contact_type = StringOrNone(payload.get("contact_type"))
    if alarm_type == "DI":
        contact_type = (contact_type or "NO").upper()
        if contact_type not in {"NO", "NC"}:
            contact_type = "NO"
    else:
        contact_type = None

    math_formula = StringOrNone(payload.get("math_formula"))
    if not math_formula:
        math_formula = None

    operator = StringOrNone(payload.get("operator"))
    threshold = payload.get("threshold")

    if alarm_type == "DI":
        operator = None

    if "device_id" in payload:
        alarm.device_id = payload["device_id"]

    if "model" in payload:
        alarm.model = payload.get("model")

    if "tag" in payload:
        alarm.tag = payload["tag"]

    # 🔥 FIX: update log key safely
    if "alarm_log_key" in payload:
        alarm.alarm_log_key = (
            StringOrNone(payload.get("alarm_log_key")) or "alarmLog"
        )

    alarm.alarm_type = alarm_type
    alarm.contact_type = contact_type
    alarm.operator = operator
    alarm.threshold = threshold
    alarm.math_formula = math_formula

    if "group_name" in payload:
        alarm.group_name = payload.get("group_name")

    if "severity" in payload:
        alarm.severity = payload.get("severity")

    if "message" in payload:
        alarm.message = payload["message"]

    if "enabled" in payload:
        alarm.enabled = bool(payload.get("enabled"))

    alarm.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(alarm)

    return {
        "success": True,
        "alarm_id": alarm.id,
        "alarm_log_key": alarm.alarm_log_key,
    }


# ==========================================
# DELETE USER ALARM DEFINITIONS
# ==========================================
@router.delete("/")
def delete_alarm_definitions(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    raw_ids = payload.get("ids") or []

    ids = []
    for value in raw_ids:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue

    if not ids:
        return {
            "success": True,
            "deleted_count": 0,
            "deleted_ids": [],
        }

    rows = (
        db.query(models.AlarmDefinition)
        .filter(models.AlarmDefinition.user_id == current_user.id)
        .filter(models.AlarmDefinition.id.in_(ids))
        .all()
    )

    deleted_ids = [row.id for row in rows]

    for row in rows:
        db.delete(row)

    db.commit()

    return {
        "success": True,
        "deleted_count": len(deleted_ids),
        "deleted_ids": deleted_ids,
    }