# routers/billing.py
import os
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth_utils import get_current_user
from database import get_db
from models import User, BillingPlan, BillingAddon, UserSubscription

router = APIRouter(prefix="/billing", tags=["Billing"])

STRIPE_SECRET_KEY = str(os.getenv("STRIPE_SECRET_KEY") or "").strip()
STRIPE_WEBHOOK_SECRET = str(os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()

# Frontend URLs for hosted Stripe Checkout redirects
FRONTEND_BASE_URL = str(
    os.getenv("COREFLEX_FRONTEND_URL")
    or os.getenv("FRONTEND_URL")
    or "http://localhost:3000"
).strip().rstrip("/")

STRIPE_CHECKOUT_SUCCESS_URL = str(
    os.getenv("STRIPE_CHECKOUT_SUCCESS_URL")
    or f"{FRONTEND_BASE_URL}/app?payment=success&session_id={{CHECKOUT_SESSION_ID}}"
).strip()

STRIPE_CHECKOUT_CANCEL_URL = str(
    os.getenv("STRIPE_CHECKOUT_CANCEL_URL")
    or f"{FRONTEND_BASE_URL}/?payment=cancel"
).strip()

NJ_SALES_TAX_RATE = Decimal("0.06625")  # 6.625% used for actual tax calculation

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


class CreatePaymentIntentRequest(BaseModel):
    planKey: str
    billingType: str
    extraTenantUsers: int = 0


class CreateCheckoutSessionRequest(BaseModel):
    planKey: str
    billingType: str
    extraTenantUsers: int = 0


class ApplyPaymentRequest(BaseModel):
    paymentIntentId: str


def ensure_stripe_ready() -> None:
    if not STRIPE_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="Stripe is not configured. Missing STRIPE_SECRET_KEY.",
        )


def ensure_stripe_webhook_ready() -> None:
    ensure_stripe_ready()
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Stripe webhook is not configured. Missing STRIPE_WEBHOOK_SECRET.",
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
    return int(quantize_decimal(value, "0.01") * Decimal("100"))


def decimal_to_float_2(value: Decimal) -> float:
    return float(quantize_decimal(value, "0.01"))


