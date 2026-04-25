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
    _safe_metadata_dict,
    _merge_metadata,
    _normalize_payment_metadata,
    _metadata_from_client_reference_id,
    _apply_payment_effects,
)
from routers.billing_purchase_helpers import (
    _build_purchase_context,
    _build_checkout_line_items,
)
from routers.billing_webhook_helpers import (
    _resolve_checkout_session_payment_intent,
)

router = APIRouter()


def _get_or_create_stripe_customer_for_user(current_user: User):
    user_email = str(getattr(current_user, "email", "") or "").strip()
    user_name = str(getattr(current_user, "name", "") or "").strip()
    user_id = str(getattr(current_user, "id", "") or "").strip()

    if not user_email:
        raise HTTPException(
            status_code=400,
            detail="User email is required to create a Stripe customer.",
        )

    existing_customers = stripe.Customer.list(email=user_email, limit=10)

    for customer in existing_customers.data:
        if str(getattr(customer, "email", "") or "").strip().lower() == user_email.lower():
            print("✅ USING EXISTING STRIPE CUSTOMER:", customer.id, user_email)
            return customer

    customer = stripe.Customer.create(
        email=user_email,
        name=user_name or None,
        metadata={
            "coreflex_user_id": user_id,
            "coreflex_user_email": user_email,
        },
    )

    print("✅ CREATED NEW STRIPE CUSTOMER:", customer.id, user_email)
    return customer


def _get_existing_stripe_customers_for_user(current_user: User):
    user_email = str(getattr(current_user, "email", "") or "").strip()

    if not user_email:
        return []

    existing_customers = stripe.Customer.list(email=user_email, limit=100)

    matched_customers = []
    for customer in existing_customers.data:
        customer_email = str(getattr(customer, "email", "") or "").strip().lower()
        if customer_email == user_email.lower():
            matched_customers.append(customer)

    return matched_customers


def _cancel_other_active_subscriptions_for_customer(
    stripe_customer_id: str,
    keep_subscription_id: str | None = None,
):
    customer_id = str(stripe_customer_id or "").strip()
    keep_id = str(keep_subscription_id or "").strip()

    if not customer_id:
        return {"cancelled": 0, "kept": keep_id or None}

    cancelled_count = 0

    active_subscriptions = stripe.Subscription.list(
        customer=customer_id,
        status="active",
        limit=100,
    )

    for sub in active_subscriptions.data:
        sub_id = str(getattr(sub, "id", "") or "").strip()

        if keep_id and sub_id == keep_id:
            print("✅ KEEPING CURRENT SUBSCRIPTION:", sub_id)
            continue

        print("🔥 CANCELLING OLD ACTIVE SUBSCRIPTION:", sub_id)

        stripe.Subscription.delete(
            sub_id,
            prorate=False,
        )

        cancelled_count += 1

    return {
        "cancelled": cancelled_count,
        "kept": keep_id or None,
    }


def _cancel_all_billable_subscriptions_for_user_after_one_time_purchase(
    current_user: User,
):
    customers = _get_existing_stripe_customers_for_user(current_user)

    if not customers:
        print("✅ ONE-TIME PURCHASE CLEANUP: No Stripe customers found.")
        return {
            "cancelled": 0,
            "customers_checked": 0,
            "statuses_checked": [],
            "reason": "no_stripe_customer_found",
        }

    statuses_to_cancel = ["active", "trialing", "past_due", "unpaid", "incomplete"]
    cancelled_count = 0
    checked_count = 0
    cancelled_subscription_ids = []

    for customer in customers:
        customer_id = str(getattr(customer, "id", "") or "").strip()
        if not customer_id:
            continue

        for status in statuses_to_cancel:
            subscriptions = stripe.Subscription.list(
                customer=customer_id,
                status=status,
                limit=100,
            )

            for sub in subscriptions.data:
                checked_count += 1
                sub_id = str(getattr(sub, "id", "") or "").strip()

                if not sub_id:
                    continue

                print(
                    "🔥 ONE-TIME PURCHASE: CANCELLING RECURRING SUBSCRIPTION:",
                    sub_id,
                    "customer:",
                    customer_id,
                    "status:",
                    status,
                )

                stripe.Subscription.delete(
                    sub_id,
                    prorate=False,
                )

                cancelled_count += 1
                cancelled_subscription_ids.append(sub_id)

    result = {
        "cancelled": cancelled_count,
        "customers_checked": len(customers),
        "subscriptions_checked": checked_count,
        "statuses_checked": statuses_to_cancel,
        "cancelled_subscription_ids": cancelled_subscription_ids,
    }

    print("✅ ONE-TIME PURCHASE SUBSCRIPTION CLEANUP RESULT:", result)
    return result


