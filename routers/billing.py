# routers/billing.py
import os
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
        "user_id": str(current_user.id),
        "user_email": str(getattr(current_user, "email", "") or ""),
        "plan_key": str(plan_key),
        "current_plan_key": str(current_plan_key),
        "is_current_plan": "true" if is_current_plan else "false",
        "billing_type": str(billing_type),
        "extra_tenant_users": str(extra_tenant_users),
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
    print("🔥 APPLY PAYMENT EFFECTS payment_intent_id:", payment_intent_id)
    print("🔥 APPLY PAYMENT EFFECTS metadata:", metadata)

    raw_user_id = str(metadata.get("user_id") or "").strip()
    if not raw_user_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid payment metadata: user_id.")

    user_id = int(raw_user_id)
    plan_key = str(metadata.get("plan_key") or "free").strip().lower()
    billing_type = normalize_billing_type(metadata.get("billing_type"))
    is_current_plan = (
        str(metadata.get("is_current_plan") or "").strip().lower() == "true"
    )
    extra_tenant_users = max(0, _safe_int(metadata.get("extra_tenant_users"), 0))
    already_applied = str(metadata.get("applied") or "").strip().lower() == "true"

    subscription = _get_or_create_user_subscription(db, user_id)

    if already_applied:
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
        raise HTTPException(status_code=404, detail="Billing plan not found.")

    current_limit = int(subscription.tenants_users_limit or 0)
    base_tenant_limit = int(plan.tenant_user_limit or 0)
    base_device_limit = int(plan.device_limit or 0)

    if is_current_plan:
        new_tenant_limit = current_limit + extra_tenant_users
        new_plan_key = str(subscription.plan_key or "free").strip().lower()
        new_device_limit = int(subscription.device_limit or base_device_limit or 0)
    else:
        new_tenant_limit = base_tenant_limit + extra_tenant_users
        new_plan_key = plan_key
        new_device_limit = base_device_limit

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

    db.commit()
    db.refresh(subscription)

    try:
        stripe.PaymentIntent.modify(
            payment_intent_id,
            metadata={
                **metadata,
                "applied": "true",
            },
        )
    except stripe.error.StripeError:
        pass

    return {
        "ok": True,
        "alreadyApplied": False,
        "added": extra_tenant_users,
        "planKey": str(subscription.plan_key or "free").strip().lower(),
        "tenantsUsersLimit": int(subscription.tenants_users_limit or 0),
        "tenantUsersUsed": None,
        "message": "Payment applied successfully.",
    }


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

    metadata = getattr(session, "metadata", {}) or {}
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

    session_metadata = getattr(session, "metadata", {}) or {}
    session_user_id = str(session_metadata.get("user_id") or "").strip()

    if session_user_id != str(current_user.id):
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

    metadata = getattr(intent, "metadata", {}) or {}
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

    metadata = getattr(intent, "metadata", {}) or {}
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

    event_type = str(event.get("type") or "").strip()
    data_object = ((event.get("data") or {}).get("object") or {})

    try:
        if event_type == "checkout.session.completed":
            session_obj = data_object
            payment_status = str(session_obj.get("payment_status") or "").strip().lower()
            payment_intent_id = str(session_obj.get("payment_intent") or "").strip()

            if payment_status == "paid" and payment_intent_id:
                try:
                    intent = stripe.PaymentIntent.retrieve(payment_intent_id)
                    metadata = getattr(intent, "metadata", {}) or {}
                    _apply_payment_effects(
                        db=db,
                        payment_intent_id=payment_intent_id,
                        metadata=metadata,
                    )
                except HTTPException:
                    raise
                except Exception as e:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to process checkout.session.completed: {str(e)}",
                    )

        elif event_type == "payment_intent.succeeded":
            intent_obj = data_object
            payment_intent_id = str(intent_obj.get("id") or "").strip()
            metadata = intent_obj.get("metadata") or {}

            if payment_intent_id and metadata:
                try:
                    _apply_payment_effects(
                        db=db,
                        payment_intent_id=payment_intent_id,
                        metadata=metadata,
                    )
                except HTTPException:
                    raise
                except Exception as e:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to process payment_intent.succeeded: {str(e)}",
                    )

    except HTTPException:
        raise

    return {"ok": True, "received": True, "eventType": event_type}