def percent_display_2_from_rate(rate: Decimal) -> float:
    return float(
        (Decimal(str(rate)) * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def rate_display_2_from_percent(percent_value: float) -> float:
    return float(
        (Decimal(str(percent_value)) / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def _utcnow():
    return datetime.now(timezone.utc)


def _safe_int(value, default=0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _safe_metadata_dict(value) -> dict:
    if value is None:
        return {}

    if isinstance(value, dict):
        return dict(value)

    try:
        if hasattr(value, "to_dict_recursive"):
            converted = value.to_dict_recursive()
            if isinstance(converted, dict):
                return converted
    except Exception:
        pass

    try:
        keys = value.keys()
        return {str(k): value[k] for k in keys}
    except Exception:
        pass

    try:
        return dict(value)
    except Exception:
        return {}


def _describe_exception(exc: Exception) -> str:
    try:
        text = str(exc).strip()
        if text:
            return f"{type(exc).__name__}: {text}"
        return type(exc).__name__
    except Exception:
        return exc.__class__.__name__


def _log_debug(prefix: str, **kwargs) -> None:
    try:
        print(prefix)
        for key, value in kwargs.items():
            print(f"   {key}: {value}")
    except Exception:
        pass


def _merge_metadata(*items) -> dict:
    merged = {}
    for item in items:
        d = _safe_metadata_dict(item)
        for key, value in d.items():
            if value is None:
                continue
            text = str(value).strip()
            if text == "":
                continue
            merged[str(key)] = text
    return merged


def _normalize_payment_metadata(db: Session, metadata: dict) -> dict:
    md = _safe_metadata_dict(metadata)

    raw_user_id = str(md.get("user_id") or "").strip()
    if raw_user_id.isdigit():
        md["user_id"] = raw_user_id
        return md

    raw_user_email = str(md.get("user_email") or "").strip().lower()
    if raw_user_email:
        user = db.query(User).filter(User.email == raw_user_email).first()
        if user:
            md["user_id"] = str(user.id)
            md["user_email"] = raw_user_email
            print("✅ RESOLVED user_id FROM user_email:", raw_user_email, "->", user.id)
            return md

    return md


def _mark_payment_intent_applied(payment_intent_id: str, metadata: dict) -> None:
    try:
        stripe.PaymentIntent.modify(
            payment_intent_id,
            metadata={
                **_safe_metadata_dict(metadata),
                "applied": "true",
            },
        )
        print("✅ STRIPE METADATA UPDATED applied=true", payment_intent_id)
    except stripe.error.StripeError as e:
        print(
            "⚠️ FAILED TO UPDATE STRIPE PAYMENTINTENT METADATA",
            payment_intent_id,
            _describe_exception(e),
        )


def _get_or_create_user_subscription(db: Session, user_id: int) -> UserSubscription:
    row = (
        db.query(UserSubscription)
        .filter(UserSubscription.user_id == user_id)
        .first()
    )
    if row:
        return row

    row = UserSubscription(
        user_id=user_id,
        plan_key="free",
        device_limit=1,
        tenants_users_limit=1,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _resolve_plan_and_addon_for_purchase(
    db: Session,
    plan_key: str,
    billing_type: str,
    extra_tenant_users: int,
):
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

    return plan, addon


def _build_purchase_context(
    db: Session,
    current_user: User,
    plan_key: str,
    billing_type: str,
    extra_tenant_users: int,
):
    subscription = _get_or_create_user_subscription(db, current_user.id)
    current_plan_key = str(subscription.plan_key or "free").strip().lower()
    is_current_plan = current_plan_key == plan_key

    plan, addon = _resolve_plan_and_addon_for_purchase(
        db=db,
        plan_key=plan_key,
        billing_type=billing_type,
        extra_tenant_users=extra_tenant_users,
    )

    if is_current_plan:
        plan_amount_usd = Decimal("0.00")
    else:
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

    tax_rate_percent_display = percent_display_2_from_rate(NJ_SALES_TAX_RATE)
    tax_rate_display = rate_display_2_from_percent(tax_rate_percent_display)

    metadata = {
        "user_id": str(current_user.id or ""),
        "user_email": str(getattr(current_user, "email", "") or ""),
        "plan_key": str(plan_key or ""),
        "current_plan_key": str(current_plan_key or ""),
        "is_current_plan": "true" if is_current_plan else "false",
        "billing_type": str(billing_type or ""),
        "extra_tenant_users": str(extra_tenant_users or 0),
        "tax_state": "NJ",
        "tax_rate": str(NJ_SALES_TAX_RATE),
        "plan_amount_usd": str(plan_amount_usd),
        "addon_amount_usd": str(addon_amount_usd),
        "subtotal_usd": str(subtotal_usd),
        "tax_amount_usd": str(tax_amount_usd),
        "total_usd": str(total_usd),
        "applied": "false",
    }

    return {
        "subscription": subscription,
        "current_plan_key": current_plan_key,
        "is_current_plan": is_current_plan,
        "plan": plan,
        "addon": addon,
        "plan_amount_usd": plan_amount_usd,
        "addon_unit_price_usd": addon_unit_price_usd,
        "addon_amount_usd": addon_amount_usd,
        "subtotal_usd": subtotal_usd,
        "tax_amount_usd": tax_amount_usd,
        "total_usd": total_usd,
        "amount_cents": amount_cents,
        "tax_rate": tax_rate_display,
        "tax_rate_percent": tax_rate_percent_display,
        "metadata": metadata,
    }


def _apply_payment_effects(
    db: Session,
    *,
    payment_intent_id: str,
    metadata: dict,
):
    print("🔥 APPLY PAYMENT EFFECTS START")
    _log_debug(
        "🔥 APPLY PAYMENT EFFECTS INPUT",
        payment_intent_id=payment_intent_id,
        metadata=metadata,
    )

    metadata = _normalize_payment_metadata(db, metadata)

    raw_user_id = str(metadata.get("user_id") or "").strip()
    if not raw_user_id.isdigit():
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid payment metadata: user_id. "
                f"metadata={metadata}"
            ),
        )

    user_id = int(raw_user_id)
    plan_key = str(metadata.get("plan_key") or "free").strip().lower()

    raw_billing_type = str(metadata.get("billing_type") or "").strip().lower()
    if raw_billing_type not in {"monthly", "one_time"}:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid billing_type in metadata: {raw_billing_type}",
        )
    billing_type = raw_billing_type

    is_current_plan = (
        str(metadata.get("is_current_plan") or "").strip().lower() == "true"
    )
    extra_tenant_users = max(0, _safe_int(metadata.get("extra_tenant_users"), 0))
    already_applied = str(metadata.get("applied") or "").strip().lower() == "true"

    _log_debug(
        "🔥 PARSED PAYMENT METADATA",
        user_id=user_id,
        plan_key=plan_key,
        billing_type=billing_type,
        is_current_plan=is_current_plan,
        extra_tenant_users=extra_tenant_users,
        already_applied=already_applied,
    )

    subscription = _get_or_create_user_subscription(db, user_id)

    if already_applied:
        print("✅ PAYMENT ALREADY MARKED APPLIED IN STRIPE METADATA")
        return {
            "ok": True,
            "alreadyApplied": True,
            "added": 0,
            "planKey": str(subscription.plan_key or "free").strip().lower(),
            "tenantsUsersLimit": int(subscription.tenants_users_limit or 0),
            "tenantUsersUsed": None,
            "message": "Payment was already applied earlier.",
        }

    print("🔥 LOOKING FOR PLAN:", plan_key, billing_type)
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
        raise HTTPException(
            status_code=404,
            detail=f"Billing plan not found for plan_key={plan_key}, billing_type={billing_type}.",
        )

    _log_debug(
        "🔥 PLAN FOUND",
        plan_id=getattr(plan, "id", None),
        plan_key=getattr(plan, "plan_key", None),
        billing_type=getattr(plan, "billing_type", None),
        tenant_user_limit=getattr(plan, "tenant_user_limit", None),
        device_limit=getattr(plan, "device_limit", None),
    )

    current_plan_key = str(subscription.plan_key or "free").strip().lower()
    current_limit = int(subscription.tenants_users_limit or 0)
    current_device_limit = int(subscription.device_limit or 0)
    base_tenant_limit = int(plan.tenant_user_limit or 0)
    base_device_limit = int(plan.device_limit or 0)

    if is_current_plan:
        new_tenant_limit = current_limit + extra_tenant_users
        new_plan_key = current_plan_key
        new_device_limit = current_device_limit or base_device_limit
    else:
        new_tenant_limit = base_tenant_limit + extra_tenant_users
        new_plan_key = plan_key
        new_device_limit = base_device_limit

    _log_debug(
        "🔥 SUBSCRIPTION UPDATE PREVIEW",
        current_plan_key=current_plan_key,
        current_limit=current_limit,
        current_device_limit=current_device_limit,
        new_plan_key=new_plan_key,
        new_tenant_limit=new_tenant_limit,
        new_device_limit=new_device_limit,
    )

    subscription.plan_key = new_plan_key
    subscription.device_limit = new_device_limit
    subscription.tenants_users_limit = new_tenant_limit
    subscription.is_active = True

    if hasattr(subscription, "status"):
        subscription.status = "Active"

    if hasattr(subscription, "renewal_date"):
        if billing_type == "monthly":
            subscription.renewal_date = _utcnow() + timedelta(days=30)
        else:
            subscription.renewal_date = None

    try:
        print("🔥 DB COMMIT START")
        db.commit()
        print("✅ DB COMMIT OK")
    except Exception:
        db.rollback()
        print("❌ DB COMMIT FAILED - ROLLBACK DONE")
        traceback.print_exc()
        raise

    db.refresh(subscription)
    print("✅ SUBSCRIPTION REFRESH OK")

    _mark_payment_intent_applied(payment_intent_id, metadata)

    return {
        "ok": True,
        "alreadyApplied": False,
        "added": extra_tenant_users,
        "planKey": str(subscription.plan_key or "free").strip().lower(),
        "tenantsUsersLimit": int(subscription.tenants_users_limit or 0),
        "tenantUsersUsed": None,
        "message": "Payment applied successfully.",
    }


def _retrieve_payment_intent_or_none(payment_intent_id: str):
    pid = str(payment_intent_id or "").strip()
    if not pid:
        return None
    try:
        return stripe.PaymentIntent.retrieve(pid)
    except stripe.error.StripeError as e:
        print(
            "⚠️ FAILED TO RETRIEVE PAYMENTINTENT",
            pid,
            _describe_exception(e),
        )
        return None


def _extract_checkout_session_data(session_obj):
    session_id = str(getattr(session_obj, "id", "") or "").strip()
    payment_status = str(
        getattr(session_obj, "payment_status", "") or ""
    ).strip().lower()
    payment_intent_id = str(getattr(session_obj, "payment_intent", "") or "").strip()
    metadata = _safe_metadata_dict(getattr(session_obj, "metadata", None))

    return {
        "session_id": session_id,
        "payment_status": payment_status,
        "payment_intent_id": payment_intent_id,
        "metadata": metadata,
    }


def _process_checkout_session_completed(db: Session, session_obj):
    extracted = _extract_checkout_session_data(session_obj)
    session_id = extracted["session_id"]
    payment_status = extracted["payment_status"]
    payment_intent_id = extracted["payment_intent_id"]
    session_metadata = extracted["metadata"]

    _log_debug(
        "🔥 WEBHOOK checkout.session.completed",
        session_id=session_id,
        payment_status=payment_status,
        payment_intent_id=payment_intent_id,
        session_metadata=session_metadata,
    )

    if payment_status != "paid":
        print("ℹ️ checkout.session.completed ignored because payment_status is not paid")
        return {"ok": True, "ignored": True, "reason": "payment_status_not_paid"}

    if not payment_intent_id:
        print("ℹ️ checkout.session.completed ignored because payment_intent is missing")
        return {"ok": True, "ignored": True, "reason": "missing_payment_intent"}

    intent = _retrieve_payment_intent_or_none(payment_intent_id)
    if not intent:
        raise HTTPException(
            status_code=500,
            detail="Failed to process checkout.session.completed: could not retrieve payment intent.",
        )

    intent_metadata = _safe_metadata_dict(getattr(intent, "metadata", None))

    _log_debug(
        "🔎 WEBHOOK RETRIEVE CHECK",
        session_id=session_id,
        session_metadata_from_event=session_metadata,
        retrieved_intent_id=payment_intent_id,
        retrieved_intent_metadata=intent_metadata,
    )

    metadata = _merge_metadata(session_metadata, intent_metadata)
    metadata = _normalize_payment_metadata(db, metadata)

    _log_debug(
        "🔥 FINAL CHECKOUT METADATA",
        session_id=session_id,
        payment_intent_id=payment_intent_id,
        session_metadata=session_metadata,
        intent_metadata=intent_metadata,
        merged_metadata=metadata,
    )

    if not str(metadata.get("user_id") or "").strip().isdigit():
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid payment metadata: user_id. "
                f"session_metadata={session_metadata} "
                f"intent_metadata={intent_metadata} "
                f"merged_metadata={metadata}"
            ),
        )

    return _apply_payment_effects(
        db=db,
        payment_intent_id=payment_intent_id,
        metadata=metadata,
    )


def _process_payment_intent_succeeded(db: Session, intent_obj):
    payment_intent_id = str(getattr(intent_obj, "id", "") or "").strip()
    metadata = _safe_metadata_dict(getattr(intent_obj, "metadata", None))
    metadata = _normalize_payment_metadata(db, metadata)

    _log_debug(
        "🔥 WEBHOOK payment_intent.succeeded",
        payment_intent_id=payment_intent_id,
        metadata=metadata,
    )

    if not payment_intent_id:
        print("ℹ️ payment_intent.succeeded ignored because id is missing")
        return {"ok": True, "ignored": True, "reason": "missing_payment_intent_id"}

    if not metadata:
        print("ℹ️ payment_intent.succeeded ignored because metadata is missing")
        return {"ok": True, "ignored": True, "reason": "missing_metadata"}

    return _apply_payment_effects(
        db=db,
        payment_intent_id=payment_intent_id,
        metadata=metadata,
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

    ctx = _build_purchase_context(
        db=db,
        current_user=current_user,
        plan_key=plan_key,
        billing_type=billing_type,
        extra_tenant_users=extra_tenant_users,
    )

    try:
        if ctx["amount_cents"] <= 0:
            raise HTTPException(
                status_code=400,
                detail="Payment amount must be greater than zero.",
            )

        intent = stripe.PaymentIntent.create(
            amount=ctx["amount_cents"],
            currency="usd",
            payment_method_types=["card"],
            receipt_email=str(getattr(current_user, "email", "") or "").strip() or None,
            metadata=ctx["metadata"],
        )

        return {
            "ok": True,
            "clientSecret": intent.client_secret,
            "paymentIntentId": intent.id,
            "amount": ctx["amount_cents"],
            "currency": "usd",
            "planAmount": decimal_to_float_2(ctx["plan_amount_usd"]),
            "addonAmount": decimal_to_float_2(ctx["addon_amount_usd"]),
            "subtotal": decimal_to_float_2(ctx["subtotal_usd"]),
            "tax": decimal_to_float_2(ctx["tax_amount_usd"]),
            "taxRate": ctx["tax_rate"],
            "taxRatePercent": ctx["tax_rate_percent"],
            "taxLabel": "NJ Sales Tax",
            "total": decimal_to_float_2(ctx["total_usd"]),
        }

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)}")


