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
        print("✅ SAVE /dashboard/main USER:", current_user.id, current_user.email)

        record = (
            db.query(MainDashboard)
            .filter(MainDashboard.user_id == current_user.id)
            .first()
        )

        dashboard_data = payload.model_dump()

        if record:
            record.layout = dashboard_data
            record.updated_at = datetime.utcnow()
        else:
            record = MainDashboard(
                user_id=current_user.id,
                layout=dashboard_data,
                updated_at=datetime.utcnow(),
            )
            db.add(record)

        db.commit()

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
            "user_id": current_user.id,
            "email": current_user.email,
        }

    except Exception as e:
        print("❌ SAVE MAIN DASHBOARD ERROR:", e)
        raise HTTPException(status_code=500, detail="Failed to save dashboard")


# =========================
# ♻️ RESTORE MAIN DASHBOARD
# =========================
@router.post("/main/restore")
def restore_main_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Manual restore endpoint for the authenticated user's Main Dashboard.

    This route is only for the explicit Restore Project action.
    Normal GET /dashboard/main remains for load/refresh/auto-restore.
    """
    try:
        print(
            "♻️ RESTORE /dashboard/main/restore USER:",
            current_user.id,
            current_user.email,
        )

        record = (
            db.query(MainDashboard)
            .filter(MainDashboard.user_id == current_user.id)
            .first()
        )

        if not record:
            return {
                "success": False,
                "user_id": current_user.id,
                "email": current_user.email,
                "layout": None,
                "updated_at": None,
                "message": "No saved Main Dashboard found",
            }

        send_log(
            user_id=current_user.id,
            user_email=current_user.email,
            category=LOG_CATEGORY_DASHBOARD,
            action="DASHBOARD_RESTORE",
            status=LOG_STATUS_SUCCESS,
            message="Main Dashboard restored",
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
            "user_id": current_user.id,
            "email": current_user.email,
            "layout": record.layout,
            "updated_at": (
                record.updated_at.isoformat()
                if record.updated_at
                else None
            ),
        }

    except Exception as e:
        print("❌ RESTORE MAIN DASHBOARD ERROR:", e)
        raise HTTPException(status_code=500, detail="Failed to restore dashboard")


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
    print("✅ LOAD /dashboard/main USER:", current_user.id, current_user.email)

    record = (
        db.query(MainDashboard)
        .filter(MainDashboard.user_id == current_user.id)
        .first()
    )

    if not record:
        return {
            "user_id": current_user.id,
            "email": current_user.email,
            "layout": None,
            "updated_at": None,
        }

    return {
        "user_id": current_user.id,
        "email": current_user.email,
        "layout": record.layout,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }