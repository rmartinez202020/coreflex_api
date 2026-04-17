# routers/billing.py
import os

import stripe
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth_utils import get_current_user
from database import get_db
from models import User, BillingPlan, BillingAddon

router = APIRouter(prefix="/billing", tags=["Billing"])

STRIPE_SECRET_KEY = str(os.getenv("STRIPE_SECRET_KEY") or "").strip()

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


class CreatePaymentIntentRequest(BaseModel):
    planKey: str
    billingType: str
    extraTenantUsers: int = 0


def ensure_stripe_ready() -> None:
    if not STRIPE_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="Stripe is not configured. Missing STRIPE_SECRET_KEY.",
        )


def normalize_billing_type(value: str) -> str:
    v = str(value or "").strip().lower()
    if v not in {"monthly", "one_time"}:
        raise HTTPException(
            status_code=400,
            detail="billingType must be monthly or one_time.",
        )
    return v


@router.post("/create-payment-intent")
def create_payment_intent(
    payload: CreatePaymentIntentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_stripe_ready()

    plan_key = str(payload.planKey or "").strip().lower()
    billing_type = normalize_billing_type(payload.billingType)
    extra_tenant_users = max(0, int(payload.extraTenantUsers or 0))

    plan = (
        db.query(BillingPlan)
        .filter(
            BillingPlan.plan_key == plan_key,
            BillingPlan.billing_type == billing_type,
            BillingPlan.is_active.is_(True),
        )
        .first()
    )
    if not plan:
        raise HTTPException(status_code=404, detail="Billing plan not found.")

    plan_price_id = str(plan.stripe_price_id or "").strip()
    if not plan_price_id:
        raise HTTPException(
            status_code=400,
            detail="Selected plan is not synced to Stripe yet.",
        )

    line_items = [
        {
            "price": plan_price_id,
            "quantity": 1,
        }
    ]

    if extra_tenant_users > 0:
        addon = (
            db.query(BillingAddon)
            .filter(
                BillingAddon.addon_key == "tenant_user",
                BillingAddon.billing_type == billing_type,
                BillingAddon.is_active.is_(True),
            )
            .first()
        )
        if not addon:
            raise HTTPException(
                status_code=404,
                detail="Tenant-user addon not found.",
            )

        addon_price_id = str(addon.stripe_price_id or "").strip()
        if not addon_price_id:
            raise HTTPException(
                status_code=400,
                detail="Tenant-user addon is not synced to Stripe yet.",
            )

        line_items.append(
            {
                "price": addon_price_id,
                "quantity": extra_tenant_users,
            }
        )

    try:
        # Temporary PaymentIntent for Stripe Elements flow.
        # Amount must come from Stripe prices or your own trusted DB math.
        # For now, calculate from DB values.
        amount_usd = float(plan.price_usd or 0)

        if extra_tenant_users > 0:
            addon = (
                db.query(BillingAddon)
                .filter(
                    BillingAddon.addon_key == "tenant_user",
                    BillingAddon.billing_type == billing_type,
                    BillingAddon.is_active.is_(True),
                )
                .first()
            )
            if addon:
                amount_usd += float(addon.price_usd or 0) * extra_tenant_users

        amount_cents = int(round(amount_usd * 100))

        if amount_cents <= 0:
            raise HTTPException(
                status_code=400,
                detail="Payment amount must be greater than zero.",
            )

        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="usd",
            automatic_payment_methods={"enabled": True},
            receipt_email=str(getattr(current_user, "email", "") or "").strip() or None,
            metadata={
                "user_id": str(current_user.id),
                "user_email": str(getattr(current_user, "email", "") or ""),
                "plan_key": plan_key,
                "billing_type": billing_type,
                "extra_tenant_users": str(extra_tenant_users),
            },
        )

        return {
            "ok": True,
            "clientSecret": intent.client_secret,
            "amount": amount_cents,
            "currency": "usd",
        }

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)}")