@router.post("/create-checkout-session")
def create_checkout_session(
    payload: CreateCheckoutSessionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_stripe_ready()

    plan_key = str(payload.planKey or "").strip().lower()
    billing_type = normalize_billing_type(payload.billingType)
    extra_tenant_users = max(0, int(payload.extraTenantUsers or 0))

    ctx = _build_purchase_context(
        db=db,
        current_user=current_user,
        plan_key=plan_key,
        billing_type=billing_type,
        extra_tenant_users=extra_tenant_users,
    )

    if ctx["amount_cents"] <= 0:
        raise HTTPException(
            status_code=400,
            detail="Payment amount must be greater than zero.",
        )

    line_items = []

    if ctx["plan_amount_usd"] > 0:
        line_items.append(
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": (
                            f"{ctx['plan'].plan_name} Plan "
                            f"({'Monthly' if billing_type == 'monthly' else 'One-Time License'})"
                        ),
                    },
                    "unit_amount": money_to_cents(ctx["plan_amount_usd"]),
                },
                "quantity": 1,
            }
        )

    if extra_tenant_users > 0 and ctx["addon"] and ctx["addon_amount_usd"] > 0:
        line_items.append(
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": "Additional Tenant-User",
                    },
                    "unit_amount": money_to_cents(ctx["addon_unit_price_usd"]),
                },
                "quantity": extra_tenant_users,
            }
        )

    if ctx["tax_amount_usd"] > 0:
        line_items.append(
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": "NJ Sales Tax",
                    },
                    "unit_amount": money_to_cents(ctx["tax_amount_usd"]),
                },
                "quantity": 1,
            }
        )

    if not line_items:
        raise HTTPException(
            status_code=400,
            detail="There is no charge to process for this checkout session.",
        )

    try:
        print("🔥 SENDING METADATA TO STRIPE:", ctx["metadata"])

        session = stripe.checkout.Session.create(
            mode="payment",
            success_url=STRIPE_CHECKOUT_SUCCESS_URL,
            cancel_url=STRIPE_CHECKOUT_CANCEL_URL,
            customer_email=str(getattr(current_user, "email", "") or "").strip() or None,
            payment_method_types=["card"],
            line_items=line_items,
            metadata=ctx["metadata"],
            payment_intent_data={
                "receipt_email": str(getattr(current_user, "email", "") or "").strip() or None,
                "metadata": ctx["metadata"],
            },
        )

        print("✅ CHECKOUT SESSION CREATED")
        print("   session_id:", session.id)
        print(
            "   session_metadata_immediate:",
            _safe_metadata_dict(getattr(session, "metadata", None)),
        )
        print(
            "   payment_intent_immediate:",
            getattr(session, "payment_intent", None),
        )

        verified_session = stripe.checkout.Session.retrieve(session.id)
        verified_session_metadata = _safe_metadata_dict(
            getattr(verified_session, "metadata", None)
        )
        verified_payment_intent_id = str(
            getattr(verified_session, "payment_intent", "") or ""
        ).strip()

        verified_intent_metadata = {}
        if verified_payment_intent_id:
            verified_intent = stripe.PaymentIntent.retrieve(verified_payment_intent_id)
            verified_intent_metadata = _safe_metadata_dict(
                getattr(verified_intent, "metadata", None)
            )

        print("✅ VERIFIED STRIPE OBJECTS AFTER CREATE")
        print("   verified_session_id:", getattr(verified_session, "id", None))
        print("   verified_session_metadata:", verified_session_metadata)
        print("   verified_payment_intent_id:", verified_payment_intent_id)
        print("   verified_payment_intent_metadata:", verified_intent_metadata)

        return {
            "ok": True,
            "checkoutSessionId": session.id,
            "url": session.url,
            "planAmount": decimal_to_float_2(ctx["plan_amount_usd"]),
            "addonAmount": decimal_to_float_2(ctx["addon_amount_usd"]),
            "subtotal": decimal_to_float_2(ctx["subtotal_usd"]),
            "tax": decimal_to_float_2(ctx["tax_amount_usd"]),
            "taxRate": ctx["tax_rate"],
            "taxRatePercent": ctx["tax_rate_percent"],
            "taxLabel": "NJ Sales Tax",
            "total": decimal_to_float_2(ctx["total_usd"]),
        }
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)}")


