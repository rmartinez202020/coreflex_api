from fastapi import APIRouter, Depends, HTTPException, Query, Header
from sqlalchemy.orm import Session
from database import get_db
from auth_utils import get_current_user
import models
from datetime import datetime

router = APIRouter(prefix="/alarm-definitions", tags=["Alarm Definitions"])


# ==========================================
# SMALL HELPERS
# ==========================================
def StringOrNone(value):
    s = str(value).strip() if value is not None else ""
    return s or None


def _resolve_request_user_id(
    db: Session,
    current_user,
    tenant_email: str = "",
    dashboard_slug: str = "",
    public_launch_id: str = "",
):
    # ✅ Owner-auth mode
    if current_user is not None:
        return current_user.id

    # ✅ Public tenant mode
    email = StringOrNone(tenant_email)
    slug = StringOrNone(dashboard_slug)
    launch_id = StringOrNone(public_launch_id)

    if email:
        email = email.lower()

    if not email or not slug or not launch_id:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated and missing public tenant headers.",
        )

    dashboard = (
        db.query(models.CustomerDashboard)
        .filter(models.CustomerDashboard.dashboard_slug == slug)
        .filter(models.CustomerDashboard.public_launch_id == launch_id)
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
        db.query(models.TenantUser)
        .filter(models.TenantUser.owner_user_id == owner_user_id)
        .filter(models.TenantUser.email.ilike(email))
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
        db.query(models.TenantUserDashboardAccess)
        .filter(models.TenantUserDashboardAccess.tenant_user_id == tenant.id)
        .filter(models.TenantUserDashboardAccess.dashboard_id == dashboard_id)
        .first()
    )

    if not access_row:
        raise HTTPException(
            status_code=403,
            detail="Tenant user does not have access to this dashboard.",
        )

    return owner_user_id


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

    # 🔥 CRITICAL FIX: enforce alarm_log_key
    alarm_log_key = StringOrNone(payload.get("alarm_log_key")) or "alarmLog"

    if alarm_type == "DI":
        operator = None

    alarm = models.AlarmDefinition(
        user_id=current_user.id,
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

        # 🔥 IMPORTANT
        alarm_log_key=alarm_log_key,

        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )

    db.add(alarm)
    db.commit()
    db.refresh(alarm)

    return {
        "success": True,
        "alarm_id": alarm.id,
        "alarm_log_key": alarm.alarm_log_key,
    }


# ==========================================
# GET USER ALARMS (OWNER + PUBLIC TENANT SUPPORT)
# ==========================================
@router.get("/")
def get_user_alarm_definitions(
    alarm_log_key: str = Query("alarmLog"),
    db: Session = Depends(get_db),
    x_tenant_email: str | None = Header(default=None, alias="x-tenant-email"),
    x_dashboard_slug: str | None = Header(default=None, alias="x-dashboard-slug"),
    x_public_launch_id: str | None = Header(default=None, alias="x-public-launch-id"),
    current_user: models.User | None = Depends(get_current_user),
):
    request_user_id = _resolve_request_user_id(
        db=db,
        current_user=current_user,
        tenant_email=x_tenant_email or "",
        dashboard_slug=x_dashboard_slug or "",
        public_launch_id=x_public_launch_id or "",
    )

    alarms = (
        db.query(models.AlarmDefinition)
        .filter(models.AlarmDefinition.user_id == request_user_id)
        .filter(models.AlarmDefinition.alarm_log_key == alarm_log_key)
        .order_by(models.AlarmDefinition.id.asc())
        .all()
    )

    return alarms


# ==========================================
# UPDATE ONE USER ALARM DEFINITION
# ==========================================
@router.patch("/{alarm_id}")
def update_alarm_definition(
    alarm_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    alarm = (
        db.query(models.AlarmDefinition)
        .filter(models.AlarmDefinition.id == alarm_id)
        .filter(models.AlarmDefinition.user_id == current_user.id)
        .first()
    )

    if not alarm:
        raise HTTPException(status_code=404, detail="Alarm definition not found")

    alarm_type = StringOrNone(payload.get("alarm_type")) or StringOrNone(
        alarm.alarm_type
    ) or ""
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

    if "device_id" in payload:
        alarm.device_id = payload["device_id"]

    if "model" in payload:
        alarm.model = payload.get("model")

    if "tag" in payload:
        alarm.tag = payload["tag"]

    # 🔥 FIX: update log key safely
    if "alarm_log_key" in payload:
        alarm.alarm_log_key = (
            StringOrNone(payload.get("alarm_log_key")) or "alarmLog"
        )

    alarm.alarm_type = alarm_type
    alarm.contact_type = contact_type
    alarm.operator = operator
    alarm.threshold = threshold
    alarm.math_formula = math_formula

    if "group_name" in payload:
        alarm.group_name = payload.get("group_name")

    if "severity" in payload:
        alarm.severity = payload.get("severity")

    if "message" in payload:
        alarm.message = payload["message"]

    if "enabled" in payload:
        alarm.enabled = bool(payload.get("enabled"))

    alarm.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(alarm)

    return {
        "success": True,
        "alarm_id": alarm.id,
        "alarm_log_key": alarm.alarm_log_key,
    }


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