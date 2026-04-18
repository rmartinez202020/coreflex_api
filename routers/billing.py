# routers/billing.py
import os
from decimal import Decimal, ROUND_HALF_UP

import stripe
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth_utils import get_current_user
from database import get_db
from models import User, BillingPlan, BillingAddon

router = APIRouter(prefix="/billing", tags=["Billing"])

STRIPE_SECRET_KEY = str(os.getenv("STRIPE_SECRET_KEY") or "").strip()
NJ_SALES_TAX_RATE = Decimal("0.06625")  # 6.625% used for actual tax calculation

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


def to_money_decimal(value) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid money amount.")


def quantize_decimal(value: Decimal, places: str = "0.01") -> Decimal:
    return Decimal(str(value or 0)).quantize(
        Decimal(places), rounding=ROUND_HALF_UP
    )


def money_to_cents(value: Decimal) -> int:
    return int(
        quantize_decimal(value, "0.01") * Decimal("100")
    )


def decimal_to_float_2(value: Decimal) -> float:
    return float(quantize_decimal(value, "0.01"))


def percent_display_2_from_rate(rate: Decimal) -> float:
    return float((Decimal(str(rate)) * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    ))


def rate_display_2_from_percent(percent_value: float) -> float:
    return float(
        (Decimal(str(percent_value)) / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


@router.get("/catalog")
def get_billing_catalog(
    db: Session = Depends(get_db),
):
    plans = (
        db.query(BillingPlan)
        .filter(BillingPlan.is_active.is_(True))
        .order_by(BillingPlan.sort_order.asc(), BillingPlan.id.asc())
        .all()
    )

    addons = (
        db.query(BillingAddon)
        .filter(BillingAddon.is_active.is_(True))
        .order_by(BillingAddon.id.asc())
        .all()
    )

    tax_rate_percent_display = percent_display_2_from_rate(NJ_SALES_TAX_RATE)
    tax_rate_display = rate_display_2_from_percent(tax_rate_percent_display)

    return {
        "ok": True,
        "plans": [
            {
                "id": p.id,
                "plan_key": p.plan_key,
                "plan_name": p.plan_name,
                "billing_type": p.billing_type,
                "price_usd": float(to_money_decimal(p.price_usd))
                if p.price_usd is not None
                else None,
                "currency": getattr(p, "currency", "usd"),
                "device_limit": p.device_limit,
                "tenant_user_limit": p.tenant_user_limit,
                "data_history_days": p.data_history_days,
                "sort_order": p.sort_order,
                "is_active": p.is_active,
                "stripe_product_id": p.stripe_product_id,
                "stripe_price_id": p.stripe_price_id,
            }
            for p in plans
        ],
        "addons": [
            {
                "id": a.id,
                "addon_key": a.addon_key,
                "billing_type": a.billing_type,
                "price_usd": float(to_money_decimal(a.price_usd))
                if a.price_usd is not None
                else None,
                "currency": getattr(a, "currency", "usd"),
                "is_active": a.is_active,
                "stripe_product_id": getattr(a, "stripe_product_id", None),
                "stripe_price_id": a.stripe_price_id,
            }
            for a in addons
        ],
        "tax": {
            "state": "NJ",
            "label": "NJ Sales Tax",
            "rate": tax_rate_display,
            "rate_percent": tax_rate_percent_display,
        },
    }


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

    addon = None
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

    try:
        plan_amount_usd = to_money_decimal(plan.price_usd)

        addon_unit_price_usd = Decimal("0.00")
        addon_amount_usd = Decimal("0.00")

        if extra_tenant_users > 0 and addon:
            addon_unit_price_usd = to_money_decimal(addon.price_usd)
            addon_amount_usd = quantize_decimal(
                addon_unit_price_usd * Decimal(extra_tenant_users),
                "0.01",
            )

        subtotal_usd = quantize_decimal(
            plan_amount_usd + addon_amount_usd,
            "0.01",
        )

        tax_amount_usd = quantize_decimal(
            subtotal_usd * NJ_SALES_TAX_RATE,
            "0.01",
        )

        total_usd = quantize_decimal(
            subtotal_usd + tax_amount_usd,
            "0.01",
        )

        amount_cents = money_to_cents(total_usd)

        if amount_cents <= 0:
            raise HTTPException(
                status_code=400,
                detail="Payment amount must be greater than zero.",
            )

        tax_rate_percent_display = percent_display_2_from_rate(NJ_SALES_TAX_RATE)
        tax_rate_display = rate_display_2_from_percent(tax_rate_percent_display)

        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="usd",
            payment_method_types=["card"],
            receipt_email=str(getattr(current_user, "email", "") or "").strip() or None,
            metadata={
                "user_id": str(current_user.id),
                "user_email": str(getattr(current_user, "email", "") or ""),
                "plan_key": plan_key,
                "billing_type": billing_type,
                "extra_tenant_users": str(extra_tenant_users),
                "tax_state": "NJ",
                "tax_rate": str(NJ_SALES_TAX_RATE),
                "plan_amount_usd": str(plan_amount_usd),
                "addon_amount_usd": str(addon_amount_usd),
                "subtotal_usd": str(subtotal_usd),
                "tax_amount_usd": str(tax_amount_usd),
                "total_usd": str(total_usd),
            },
        )

        return {
            "ok": True,
            "clientSecret": intent.client_secret,
            "amount": amount_cents,
            "currency": "usd",
            "planAmount": decimal_to_float_2(plan_amount_usd),
            "addonAmount": decimal_to_float_2(addon_amount_usd),
            "subtotal": decimal_to_float_2(subtotal_usd),
            "tax": decimal_to_float_2(tax_amount_usd),
            "taxRate": tax_rate_display,
            "taxRatePercent": tax_rate_percent_display,
            "taxLabel": "NJ Sales Tax",
            "total": decimal_to_float_2(total_usd),
        }

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)}")