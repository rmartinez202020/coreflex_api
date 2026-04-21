# routers/billing_checkout.py
import stripe
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth_utils import get_current_user
from database import get_db
from models import User, BillingPlan, BillingAddon
from routers.billing_common import (
    NJ_SALES_TAX_RATE,
    CreatePaymentIntentRequest,
    CreateCheckoutSessionRequest,
    ApplyPaymentRequest,
    STRIPE_CHECKOUT_SUCCESS_URL,
    STRIPE_CHECKOUT_CANCEL_URL,
    ensure_stripe_ready,
    normalize_billing_type,
    to_money_decimal,
    decimal_to_float_2,
    percent_display_2_from_rate,
    rate_display_2_from_percent,
    _build_purchase_context,
    _build_checkout_line_items,
    _resolve_checkout_session_payment_intent,
    _safe_metadata_dict,
    _merge_metadata,
    _normalize_payment_metadata,
    _apply_payment_effects,
)

router = APIRouter()


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

    checkout_mode, line_items = _build_checkout_line_items(ctx, billing_type)

    if not line_items:
        raise HTTPException(
            status_code=400,
            detail="There is no charge to process for this checkout session.",
        )

    try:
        print("🔥 SENDING METADATA TO STRIPE:", ctx["metadata"])
        print("🔥 CHECKOUT MODE:", checkout_mode)
        print("🔥 LINE ITEMS:", line_items)

        checkout_kwargs = {
            "mode": checkout_mode,
            "success_url": STRIPE_CHECKOUT_SUCCESS_URL,
            "cancel_url": STRIPE_CHECKOUT_CANCEL_URL,
            "customer_email": str(getattr(current_user, "email", "") or "").strip() or None,
            "payment_method_types": ["card"],
            "line_items": line_items,
            "metadata": ctx["metadata"],
        }

        if checkout_mode == "payment":
            checkout_kwargs["payment_intent_data"] = {
                "receipt_email": str(getattr(current_user, "email", "") or "").strip() or None,
                "metadata": ctx["metadata"],
            }
        else:
            checkout_kwargs["subscription_data"] = {
                "metadata": ctx["metadata"],
            }

        session = stripe.checkout.Session.create(**checkout_kwargs)

        print("✅ CHECKOUT SESSION CREATED")
        print("   session_id:", session.id)
        print("   checkout_mode:", checkout_mode)
        print(
            "   session_metadata_immediate:",
            _safe_metadata_dict(getattr(session, "metadata", None)),
        )
        print(
            "   payment_intent_immediate:",
            getattr(session, "payment_intent", None),
        )
        print(
            "   invoice_immediate:",
            getattr(session, "invoice", None),
        )
        print(
            "   subscription_immediate:",
            getattr(session, "subscription", None),
        )

        verified_session = stripe.checkout.Session.retrieve(session.id)
        verified_session_metadata = _safe_metadata_dict(
            getattr(verified_session, "metadata", None)
        )

        resolved_payment = _resolve_checkout_session_payment_intent(verified_session)
        verified_payment_intent_id = resolved_payment["payment_intent_id"]
        verified_payment_source = resolved_payment["source"]
        verified_invoice_id = resolved_payment["invoice_id"]
        verified_subscription_id = resolved_payment["subscription_id"]

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
        print("   verified_payment_intent_source:", verified_payment_source)
        print("   verified_invoice_id:", verified_invoice_id)
        print("   verified_subscription_id:", verified_subscription_id)
        print("   verified_payment_intent_metadata:", verified_intent_metadata)

        return {
            "ok": True,
            "checkoutSessionId": session.id,
            "url": session.url,
            "checkoutMode": checkout_mode,
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

    resolved_payment = _resolve_checkout_session_payment_intent(session)

    return {
        "ok": True,
        "id": session.id,
        "mode": getattr(session, "mode", None),
        "status": getattr(session, "status", None),
        "payment_status": getattr(session, "payment_status", None),
        "payment_intent": resolved_payment["payment_intent_id"] or getattr(session, "payment_intent", None),
        "payment_intent_source": resolved_payment["source"],
        "invoice_id": resolved_payment["invoice_id"],
        "subscription_id": resolved_payment["subscription_id"],
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

    resolved_payment = _resolve_checkout_session_payment_intent(session)
    payment_intent_id = resolved_payment["payment_intent_id"]

    if not payment_intent_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Checkout session does not have a resolvable payment intent. "
                f"invoice_id={resolved_payment['invoice_id']} "
                f"subscription_id={resolved_payment['subscription_id']}"
            ),
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