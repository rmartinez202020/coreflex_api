from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Any, Dict

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
class MainDashboardSaveRequest(BaseModel):
    layout: Dict[str, Any]


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

        if record:
            record.layout = payload.layout
        else:
            record = MainDashboard(
                user_id=current_user.id,
                layout=payload.layout,
            )
            db.add(record)

        db.commit()

        return {"status": "saved"}

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
        return {"layout": None}

    return {"layout": record.layout}

