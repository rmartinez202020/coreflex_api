# routers/alarm_history.py

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
import requests
from sqlalchemy.orm import Session

from auth_utils import get_current_user_optional
from database import get_db
from models import User, CustomerDashboard, TenantUser, TenantUserDashboardAccess

router = APIRouter(prefix="/alarm-history", tags=["Alarm History"])

NODE_RED_READ_URL = "http://98.90.225.131:1880/alarm-history/read"
NODE_RED_COMMAND_KEY = "CFX_k29sLx92Jd8s1Qp4NzT7MartinezVx93LwQa2"


class AlarmHistoryReadBody(BaseModel):
    alarm_log_key: str


def _norm(v):
    return str(v or "").strip()


def _resolve_request_user_id(
    db: Session,
    current_user: User | None,
    tenant_email: str = "",
    dashboard_slug: str = "",
    public_launch_id: str = "",
):
    # ✅ Owner-auth mode
    if current_user is not None:
        return current_user.id

    # ✅ Public tenant mode
    email = _norm(tenant_email).lower()
    slug = _norm(dashboard_slug)
    launch_id = _norm(public_launch_id)

    if not email or not slug or not launch_id:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated and missing public tenant headers.",
        )

    dashboard = (
        db.query(CustomerDashboard)
        .filter(CustomerDashboard.dashboard_slug == slug)
        .filter(CustomerDashboard.public_launch_id == launch_id)
        .first()
    )

    if not dashboard:
        raise HTTPException(
            status_code=404,
            detail="Public dashboard not found or no longer available.",
        )

    owner_user_id = getattr(dashboard, "user_id", None)
    dashboard_id = getattr(dashboard, "id", None)

    if owner_user_id is None or dashboard_id is None:
        raise HTTPException(
            status_code=500,
            detail="Dashboard configuration is invalid.",
        )

    tenant = (
        db.query(TenantUser)
        .filter(TenantUser.owner_user_id == owner_user_id)
        .filter(TenantUser.email.ilike(email))
        .first()
    )

    if not tenant:
        raise HTTPException(
            status_code=403,
            detail="Tenant user is not authorized for this dashboard.",
        )

    if not bool(getattr(tenant, "is_active", True)):
        raise HTTPException(
            status_code=403,
            detail="Tenant user is inactive.",
        )

    access_row = (
        db.query(TenantUserDashboardAccess)
        .filter(TenantUserDashboardAccess.tenant_user_id == tenant.id)
        .filter(TenantUserDashboardAccess.dashboard_id == dashboard_id)
        .first()
    )

    if not access_row:
        raise HTTPException(
            status_code=403,
            detail="Tenant user does not have access to this dashboard.",
        )

    return owner_user_id


@router.post("/read")
def read_alarm_history(
    body: AlarmHistoryReadBody,
    db: Session = Depends(get_db),
    x_tenant_email: str | None = Header(default=None, alias="x-tenant-email"),
    x_dashboard_slug: str | None = Header(default=None, alias="x-dashboard-slug"),
    x_public_launch_id: str | None = Header(default=None, alias="x-public-launch-id"),
    current_user: User | None = Depends(get_current_user_optional),
):
    alarm_log_key = str(body.alarm_log_key or "").strip()

    if not alarm_log_key:
        raise HTTPException(status_code=400, detail="alarm_log_key is required")

    request_user_id = _resolve_request_user_id(
        db=db,
        current_user=current_user,
        tenant_email=x_tenant_email or "",
        dashboard_slug=x_dashboard_slug or "",
        public_launch_id=x_public_launch_id or "",
    )

    try:
        res = requests.post(
            NODE_RED_READ_URL,
            json={
                "user_id": request_user_id,
                "alarm_log_key": alarm_log_key,
            },
            headers={
                "Content-Type": "application/json",
                "x-command-key": NODE_RED_COMMAND_KEY,
            },
            timeout=5,
        )

        # ✅ If Node-RED/file is not ready yet, treat it as empty history
        if not res.ok:
            status = int(res.status_code or 0)
            text = str(res.text or "").strip().lower()

            missing_markers = [
                "enoent",
                "no such file",
                "not found",
                "file does not exist",
                "cannot find the file",
                "missing",
            ]

            if status in (404, 204):
                return []

            if any(marker in text for marker in missing_markers):
                return []

            raise HTTPException(
                status_code=502,
                detail=f"Node-RED read failed: {res.status_code} {res.text[:300]}",
            )

        try:
            data = res.json()
        except Exception:
            raw_text = str(getattr(res, "text", "") or "").strip()
            if raw_text == "":
                return []

            raise HTTPException(
                status_code=502,
                detail="Node-RED returned non-JSON response",
            )

        # ✅ Normalize safe payloads to a list
        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            if isinstance(data.get("items"), list):
                return data.get("items") or []

            if isinstance(data.get("history"), list):
                return data.get("history") or []

            if isinstance(data.get("rows"), list):
                return data.get("rows") or []

            if data.get("success") is True and (
                data.get("items") is None
                or data.get("history") is None
                or data.get("rows") is None
            ):
                msg = str(
                    data.get("message")
                    or data.get("detail")
                    or data.get("error")
                    or ""
                ).strip().lower()

                missing_markers = [
                    "enoent",
                    "no such file",
                    "not found",
                    "file does not exist",
                    "cannot find the file",
                    "missing",
                    "no history",
                    "empty",
                ]

                if not msg or any(marker in msg for marker in missing_markers):
                    return []

        raise HTTPException(
            status_code=502,
            detail="Node-RED returned invalid history payload",
        )

    except HTTPException:
        raise
    except requests.Timeout:
        return []
    except requests.RequestException as e:
        raise HTTPException(
            status_code=502,
            detail=f"Alarm history request failed: {str(e)}",
        )