@router.get("/checkout-session/{session_id}")
def get_checkout_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
):
    ensure_stripe_ready()

    sid = str(session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required.")

    try:
        session = stripe.checkout.Session.retrieve(sid)
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)}")

    metadata = _safe_metadata_dict(getattr(session, "metadata", None))
    session_user_id = str(metadata.get("user_id") or "").strip()

    if session_user_id and session_user_id != str(current_user.id):
        raise HTTPException(
            status_code=403,
            detail="This checkout session does not belong to the authenticated user.",
        )

    return {
        "ok": True,
        "id": session.id,
        "status": getattr(session, "status", None),
        "payment_status": getattr(session, "payment_status", None),
        "payment_intent": getattr(session, "payment_intent", None),
        "customer_email": getattr(session, "customer_email", None),
        "metadata": metadata,
    }


@router.post("/checkout-session/{session_id}/apply")
def apply_checkout_session(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_stripe_ready()

    sid = str(session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required.")

    try:
        session = stripe.checkout.Session.retrieve(sid)
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)}")

    if not session:
        raise HTTPException(status_code=404, detail="Checkout session not found.")

    session_metadata = _safe_metadata_dict(getattr(session, "metadata", None))
    session_user_id = str(session_metadata.get("user_id") or "").strip()

    if session_user_id and session_user_id != str(current_user.id):
        raise HTTPException(
            status_code=403,
            detail="This checkout session does not belong to the authenticated user.",
        )

    payment_status = str(getattr(session, "payment_status", "") or "").strip().lower()
    if payment_status != "paid":
        raise HTTPException(
            status_code=400,
            detail="Checkout session is not paid.",
        )

    payment_intent_id = str(getattr(session, "payment_intent", "") or "").strip()
    if not payment_intent_id:
        raise HTTPException(
            status_code=400,
            detail="Checkout session does not have a payment intent.",
        )

    try:
        intent = stripe.PaymentIntent.retrieve(payment_intent_id)
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)}")

    if not intent:
        raise HTTPException(status_code=404, detail="PaymentIntent not found.")

    intent_status = str(getattr(intent, "status", "") or "").strip().lower()
    if intent_status != "succeeded":
        raise HTTPException(
            status_code=400,
            detail="PaymentIntent is not completed yet.",
        )

    metadata = _merge_metadata(
        _safe_metadata_dict(getattr(session, "metadata", None)),
        _safe_metadata_dict(getattr(intent, "metadata", None)),
    )
    metadata = _normalize_payment_metadata(db, metadata)

    intent_user_id = str(metadata.get("user_id") or "").strip()
    if intent_user_id != str(current_user.id):
        raise HTTPException(
            status_code=403,
            detail="This payment does not belong to the authenticated user.",
        )

    return _apply_payment_effects(
        db=db,
        payment_intent_id=payment_intent_id,
        metadata=metadata,
    )


