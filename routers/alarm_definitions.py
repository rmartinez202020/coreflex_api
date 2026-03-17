from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from database import get_db
from auth_utils import get_current_user
import models
from datetime import datetime

router = APIRouter(prefix="/alarm-definitions", tags=["Alarm Definitions"])


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

    if alarm_type == "DI":
        operator = None

    alarm = models.AlarmDefinition(
        user_id=current_user.id,  # ✅ secure (not from frontend)
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
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )

    db.add(alarm)
    db.commit()
    db.refresh(alarm)

    return {
        "success": True,
        "alarm_id": alarm.id,
        "contact_type": alarm.contact_type,
        "math_formula": alarm.math_formula,
    }


# ==========================================
# GET USER ALARMS
# ==========================================
@router.get("/")
def get_user_alarm_definitions(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    alarms = (
        db.query(models.AlarmDefinition)
        .filter(models.AlarmDefinition.user_id == current_user.id)
        .order_by(models.AlarmDefinition.id.asc())
        .all()
    )

    return alarms


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


# ==========================================
# SMALL HELPER
# ==========================================
def StringOrNone(value):
    s = str(value).strip() if value is not None else ""
    return s or None