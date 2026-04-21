# routers/admin_subscriptions.py

import os
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from auth_utils import get_current_user
from database import get_db
from models import (
    User,
    UserSubscription,
    DeviceRegistry,
    TenantUser,
)

router = APIRouter(prefix="/admin/subscriptions", tags=["Admin Subscriptions"])


OWNER_EMAILS = {
    e.strip().lower()
    for e in str(os.getenv("COREFLEX_OWNER_EMAILS") or "").split(",")
    if e.strip()
}


def ensure_owner(current_user):
    email = str(getattr(current_user, "email", "") or "").strip().lower()
    if email not in OWNER_EMAILS:
        raise HTTPException(status_code=403, detail="Owner access required.")
    return current_user


def serialize_plan_label(plan_key: str) -> str:
    key = str(plan_key or "").strip().lower()
    mapping = {
        "free": "Free",
        "starter": "Starter",
        "professional": "Professional",
        "industrial": "Industrial",
        "enterprise": "Enterprise",
    }
    return mapping.get(key, key.title() if key else "Unknown")


class SubscriptionUpsertPayload(BaseModel):
    user_id: int
    plan_key: str = "free"
    device_limit: int = 1
    tenants_users_limit: int = 1
    active_date: Optional[datetime] = None
    renewal_date: Optional[datetime] = None
    is_active: bool = True


class SubscriptionUpdatePayload(BaseModel):
    plan_key: Optional[str] = None
    device_limit: Optional[int] = None
    tenants_users_limit: Optional[int] = None
    active_date: Optional[datetime] = None
    renewal_date: Optional[datetime] = None
    is_active: Optional[bool] = None