def _is_tenant_user_addon_only_checkout(ctx: dict, extra_tenant_users: int) -> bool:
    try:
        return (
            bool(ctx.get("is_current_plan")) is True
            and int(extra_tenant_users or 0) > 0
            and to_money_decimal(ctx.get("plan_amount_usd")) <= to_money_decimal(0)
            and to_money_decimal(ctx.get("addon_amount_usd")) > to_money_decimal(0)
            and int(ctx.get("amount_cents") or 0) > 0
        )
    except Exception:
        return False


def _is_one_time_license_payment(metadata: dict) -> bool:
    billing_type = str(metadata.get("billing_type") or "").strip().lower()
    checkout_type = str(metadata.get("checkout_type") or "").strip().lower()
    force_one_time_payment = (
        str(metadata.get("force_one_time_payment") or "").strip().lower() == "true"
    )
    do_not_create_subscription = (
        str(metadata.get("do_not_create_subscription") or "").strip().lower() == "true"
    )

    if checkout_type == "tenant_user_addon_only":
        return False

    if force_one_time_payment and do_not_create_subscription:
        return False

    if billing_type != "one_time":
        return False

    return True


def _build_one_time_tenant_user_addon_line_items(ctx: dict, extra_tenant_users: int):
    total_cents = int(ctx.get("amount_cents") or 0)

    if total_cents <= 0:
        raise HTTPException(
            status_code=400,
            detail="Tenant-user add-on payment amount must be greater than zero.",
        )

    return [
        {
            "price_data": {
                "currency": "usd",
                "product_data": {
                    "name": (
                        "Additional Tenant-User"
                        if int(extra_tenant_users or 0) == 1
                        else f"Additional Tenant-Users × {int(extra_tenant_users or 0)}"
                    ),
                    "metadata": {
                        "coreflex_item_type": "tenant_user_addon",
                        "extra_tenant_users": str(int(extra_tenant_users or 0)),
                    },
                },
                "unit_amount": total_cents,
            },
            "quantity": 1,
        }
    ]


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

    is_tenant_user_addon_only = _is_tenant_user_addon_only_checkout(
        ctx=ctx,
        extra_tenant_users=extra_tenant_users,
    )

    if is_tenant_user_addon_only:
        checkout_mode = "payment"
        line_items = _build_one_time_tenant_user_addon_line_items(
            ctx=ctx,
            extra_tenant_users=extra_tenant_users,
        )

        ctx["metadata"] = {
            **ctx["metadata"],
            "checkout_type": "tenant_user_addon_only",
            "force_one_time_payment": "true",
            "do_not_create_subscription": "true",
        }
    else:
        checkout_mode, line_items = _build_checkout_line_items(ctx, billing_type)

    # ✅ HARD SAFETY LOCK:
    # One-Time License must NEVER create a Stripe subscription.
    # Monthly must use subscription mode unless this is tenant-user add-on only.
    if billing_type == "one_time":
        checkout_mode = "payment"
    elif billing_type == "monthly" and not is_tenant_user_addon_only:
        checkout_mode = "subscription"

    if not line_items:
        raise HTTPException(
            status_code=400,
            detail="There is no charge to process for this checkout session.",
        )

    checkout_customer_email = (
        str(getattr(current_user, "email", "") or "").strip() or None
    )

    client_reference_id = (
        f"uid={current_user.id};plan={plan_key};billing_type={billing_type}"
    )

    try:
        print("🔥 SENDING METADATA TO STRIPE:", ctx["metadata"])
        print("🔥 IS TENANT USER ADDON ONLY:", is_tenant_user_addon_only)
        print("🔥 BILLING TYPE:", billing_type)
        print("🔥 FINAL CHECKOUT MODE:", checkout_mode)
        print("🔥 LINE ITEMS:", line_items)
        print("🔥 CLIENT REFERENCE ID:", client_reference_id)

        checkout_kwargs = {
            "mode": checkout_mode,
            "success_url": STRIPE_CHECKOUT_SUCCESS_URL,
            "cancel_url": STRIPE_CHECKOUT_CANCEL_URL,
            "client_reference_id": client_reference_id,
            "payment_method_types": ["card"],
            "line_items": line_items,
            "metadata": ctx["metadata"],
        }

        if checkout_mode == "payment":
            checkout_kwargs["customer_email"] = checkout_customer_email
            checkout_kwargs["payment_intent_data"] = {
                "receipt_email": checkout_customer_email,
                "metadata": ctx["metadata"],
            }

            # ✅ Extra protection: payment mode must not send subscription data.
            checkout_kwargs.pop("subscription_data", None)
            checkout_kwargs.pop("customer", None)

            print("✅ PAYMENT CHECKOUT LOCKED")
            print("   reason:", "one_time" if billing_type == "one_time" else "addon_only")
            print("   mode:", checkout_kwargs.get("mode"))
            print("   payment_intent_data.metadata:", ctx["metadata"])

        else:
            stripe_customer = _get_or_create_stripe_customer_for_user(current_user)

            checkout_kwargs["customer"] = stripe_customer.id
            checkout_kwargs["subscription_data"] = {
                "metadata": ctx["metadata"],
                "description": (
                    f"CoreFlex billing "
                    f"user_id={current_user.id} "
                    f"plan_key={plan_key} "
                    f"billing_type={billing_type} "
                    f"extra_tenant_users={extra_tenant_users}"
                ),
            }

            # ✅ Extra protection: subscription mode must not send payment_intent_data.
            checkout_kwargs.pop("customer_email", None)
            checkout_kwargs.pop("payment_intent_data", None)

            print("✅ SUBSCRIPTION CHECKOUT LOCKED")
            print("   mode:", checkout_kwargs.get("mode"))
            print("   subscription_data.metadata:", ctx["metadata"])

        session = stripe.checkout.Session.create(**checkout_kwargs)

        print("✅ CHECKOUT SESSION CREATED")
        print("   session_id:", session.id)
        print("   checkout_mode:", checkout_mode)
        print("   stripe_session_mode:", getattr(session, "mode", None))
        print(
            "   session_metadata_immediate:",
            _safe_metadata_dict(getattr(session, "metadata", None)),
        )
        print(
            "   session_client_reference_id_immediate:",
            getattr(session, "client_reference_id", None),
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
        verified_client_reference_id = str(
            getattr(verified_session, "client_reference_id", "") or ""
        ).strip()

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

        verified_subscription_metadata = {}
        if verified_subscription_id:
            verified_subscription = stripe.Subscription.retrieve(
                verified_subscription_id
            )
            verified_subscription_metadata = _safe_metadata_dict(
                getattr(verified_subscription, "metadata", None)
            )

        print("✅ VERIFIED STRIPE OBJECTS AFTER CREATE")
        print("   verified_session_id:", getattr(verified_session, "id", None))
        print("   verified_session_mode:", getattr(verified_session, "mode", None))
        print("   verified_session_metadata:", verified_session_metadata)
        print("   verified_client_reference_id:", verified_client_reference_id)
        print("   verified_payment_intent_id:", verified_payment_intent_id)
        print("   verified_payment_intent_source:", verified_payment_source)
        print("   verified_invoice_id:", verified_invoice_id)
        print("   verified_subscription_id:", verified_subscription_id)
        print("   verified_subscription_metadata:", verified_subscription_metadata)
        print("   verified_payment_intent_metadata:", verified_intent_metadata)

        if billing_type == "one_time" and getattr(verified_session, "mode", None) != "payment":
            raise HTTPException(
                status_code=500,
                detail="Safety lock failed: one-time checkout was not created in payment mode.",
            )

        if billing_type == "one_time" and verified_subscription_id:
            raise HTTPException(
                status_code=500,
                detail="Safety lock failed: one-time checkout created a subscription.",
            )

        return {
            "ok": True,
            "checkoutSessionId": session.id,
            "url": session.url,
            "checkoutMode": checkout_mode,
            "stripeSessionMode": getattr(verified_session, "mode", None),
            "isTenantUserAddonOnly": is_tenant_user_addon_only,
            "clientReferenceId": client_reference_id,
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
    if not metadata:
        metadata = _metadata_from_client_reference_id(
            getattr(session, "client_reference_id", None)
        )

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
        "client_reference_id": getattr(session, "client_reference_id", None),
        "payment_intent": resolved_payment["payment_intent_id"]
        or getattr(session, "payment_intent", None),
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
    if not session_metadata:
        session_metadata = _metadata_from_client_reference_id(
            getattr(session, "client_reference_id", None)
        )

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
    new_subscription_id = str(resolved_payment["subscription_id"] or "").strip()

    session_mode = str(getattr(session, "mode", "") or "").strip().lower()
    is_payment_mode = session_mode == "payment"

    if not payment_intent_id and is_payment_mode:
        raise HTTPException(
            status_code=400,
            detail=(
                "Checkout session does not have a resolvable payment intent. "
                f"invoice_id={resolved_payment['invoice_id']} "
                f"subscription_id={resolved_payment['subscription_id']}"
            ),
        )

    intent = None
    intent_metadata = {}

    if payment_intent_id:
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

        intent_metadata = _safe_metadata_dict(getattr(intent, "metadata", None))

    metadata = _merge_metadata(
        session_metadata,
        intent_metadata,
    )
    metadata = _normalize_payment_metadata(db, metadata)

    if not str(metadata.get("user_id") or "").strip().isdigit():
        metadata = _normalize_payment_metadata(db, session_metadata)

    intent_user_id = str(metadata.get("user_id") or "").strip()
    if intent_user_id != str(current_user.id):
        raise HTTPException(
            status_code=403,
            detail="This payment does not belong to the authenticated user.",
        )

    apply_result = _apply_payment_effects(
        db=db,
        payment_intent_id=payment_intent_id,
        metadata=metadata,
    )

    if _is_one_time_license_payment(metadata):
        try:
            cleanup_result = _cancel_all_billable_subscriptions_for_user_after_one_time_purchase(
                current_user=current_user,
            )

            if isinstance(apply_result, dict):
                apply_result["stripe_one_time_cleanup"] = cleanup_result

        except stripe.error.StripeError as e:
            print("❌ Stripe one-time cleanup failed after payment:", str(e))
            raise HTTPException(
                status_code=502,
                detail=f"Stripe one-time cleanup failed: {str(e)}",
            )

        return apply_result

    if new_subscription_id:
        try:
            new_subscription = stripe.Subscription.retrieve(new_subscription_id)
            stripe_customer_id = str(
                getattr(new_subscription, "customer", "") or ""
            ).strip()

            cleanup_result = _cancel_other_active_subscriptions_for_customer(
                stripe_customer_id=stripe_customer_id,
                keep_subscription_id=new_subscription_id,
            )

            print("✅ STRIPE SUBSCRIPTION CLEANUP RESULT:", cleanup_result)

            if isinstance(apply_result, dict):
                apply_result["stripe_subscription_cleanup"] = cleanup_result

        except stripe.error.StripeError as e:
            print("❌ Stripe cleanup failed after payment:", str(e))
            raise HTTPException(
                status_code=502,
                detail=f"Stripe subscription cleanup failed: {str(e)}",
            )

    return apply_result


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

    apply_result = _apply_payment_effects(
        db=db,
        payment_intent_id=payment_intent_id,
        metadata=metadata,
    )

    if _is_one_time_license_payment(metadata):
        try:
            cleanup_result = _cancel_all_billable_subscriptions_for_user_after_one_time_purchase(
                current_user=current_user,
            )

            if isinstance(apply_result, dict):
                apply_result["stripe_one_time_cleanup"] = cleanup_result

        except stripe.error.StripeError as e:
            print("❌ Stripe one-time cleanup failed after payment:", str(e))
            raise HTTPException(
                status_code=502,
                detail=f"Stripe one-time cleanup failed: {str(e)}",
            )

    return apply_result