@router.post("/apply-payment")
def apply_payment(
    payload: ApplyPaymentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_stripe_ready()

    payment_intent_id = str(payload.paymentIntentId or "").strip()
    if not payment_intent_id:
        raise HTTPException(status_code=400, detail="paymentIntentId is required.")

    try:
        intent = stripe.PaymentIntent.retrieve(payment_intent_id)
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)}")

    if not intent:
        raise HTTPException(status_code=404, detail="PaymentIntent not found.")

    if str(getattr(intent, "status", "") or "").strip().lower() != "succeeded":
        raise HTTPException(
            status_code=400,
            detail="PaymentIntent is not completed yet.",
        )

    metadata = _safe_metadata_dict(getattr(intent, "metadata", None))
    metadata = _normalize_payment_metadata(db, metadata)

    intent_user_id = str(metadata.get("user_id") or "").strip()
    if intent_user_id != str(current_user.id):
        raise HTTPException(
            status_code=403,
            detail="This payment does not belong to the authenticated user.",
        )

    return _apply_payment_effects(
        db=db,
        payment_intent_id=payment_intent_id,
        metadata=metadata,
    )


@router.post("/stripe-webhook")
async def stripe_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    ensure_stripe_webhook_ready()

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing Stripe signature.")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid webhook payload.")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    event_id = str(getattr(event, "id", "") or "").strip()
    event_type = str(getattr(event, "type", "") or "").strip()
    event_data = getattr(event, "data", None)
    data_object = getattr(event_data, "object", None) if event_data is not None else None

    _log_debug(
        "🔥 STRIPE WEBHOOK RECEIVED",
        event_id=event_id,
        event_type=event_type,
    )

    try:
        if event_type == "checkout.session.completed":
            return_value = _process_checkout_session_completed(
                db=db,
                session_obj=data_object,
            )
            _log_debug(
                "✅ WEBHOOK checkout.session.completed PROCESSED",
                event_id=event_id,
                result=return_value,
            )

        elif event_type == "payment_intent.succeeded":
            return_value = _process_payment_intent_succeeded(
                db=db,
                intent_obj=data_object,
            )
            _log_debug(
                "✅ WEBHOOK payment_intent.succeeded PROCESSED",
                event_id=event_id,
                result=return_value,
            )

        else:
            print("ℹ️ STRIPE WEBHOOK IGNORED EVENT TYPE:", event_type)

    except HTTPException as e:
        print("❌ STRIPE WEBHOOK HTTPException")
        _log_debug(
            "❌ WEBHOOK HTTPException DETAILS",
            event_id=event_id,
            event_type=event_type,
            status_code=e.status_code,
            detail=e.detail,
        )
        raise
    except Exception as e:
        print("❌ STRIPE WEBHOOK UNHANDLED ERROR")
        _log_debug(
            "❌ WEBHOOK UNHANDLED ERROR DETAILS",
            event_id=event_id,
            event_type=event_type,
            error=_describe_exception(e),
        )
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process {event_type}: {_describe_exception(e)}",
        )

    return {"ok": True, "received": True, "eventType": event_type, "eventId": event_id}