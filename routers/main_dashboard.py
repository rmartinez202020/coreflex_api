from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Any, Dict
from datetime import datetime

from database import get_db
from models import User, MainDashboard
from auth_utils import get_current_user

router = APIRouter(
    prefix="/dashboard",
    tags=["Main Dashboard"]
)

# =========================
# 📦 Request Schema
# =========================
# Accept FULL dashboard object (not just layout)
class MainDashboardSaveRequest(BaseModel):
    version: str
    type: str
    canvas: Dict[str, Any]
    meta: Dict[str, Any]


# =========================
# 💾 SAVE MAIN DASHBOARD
# =========================
@router.post("/main")
def save_main_dashboard(
    payload: MainDashboardSaveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        record = (
            db.query(MainDashboard)
            .filter(MainDashboard.user_id == current_user.id)
            .first()
        )

        dashboard_data = payload.model_dump()

        if record:
            record.layout = dashboard_data
            # 🔥 Always store UTC
            record.updated_at = datetime.utcnow()
        else:
            record = MainDashboard(
                user_id=current_user.id,
                layout=dashboard_data,
                updated_at=datetime.utcnow(),
            )
            db.add(record)

        db.commit()

        return {"success": True}

    except Exception as e:
        print("❌ SAVE MAIN DASHBOARD ERROR:", e)
        raise HTTPException(status_code=500, detail="Failed to save dashboard")


# =========================
# 📤 LOAD MAIN DASHBOARD
# =========================
@router.get("/main")
def load_main_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = (
        db.query(MainDashboard)
        .filter(MainDashboard.user_id == current_user.id)
        .first()
    )

    if not record:
        return {
            "layout": None,
            "updated_at": None,
        }

    return {
        "layout": record.layout,
        # 🔥 Send ISO string → frontend converts to user's local time
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }
