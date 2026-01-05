from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Any, Dict, Optional
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
    """
    Saves ONE main dashboard per user_id (MainDashboard.user_id is PK).
    """
    try:
        # ✅ DEBUG: confirm backend is using the correct authenticated user
        print("✅ SAVE /dashboard/main USER:", current_user.id, current_user.email)

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

        return {
            "success": True,
            # ✅ DEBUG: echo back who we saved for
            "user_id": current_user.id,
            "email": current_user.email,
        }

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
    """
    Loads the authenticated user's main dashboard.
    """
    # ✅ DEBUG: confirm backend is using the correct authenticated user
    print("✅ LOAD /dashboard/main USER:", current_user.id, current_user.email)

    record = (
        db.query(MainDashboard)
        .filter(MainDashboard.user_id == current_user.id)
        .first()
    )

    if not record:
        return {
            # ✅ DEBUG: echo back who we tried to load for
            "user_id": current_user.id,
            "email": current_user.email,
            "layout": None,
            "updated_at": None,
        }

    return {
        # ✅ DEBUG: echo back who we loaded for
        "user_id": current_user.id,
        "email": current_user.email,

        # existing payload
        "layout": record.layout,
        # 🔥 Send ISO string → frontend converts to user's local time
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }
