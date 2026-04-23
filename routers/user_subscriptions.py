# routers/user_subscriptions.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db
from auth_utils import get_current_user
from models import UserSubscription, DeviceRegistry, TenantUser

router = APIRouter(prefix="/subscription", tags=["subscription"])


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


@router.get("/me")
def get_my_subscription(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        subscription = (
            db.query(UserSubscription)
            .filter(
                UserSubscription.user_id == current_user.id,
                UserSubscription.is_active.is_(True),
            )
            .order_by(UserSubscription.id.desc())
            .first()
        )

        if not subscription:
            raise HTTPException(
                status_code=404,
                detail="No active subscription found for this user.",
            )

        devices_used = (
            db.query(func.count(DeviceRegistry.id))
            .filter(DeviceRegistry.claimed_by_user_id == current_user.id)
            .scalar()
            or 0
        )

        tenant_users_used = (
            db.query(func.count(TenantUser.id))
            .filter(TenantUser.owner_user_id == current_user.id)
            .scalar()
            or 0
        )

        subscription_status = str(
            getattr(subscription, "subscription_status", "") or ""
        ).strip()

        cancel_at_period_end = bool(
            getattr(subscription, "cancel_at_period_end", False)
        )

        display_status = (
            "Canceled"
            if cancel_at_period_end
            else (
                subscription_status.title()
                if subscription_status
                else ("Active" if bool(subscription.is_active) else "Inactive")
            )
        )

        return {
            "user_id": current_user.id,
            "plan_key": subscription.plan_key,
            "plan_label": serialize_plan_label(subscription.plan_key),
            "status": display_status,
            "subscription_status": subscription_status or None,
            "is_active": bool(subscription.is_active),
            "cancel_at_period_end": cancel_at_period_end,
            "active_date": subscription.active_date,
            "renewal_date": subscription.renewal_date,
            "current_period_start": getattr(subscription, "current_period_start", None),
            "current_period_end": getattr(subscription, "current_period_end", None),
            "updated_at": getattr(subscription, "updated_at", None),
            "device_limit": int(subscription.device_limit or 0),
            "tenants_users_limit": int(subscription.tenants_users_limit or 0),
            "devices_used": int(devices_used),
            "tenant_users_used": int(tenant_users_used),
            "stripe_customer_id": getattr(subscription, "stripe_customer_id", None),
            "stripe_subscription_id": getattr(
                subscription, "stripe_subscription_id", None
            ),
            "stripe_price_id": getattr(subscription, "stripe_price_id", None),
            "last_invoice_id": getattr(subscription, "last_invoice_id", None),
        }

    except HTTPException:
        raise

    except Exception as e:
        print("🔥 SUBSCRIPTION /me ERROR:", e)
        raise HTTPException(status_code=500, detail="Internal server error")