@router.get("")
def list_all_subscriptions(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ensure_owner(current_user)

    users = db.query(User).order_by(User.id.asc()).all()

    subscription_rows = (
        db.query(UserSubscription)
        .order_by(UserSubscription.user_id.asc())
        .all()
    )
    subscriptions_by_user_id = {row.user_id: row for row in subscription_rows}

    device_counts = dict(
        db.query(
            DeviceRegistry.claimed_by_user_id,
            func.count(DeviceRegistry.id),
        )
        .filter(DeviceRegistry.claimed_by_user_id.isnot(None))
        .group_by(DeviceRegistry.claimed_by_user_id)
        .all()
    )

    tenant_counts = dict(
        db.query(
            TenantUser.owner_user_id,
            func.count(TenantUser.id),
        )
        .group_by(TenantUser.owner_user_id)
        .all()
    )

    results = []
    for user in users:
        sub = subscriptions_by_user_id.get(user.id)

        results.append(
            {
                "user_id": user.id,
                "name": user.name,
                "email": user.email,
                "company": user.company,
                "has_subscription_row": bool(sub),
                "subscription_id": sub.id if sub else None,
                "plan_key": sub.plan_key if sub else "free",
                "plan_label": serialize_plan_label(sub.plan_key if sub else "free"),
                "device_limit": int(sub.device_limit if sub else 1),
                "tenants_users_limit": int(sub.tenants_users_limit if sub else 1),
                "active_date": sub.active_date if sub else None,
                "renewal_date": sub.renewal_date if sub else None,
                "is_active": bool(sub.is_active) if sub else False,
                "status": "Active" if (sub and sub.is_active) else "Inactive",
                "devices_used": int(device_counts.get(user.id, 0) or 0),
                "tenant_users_used": int(tenant_counts.get(user.id, 0) or 0),
                "device_over_limit": int(device_counts.get(user.id, 0) or 0) > int(sub.device_limit if sub else 1),
                "tenant_users_over_limit": int(tenant_counts.get(user.id, 0) or 0) > int(sub.tenants_users_limit if sub else 1),
                "created_at": sub.created_at if sub else None,
                "updated_at": sub.updated_at if sub else None,
            }
        )

    return {
        "total_users": len(results),
        "total_with_subscription_row": sum(1 for r in results if r["has_subscription_row"]),
        "items": results,
    }


@router.get("/{user_id}")
def get_admin_subscription_detail(
    user_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ensure_owner(current_user)

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    sub = (
        db.query(UserSubscription)
        .filter(UserSubscription.user_id == user_id)
        .first()
    )

    devices_used = (
        db.query(func.count(DeviceRegistry.id))
        .filter(DeviceRegistry.claimed_by_user_id == user_id)
        .scalar()
        or 0
    )

    tenant_users_used = (
        db.query(func.count(TenantUser.id))
        .filter(TenantUser.owner_user_id == user_id)
        .scalar()
        or 0
    )

    return {
        "user_id": user.id,
        "name": user.name,
        "email": user.email,
        "company": user.company,
        "subscription": {
            "subscription_id": sub.id if sub else None,
            "plan_key": sub.plan_key if sub else "free",
            "plan_label": serialize_plan_label(sub.plan_key if sub else "free"),
            "device_limit": int(sub.device_limit if sub else 1),
            "tenants_users_limit": int(sub.tenants_users_limit if sub else 1),
            "active_date": sub.active_date if sub else None,
            "renewal_date": sub.renewal_date if sub else None,
            "is_active": bool(sub.is_active) if sub else False,
            "created_at": sub.created_at if sub else None,
            "updated_at": sub.updated_at if sub else None,
        },
        "usage": {
            "devices_used": int(devices_used),
            "tenant_users_used": int(tenant_users_used),
        },
    }


@router.post("/upsert")
def upsert_admin_subscription(
    payload: SubscriptionUpsertPayload,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ensure_owner(current_user)

    user = db.query(User).filter(User.id == payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    sub = (
        db.query(UserSubscription)
        .filter(UserSubscription.user_id == payload.user_id)
        .first()
    )

    if not sub:
        sub = UserSubscription(
            user_id=payload.user_id,
            plan_key=str(payload.plan_key or "free").strip().lower(),
            device_limit=max(int(payload.device_limit or 0), 0),
            tenants_users_limit=max(int(payload.tenants_users_limit or 0), 0),
            active_date=payload.active_date or datetime.now(timezone.utc),
            renewal_date=payload.renewal_date,
            is_active=bool(payload.is_active),
        )
        db.add(sub)
    else:
        sub.plan_key = str(payload.plan_key or sub.plan_key or "free").strip().lower()
        sub.device_limit = max(int(payload.device_limit or 0), 0)
        sub.tenants_users_limit = max(int(payload.tenants_users_limit or 0), 0)
        sub.active_date = payload.active_date
        sub.renewal_date = payload.renewal_date
        sub.is_active = bool(payload.is_active)

    db.commit()
    db.refresh(sub)

    return {
        "message": "Subscription saved successfully.",
        "subscription": {
            "id": sub.id,
            "user_id": sub.user_id,
            "plan_key": sub.plan_key,
            "plan_label": serialize_plan_label(sub.plan_key),
            "device_limit": int(sub.device_limit or 0),
            "tenants_users_limit": int(sub.tenants_users_limit or 0),
            "active_date": sub.active_date,
            "renewal_date": sub.renewal_date,
            "is_active": bool(sub.is_active),
            "created_at": sub.created_at,
            "updated_at": sub.updated_at,
        },
    }


@router.put("/{user_id}")
def update_admin_subscription(
    user_id: int,
    payload: SubscriptionUpdatePayload,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ensure_owner(current_user)

    sub = (
        db.query(UserSubscription)
        .filter(UserSubscription.user_id == user_id)
        .first()
    )

    if not sub:
        raise HTTPException(
            status_code=404,
            detail="Subscription row not found for this user. Use /upsert first.",
        )

    if payload.plan_key is not None:
        sub.plan_key = str(payload.plan_key).strip().lower()

    if payload.device_limit is not None:
        sub.device_limit = max(int(payload.device_limit), 0)

    if payload.tenants_users_limit is not None:
        sub.tenants_users_limit = max(int(payload.tenants_users_limit), 0)

    if payload.active_date is not None:
        sub.active_date = payload.active_date

    if payload.renewal_date is not None:
        sub.renewal_date = payload.renewal_date

    if payload.is_active is not None:
        sub.is_active = bool(payload.is_active)

    db.commit()
    db.refresh(sub)

    return {
        "message": "Subscription updated successfully.",
        "subscription": {
            "id": sub.id,
            "user_id": sub.user_id,
            "plan_key": sub.plan_key,
            "plan_label": serialize_plan_label(sub.plan_key),
            "device_limit": int(sub.device_limit or 0),
            "tenants_users_limit": int(sub.tenants_users_limit or 0),
            "active_date": sub.active_date,
            "renewal_date": sub.renewal_date,
            "is_active": bool(sub.is_active),
            "created_at": sub.created_at,
            "updated_at": sub.updated_at,
        },
    }


@router.post("/{user_id}/deactivate")
def deactivate_admin_subscription(
    user_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ensure_owner(current_user)

    sub = (
        db.query(UserSubscription)
        .filter(UserSubscription.user_id == user_id)
        .first()
    )

    if not sub:
        raise HTTPException(status_code=404, detail="Subscription row not found.")

    sub.is_active = False
    db.commit()
    db.refresh(sub)

    return {
        "message": "Subscription deactivated successfully.",
        "user_id": sub.user_id,
        "is_active": bool(sub.is_active),
    }