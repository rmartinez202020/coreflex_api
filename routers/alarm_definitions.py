from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from database import get_db
import models
from datetime import datetime

router = APIRouter(prefix="/alarm-definitions", tags=["Alarm Definitions"])


@router.post("/")
def create_alarm_definition(payload: dict, db: Session = Depends(get_db)):
    alarm = models.AlarmDefinition(
        user_id=payload["user_id"],
        device_id=payload["device_id"],
        model=payload.get("model"),
        tag=payload["tag"],
        alarm_type=payload["alarm_type"],
        operator=payload.get("operator"),
        threshold=payload.get("threshold"),
        math_formula=payload.get("math_formula"),
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

    return {"success": True, "alarm_id": alarm.id}