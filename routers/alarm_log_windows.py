from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from auth_utils import get_current_user
from models import User, AlarmLogWindow

router = APIRouter(prefix="/alarm-log-windows", tags=["Alarm Log Windows"])


class UpsertAlarmLogWindowBody(BaseModel):
    dashboard_id: str = "main"
    window_key: str = "alarmLog"
    title: str = "Alarms Log (DI-AI)"
    pos_x: int = 140
    pos_y: int = 90
    width: int = 900
    height: int = 420
    is_open: bool = True
    is_minimized: bool = False
    is_launched: bool = False


@router.post("/upsert")
def upsert_alarm_log_window(
    body: UpsertAlarmLogWindowBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    dashboard_id = str(body.dashboard_id or "main").strip()
    window_key = str(body.window_key or "alarmLog").strip() or "alarmLog"

    row = (
        db.query(AlarmLogWindow)
        .filter(
            AlarmLogWindow.user_id == current_user.id,
            AlarmLogWindow.dashboard_id == dashboard_id,
            AlarmLogWindow.window_key == window_key,
        )
        .first()
    )

    if row:
        row.title = body.title
        row.pos_x = body.pos_x
        row.pos_y = body.pos_y
        row.width = body.width
        row.height = body.height
        row.is_open = body.is_open
        row.is_minimized = body.is_minimized
        row.is_launched = body.is_launched
    else:
        row = AlarmLogWindow(
            user_id=current_user.id,
            dashboard_id=dashboard_id,
            window_key=window_key,
            title=body.title,
            pos_x=body.pos_x,
            pos_y=body.pos_y,
            width=body.width,
            height=body.height,
            is_open=body.is_open,
            is_minimized=body.is_minimized,
            is_launched=body.is_launched,
        )
        db.add(row)

    db.commit()
    db.refresh(row)

    return {
        "ok": True,
        "id": row.id,
        "user_id": row.user_id,
        "dashboard_id": row.dashboard_id,
        "window_key": row.window_key,
        "title": row.title,
        "pos_x": row.pos_x,
        "pos_y": row.pos_y,
        "width": row.width,
        "height": row.height,
        "is_open": row.is_open,
        "is_minimized": row.is_minimized,
        "is_launched": row.is_launched,
    }


@router.get("/by-dashboard")
def get_alarm_log_window(
    dashboard_id: str = "main",
    window_key: str = "alarmLog",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(AlarmLogWindow)
        .filter(
            AlarmLogWindow.user_id == current_user.id,
            AlarmLogWindow.dashboard_id == dashboard_id,
            AlarmLogWindow.window_key == window_key,
        )
        .first()
    )

    if not row:
        return {"ok": True, "found": False}

    return {
        "ok": True,
        "found": True,
        "id": row.id,
        "user_id": row.user_id,
        "dashboard_id": row.dashboard_id,
        "window_key": row.window_key,
        "title": row.title,
        "pos_x": row.pos_x,
        "pos_y": row.pos_y,
        "width": row.width,
        "height": row.height,
        "is_open": row.is_open,
        "is_minimized": row.is_minimized,
        "is_launched": row.is_launched,
    }