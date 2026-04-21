# routers/billing_common.py
import os
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import stripe
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import User, BillingPlan, BillingAddon, UserSubscription

STRIPE_SECRET_KEY = str(os.getenv("STRIPE_SECRET_KEY") or "").strip()
STRIPE_WEBHOOK_SECRET = str(os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()

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

NJ_SALES_TAX_RATE = Decimal("0.06625")

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
    pid = str(payment_intent_id or "").strip()
    if not pid:
        print("ℹ️ SKIPPING PAYMENTINTENT applied=true MARK because payment_intent_id is empty")
        return

    try:
        stripe.PaymentIntent.modify(
            pid,
            metadata={
                **_safe_metadata_dict(metadata),
                "applied": "true",
            },
        )
        print("✅ STRIPE METADATA UPDATED applied=true", pid)
    except stripe.error.StripeError as e:
        print(
            "⚠️ FAILED TO UPDATE STRIPE PAYMENTINTENT METADATA",
            pid,
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


def _retrieve_invoice_or_none(invoice_id: str):
    iid = str(invoice_id or "").strip()
    if not iid:
        return None
    try:
        return stripe.Invoice.retrieve(iid, expand=["payment_intent"])
    except stripe.error.StripeError as e:
        print(
            "⚠️ FAILED TO RETRIEVE INVOICE",
            iid,
            _describe_exception(e),
        )
        return None


def _retrieve_subscription_or_none(subscription_id: str):
    sid = str(subscription_id or "").strip()
    if not sid:
        return None
    try:
        return stripe.Subscription.retrieve(
            sid,
            expand=["latest_invoice.payment_intent"],
        )
    except stripe.error.StripeError as e:
        print(
            "⚠️ FAILED TO RETRIEVE SUBSCRIPTION",
            sid,
            _describe_exception(e),
        )
        return None


def _extract_payment_intent_id_from_invoice(invoice_obj) -> str:
    if not invoice_obj:
        return ""

    payment_intent = getattr(invoice_obj, "payment_intent", None)
    if isinstance(payment_intent, str):
        return str(payment_intent or "").strip()

    return str(getattr(payment_intent, "id", "") or "").strip()


def _resolve_checkout_session_payment_intent(session_obj):
    direct_payment_intent_id = str(
        getattr(session_obj, "payment_intent", "") or ""
    ).strip()
    if direct_payment_intent_id:
        return {
            "payment_intent_id": direct_payment_intent_id,
            "source": "session.payment_intent",
            "invoice_id": "",
            "subscription_id": "",
        }

    invoice_id = str(getattr(session_obj, "invoice", "") or "").strip()
    if invoice_id:
        invoice_obj = _retrieve_invoice_or_none(invoice_id)
        invoice_payment_intent_id = _extract_payment_intent_id_from_invoice(invoice_obj)
        if invoice_payment_intent_id:
            return {
                "payment_intent_id": invoice_payment_intent_id,
                "source": "session.invoice.payment_intent",
                "invoice_id": invoice_id,
                "subscription_id": "",
            }

    subscription_id = str(getattr(session_obj, "subscription", "") or "").strip()
    if subscription_id:
        subscription_obj = _retrieve_subscription_or_none(subscription_id)
        latest_invoice = getattr(subscription_obj, "latest_invoice", None)
        latest_invoice_payment_intent_id = _extract_payment_intent_id_from_invoice(
            latest_invoice
        )
        if latest_invoice_payment_intent_id:
            return {
                "payment_intent_id": latest_invoice_payment_intent_id,
                "source": "session.subscription.latest_invoice.payment_intent",
                "invoice_id": str(getattr(latest_invoice, "id", "") or "").strip(),
                "subscription_id": subscription_id,
            }

    return {
        "payment_intent_id": "",
        "source": "",
        "invoice_id": invoice_id,
        "subscription_id": str(getattr(session_obj, "subscription", "") or "").strip(),
    }


def _build_checkout_line_items(ctx: dict, billing_type: str):
    checkout_mode = "subscription" if billing_type == "monthly" else "payment"
    line_items = []

    def _build_price_data(name: str, unit_amount: int):
        price_data = {
            "currency": "usd",
            "product_data": {
                "name": name,
            },
            "unit_amount": unit_amount,
        }
        if checkout_mode == "subscription":
            price_data["recurring"] = {"interval": "month"}
        return price_data

    if ctx["plan_amount_usd"] > 0:
        line_items.append(
            {
                "price_data": _build_price_data(
                    (
                        f"{ctx['plan'].plan_name} Plan "
                        f"({'Monthly' if billing_type == 'monthly' else 'One-Time License'})"
                    ),
                    money_to_cents(ctx["plan_amount_usd"]),
                ),
                "quantity": 1,
            }
        )

    if ctx["addon"] and ctx["addon_amount_usd"] > 0:
        line_items.append(
            {
                "price_data": _build_price_data(
                    "Additional Tenant-User",
                    money_to_cents(ctx["addon_unit_price_usd"]),
                ),
                "quantity": int(
                    Decimal(str(ctx["addon_amount_usd"])) / Decimal(str(ctx["addon_unit_price_usd"]))
                )
                if ctx["addon_unit_price_usd"] > 0
                else 0,
            }
        )

    if ctx["tax_amount_usd"] > 0:
        line_items.append(
            {
                "price_data": _build_price_data(
                    "NJ Sales Tax",
                    money_to_cents(ctx["tax_amount_usd"]),
                ),
                "quantity": 1,
            }
        )

    line_items = [item for item in line_items if int(item.get("quantity") or 0) > 0]
    return checkout_mode, line_items


def _extract_checkout_session_data(session_obj):
    session_id = str(getattr(session_obj, "id", "") or "").strip()
    payment_status = str(
        getattr(session_obj, "payment_status", "") or ""
    ).strip().lower()
    metadata = _safe_metadata_dict(getattr(session_obj, "metadata", None))
    resolved = _resolve_checkout_session_payment_intent(session_obj)
    payment_intent_id = resolved["payment_intent_id"]

    return {
        "session_id": session_id,
        "payment_status": payment_status,
        "payment_intent_id": payment_intent_id,
        "payment_intent_source": resolved["source"],
        "invoice_id": resolved["invoice_id"],
        "subscription_id": resolved["subscription_id"],
        "metadata": metadata,
    }


def _process_checkout_session_completed(db: Session, session_obj):
    extracted = _extract_checkout_session_data(session_obj)
    session_id = extracted["session_id"]
    payment_status = extracted["payment_status"]
    payment_intent_id = extracted["payment_intent_id"]
    payment_intent_source = extracted["payment_intent_source"]
    invoice_id = extracted["invoice_id"]
    subscription_id = extracted["subscription_id"]
    session_metadata = extracted["metadata"]

    _log_debug(
        "🔥 WEBHOOK checkout.session.completed",
        session_id=session_id,
        payment_status=payment_status,
        payment_intent_id=payment_intent_id,
        payment_intent_source=payment_intent_source,
        invoice_id=invoice_id,
        subscription_id=subscription_id,
        session_metadata=session_metadata,
    )

    if payment_status != "paid":
        print("ℹ️ checkout.session.completed ignored because payment_status is not paid")
        return {"ok": True, "ignored": True, "reason": "payment_status_not_paid"}

    if not payment_intent_id:
        print("ℹ️ checkout.session.completed ignored because payment_intent could not be resolved")
        return {
            "ok": True,
            "ignored": True,
            "reason": "missing_payment_intent",
            "invoice_id": invoice_id,
            "subscription_id": subscription_id,
        }

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
        retrieved_intent_source=payment_intent_source,
        retrieved_intent_metadata=intent_metadata,
        invoice_id=invoice_id,
        subscription_id=subscription_id,
    )

    metadata = _merge_metadata(session_metadata, intent_metadata)
    metadata = _normalize_payment_metadata(db, metadata)

    _log_debug(
        "🔥 FINAL CHECKOUT METADATA",
        session_id=session_id,
        payment_intent_id=payment_intent_id,
        payment_intent_source=payment_intent_source,
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