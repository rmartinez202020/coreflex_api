from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Any, Dict
from datetime import datetime

from database import get_db
from models import User, MainDashboard
from auth_utils import get_current_user
from routers.log_engine import (
    send_log,
    LOG_CATEGORY_DASHBOARD,
    LOG_STATUS_SUCCESS,
)

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
# 🔧 Request Helpers
# =========================
def _get_request_user_agent(request: Request) -> str:
    try:
        return str(request.headers.get("user-agent", "") or "").strip()[:500]
    except Exception:
        return ""


def _get_request_ip(request: Request) -> str | None:
    try:
        forwarded = str(request.headers.get("x-forwarded-for", "") or "").strip()

        if forwarded:
            return forwarded.split(",")[0].strip()

        if request.client and request.client.host:
            return str(request.client.host).strip() or None

    except Exception:
        pass

    return None


# =========================
# 💾 SAVE MAIN DASHBOARD
# =========================
@router.post("/main")
def save_main_dashboard(
    payload: MainDashboardSaveRequest,
    request: Request,
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

        # =====================================================
        # LOGS & ACTIVITY
        # DASHBOARD -> MAIN DASHBOARD SAVE
        #
        # The Main Dashboard is always available, so we log
        # only SAVE activity here. We do not create separate
        # CREATE or DELETE events for the Main Dashboard.
        # =====================================================
        send_log(
            user_id=current_user.id,
            user_email=current_user.email,
            category=LOG_CATEGORY_DASHBOARD,
            action="DASHBOARD_SAVE",
            status=LOG_STATUS_SUCCESS,
            message="Main Dashboard saved",
            dashboard_id="main",
            field="dashboard",
            new_value={
                "dashboard_id": "main",
                "dashboard_name": "Main Dashboard",
                "dashboard_type": "MAIN",
            },
            ip_address=_get_request_ip(request),
            user_agent=_get_request_user_agent(request),
        )

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