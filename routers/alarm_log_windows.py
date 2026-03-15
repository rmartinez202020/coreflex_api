from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from auth_utils import get_current_user
from models import User, AlarmLogWindow

router = APIRouter(prefix="/alarm-log-windows", tags=["Alarm Log Windows"])


class UpsertAlarmLogWindowBody(BaseModel):
    dashboard_id: str = "main"
    dashboard_name: str = "Main Dashboard"  # ✅ NEW
    window_key: str = "alarmLog"
    title: str = "Alarms Log (DI-AI)"
    pos_x: int = 140
    pos_y: int = 90
    width: int = 900
    height: int = 420
    is_open: bool = True
    is_minimized: bool = False
    is_launched: bool = False


# ✅ NEW: body for deleting the saved alarm log window row
class DeleteAlarmLogWindowBody(BaseModel):
    dashboard_id: str = "main"
    window_key: str = "alarmLog"


@router.post("/upsert")
def upsert_alarm_log_window(
    body: UpsertAlarmLogWindowBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    dashboard_id = str(body.dashboard_id or "main").strip() or "main"
    dashboard_name = str(body.dashboard_name or "").strip() or "Main Dashboard"
    window_key = str(body.window_key or "alarmLog").strip() or "alarmLog"

    print("🚨 ALARM LOG UPSERT HIT")
    print(
        "🚨 body =",
        {
            "dashboard_id": body.dashboard_id,
            "dashboard_name": body.dashboard_name,
            "window_key": body.window_key,
            "title": body.title,
            "pos_x": body.pos_x,
            "pos_y": body.pos_y,
            "width": body.width,
            "height": body.height,
            "is_open": body.is_open,
            "is_minimized": body.is_minimized,
            "is_launched": body.is_launched,
        },
    )
    print("🚨 current_user.id =", current_user.id)
    print("🚨 normalized dashboard_id =", dashboard_id)
    print("🚨 normalized dashboard_name =", dashboard_name)
    print("🚨 normalized window_key =", window_key)

    try:
        row = (
            db.query(AlarmLogWindow)
            .filter(
                AlarmLogWindow.user_id == current_user.id,
                AlarmLogWindow.dashboard_id == dashboard_id,
                AlarmLogWindow.window_key == window_key,
            )
            .first()
        )

        print("🚨 existing row found =", bool(row))

        if row:
            row.dashboard_name = dashboard_name  # ✅ NEW
            row.title = body.title
            row.pos_x = body.pos_x
            row.pos_y = body.pos_y
            row.width = body.width
            row.height = body.height
            row.is_open = body.is_open
            row.is_minimized = body.is_minimized
            row.is_launched = body.is_launched
            print("🚨 updated existing row in session")
        else:
            row = AlarmLogWindow(
                user_id=current_user.id,
                dashboard_id=dashboard_id,
                dashboard_name=dashboard_name,  # ✅ NEW
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
            print("🚨 created new row in session")

        print("🚨 before commit")
        db.commit()
        print("🚨 commit ok")

        print("🚨 before refresh")
        db.refresh(row)
        print("🚨 refresh ok")

        result = {
            "ok": True,
            "id": row.id,
            "user_id": row.user_id,
            "dashboard_id": row.dashboard_id,
            "dashboard_name": row.dashboard_name,  # ✅ NEW
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

        print("🚨 returning success =", result)
        return result

    except Exception as e:
        db.rollback()
        print("❌ alarm_log_windows upsert failed:", repr(e))
        raise HTTPException(
            status_code=500,
            detail=f"Alarm log upsert failed: {repr(e)}",
        )


# ✅ NEW: delete saved alarm log window row
@router.post("/delete")
def delete_alarm_log_window(
    body: DeleteAlarmLogWindowBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    dashboard_id = str(body.dashboard_id or "main").strip() or "main"
    window_key = str(body.window_key or "alarmLog").strip() or "alarmLog"

    print("🗑️ ALARM LOG DELETE HIT")
    print("🗑️ current_user.id =", current_user.id)
    print("🗑️ dashboard_id =", dashboard_id)
    print("🗑️ window_key =", window_key)

    try:
        row = (
            db.query(AlarmLogWindow)
            .filter(
                AlarmLogWindow.user_id == current_user.id,
                AlarmLogWindow.dashboard_id == dashboard_id,
                AlarmLogWindow.window_key == window_key,
            )
            .first()
        )

        print("🗑️ row found for delete =", bool(row))

        if not row:
            result = {
                "ok": True,
                "deleted": False,
                "message": "No alarm log window row found",
                "dashboard_id": dashboard_id,
                "window_key": window_key,
            }
            print("🗑️ returning =", result)
            return result

        db.delete(row)
        print("🗑️ row marked for delete")

        db.commit()
        print("🗑️ delete commit ok")

        result = {
            "ok": True,
            "deleted": True,
            "dashboard_id": dashboard_id,
            "window_key": window_key,
        }
        print("🗑️ returning =", result)
        return result

    except Exception as e:
        db.rollback()
        print("❌ alarm_log_windows delete failed:", repr(e))
        raise HTTPException(
            status_code=500,
            detail=f"Alarm log delete failed: {repr(e)}",
        )


@router.get("/by-dashboard")
def get_alarm_log_window(
    dashboard_id: str = "main",
    window_key: str = "alarmLog",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    dashboard_id = str(dashboard_id or "main").strip() or "main"
    window_key = str(window_key or "alarmLog").strip() or "alarmLog"

    print("🔎 ALARM LOG GET BY DASHBOARD HIT")
    print("🔎 current_user.id =", current_user.id)
    print("🔎 dashboard_id =", dashboard_id)
    print("🔎 window_key =", window_key)

    try:
        row = (
            db.query(AlarmLogWindow)
            .filter(
                AlarmLogWindow.user_id == current_user.id,
                AlarmLogWindow.dashboard_id == dashboard_id,
                AlarmLogWindow.window_key == window_key,
            )
            .first()
        )

        print("🔎 row found =", bool(row))

        if not row:
            return {"ok": True, "found": False}

        result = {
            "ok": True,
            "found": True,
            "id": row.id,
            "user_id": row.user_id,
            "dashboard_id": row.dashboard_id,
            "dashboard_name": row.dashboard_name,  # ✅ NEW
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

        print("🔎 returning row =", result)
        return result

    except Exception as e:
        print("❌ alarm_log_windows by-dashboard failed:", repr(e))
        raise HTTPException(
            status_code=500,
            detail=f"Alarm log fetch failed: {repr(e)}",
        )