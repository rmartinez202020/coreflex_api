import os
import traceback
import calendar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import stripe
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import User, BillingPlan, UserSubscription, SubscriptionAgreementAcceptance

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


def _add_one_calendar_month(dt_value):
    if not dt_value:
        return None

    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    else:
        dt_value = dt_value.astimezone(timezone.utc)

    year = dt_value.year
    month = dt_value.month + 1

    if month > 12:
        month = 1
        year += 1

    last_day = calendar.monthrange(year, month)[1]
    day = min(dt_value.day, last_day)

    return dt_value.replace(year=year, month=month, day=day)


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


def _metadata_from_client_reference_id(value: str) -> dict:
    text = str(value or "").strip()
    if not text:
        return {}

    parsed = {}
    for part in text.split(";"):
        part = str(part or "").strip()
        if not part or "=" not in part:
            continue

        key, raw_value = part.split("=", 1)
        key = str(key or "").strip()
        raw_value = str(raw_value or "").strip()

        if key and raw_value:
            parsed[key] = raw_value

    if parsed.get("uid") and not parsed.get("user_id"):
        parsed["user_id"] = parsed["uid"]

    if parsed.get("plan") and not parsed.get("plan_key"):
        parsed["plan_key"] = parsed["plan"]

    if parsed.get("plan_key") and not parsed.get("billing_type"):
        parsed["billing_type"] = "one_time"

    return parsed


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


def _save_one_time_license_acceptance(
    db: Session,
    *,
    user_id: int,
    plan_key: str,
    payment_intent_id: str,
    confirmed_at,
):
    plan_key = str(plan_key or "").strip().lower()
    payment_intent_id = str(payment_intent_id or "").strip()

    if not user_id or not plan_key:
        print("⚠️ SKIPPING ONE-TIME ACCEPTANCE SAVE: missing user_id or plan_key")
        return None

    existing = None

    if payment_intent_id:
        existing = (
            db.query(SubscriptionAgreementAcceptance)
            .filter(
                SubscriptionAgreementAcceptance.user_id == user_id,
                SubscriptionAgreementAcceptance.payment_intent_id == payment_intent_id,
            )
            .first()
        )

    if not existing:
        existing = (
            db.query(SubscriptionAgreementAcceptance)
            .filter(
                SubscriptionAgreementAcceptance.user_id == user_id,
                SubscriptionAgreementAcceptance.plan_key == plan_key,
                SubscriptionAgreementAcceptance.billing_type == "one_time",
                SubscriptionAgreementAcceptance.confirmed.is_(True),
            )
            .order_by(SubscriptionAgreementAcceptance.id.desc())
            .first()
        )

    if existing:
        existing.plan_key = plan_key
        existing.billing_type = "one_time"
        existing.agreement_version = getattr(existing, "agreement_version", None) or "v1"
        existing.confirmed = True
        existing.confirmed_at = getattr(existing, "confirmed_at", None) or confirmed_at

        if hasattr(existing, "payment_intent_id") and payment_intent_id:
            existing.payment_intent_id = payment_intent_id

        db.commit()
        db.refresh(existing)

        print("✅ ONE-TIME LICENSE ACCEPTANCE ALREADY EXISTS / UPDATED")
        print("   id:", getattr(existing, "id", None))
        print("   user_id:", getattr(existing, "user_id", None))
        print("   plan_key:", getattr(existing, "plan_key", None))
        print("   billing_type:", getattr(existing, "billing_type", None))
        print("   confirmed_at:", getattr(existing, "confirmed_at", None))
        print("   payment_intent_id:", getattr(existing, "payment_intent_id", None))

        return existing

    row = SubscriptionAgreementAcceptance(
        user_id=user_id,
        plan_key=plan_key,
        billing_type="one_time",
        agreement_version="v1",
        confirmed=True,
        confirmed_at=confirmed_at,
    )

    if hasattr(row, "payment_intent_id") and payment_intent_id:
        row.payment_intent_id = payment_intent_id

    db.add(row)
    db.commit()
    db.refresh(row)

    print("✅ ONE-TIME LICENSE ACCEPTANCE SAVED")
    print("   id:", getattr(row, "id", None))
    print("   user_id:", getattr(row, "user_id", None))
    print("   plan_key:", getattr(row, "plan_key", None))
    print("   billing_type:", getattr(row, "billing_type", None))
    print("   confirmed_at:", getattr(row, "confirmed_at", None))
    print("   payment_intent_id:", getattr(row, "payment_intent_id", None))

    return row


def _apply_payment_effects(
    db: Session,
    *,
    payment_intent_id: str,
    metadata: dict,
):
    from routers.billing_purchase_helpers import _get_or_create_user_subscription

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

    print("🔥 DB SUBSCRIPTION BEFORE UPDATE")
    print("   subscription.id:", getattr(subscription, "id", None))
    print("   subscription.user_id:", getattr(subscription, "user_id", None))
    print("   subscription.plan_key:", getattr(subscription, "plan_key", None))
    print("   subscription.tenants_users_limit:", getattr(subscription, "tenants_users_limit", None))
    print("   subscription.device_limit:", getattr(subscription, "device_limit", None))
    print("   subscription.active_date:", getattr(subscription, "active_date", None))
    print("   subscription.renewal_date:", getattr(subscription, "renewal_date", None))
    print("   subscription.cancel_at_period_end:", getattr(subscription, "cancel_at_period_end", None))

    current_plan_key = str(subscription.plan_key or "free").strip().lower()
    current_limit = int(subscription.tenants_users_limit or 0)
    current_device_limit = int(subscription.device_limit or 0)

    plan_amount_usd = to_money_decimal(metadata.get("plan_amount_usd"))
    addon_amount_usd = to_money_decimal(metadata.get("addon_amount_usd"))
    checkout_type = str(metadata.get("checkout_type") or "").strip().lower()

    is_addon_only_purchase = (
        extra_tenant_users > 0
        and (
            checkout_type == "tenant_user_addon_only"
            or (
                is_current_plan
                and plan_amount_usd <= Decimal("0.00")
                and addon_amount_usd > Decimal("0.00")
            )
        )
    )

    print("🔥 ADDON ONLY DETECTION")
    print("   checkout_type:", checkout_type)
    print("   plan_amount_usd:", plan_amount_usd)
    print("   addon_amount_usd:", addon_amount_usd)
    print("   is_addon_only_purchase:", is_addon_only_purchase)

    plan = None

    if not is_addon_only_purchase:
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

        target_plan_tenant_amount = int(plan.tenant_user_limit or 0)
        base_device_limit = int(plan.device_limit or 0)
    else:
        print("✅ SKIPPING BILLING PLAN LOOKUP FOR TENANT-USER ADDON ONLY PURCHASE")
        target_plan_tenant_amount = current_limit
        base_device_limit = current_device_limit

    plan_is_changing = plan_key != current_plan_key
    now_utc = _utcnow()

    print("🔥 APPLY PAYMENT EFFECTS FINAL VALUES")
    print("   user_id:", user_id)
    print("   current_plan_key:", current_plan_key)
    print("   target_plan_key:", plan_key)
    print("   billing_type:", billing_type)
    print("   is_current_plan:", is_current_plan)
    print("   is_addon_only_purchase:", is_addon_only_purchase)
    print("   plan_is_changing:", plan_is_changing)
    print("   extra_tenant_users:", extra_tenant_users)
    print("   already_applied:", already_applied)
    print("   current_limit:", current_limit)

    is_one_time_license_purchase = billing_type == "one_time" and not is_addon_only_purchase

    if is_addon_only_purchase:
        new_plan_key = current_plan_key
        new_device_limit = current_device_limit or base_device_limit

        if already_applied:
            expected_minimum_limit = current_limit + extra_tenant_users

            new_tenant_limit = expected_minimum_limit
            added_from_plan = 0
            added_from_addons = extra_tenant_users
        else:
            new_tenant_limit = current_limit + extra_tenant_users
            added_from_plan = 0
            added_from_addons = extra_tenant_users

        if not getattr(subscription, "active_date", None):
            subscription.active_date = now_utc

        if hasattr(subscription, "renewal_date"):
            if billing_type == "monthly" and not getattr(subscription, "renewal_date", None):
                subscription.renewal_date = _add_one_calendar_month(
                    getattr(subscription, "active_date", None) or now_utc
                )

    elif is_one_time_license_purchase:
        new_plan_key = plan_key
        new_device_limit = base_device_limit
        new_tenant_limit = target_plan_tenant_amount + extra_tenant_users

        added_from_plan = max(0, target_plan_tenant_amount - current_limit)
        added_from_addons = extra_tenant_users

        subscription.active_date = now_utc
        subscription.renewal_date = None

        if hasattr(subscription, "cancel_at_period_end"):
            subscription.cancel_at_period_end = False

        if hasattr(subscription, "subscription_status"):
            subscription.subscription_status = "paid"

        if hasattr(subscription, "status"):
            subscription.status = "Paid"

    elif plan_is_changing or not is_current_plan:
        new_plan_key = plan_key
        new_device_limit = base_device_limit
        new_tenant_limit = target_plan_tenant_amount + extra_tenant_users

        added_from_plan = max(0, target_plan_tenant_amount - current_limit)
        added_from_addons = extra_tenant_users

        subscription.active_date = now_utc

        if billing_type == "monthly":
            subscription.renewal_date = _add_one_calendar_month(now_utc)
        else:
            subscription.renewal_date = None

        if hasattr(subscription, "cancel_at_period_end"):
            subscription.cancel_at_period_end = False

        if hasattr(subscription, "subscription_status"):
            subscription.subscription_status = "active"

        if hasattr(subscription, "status"):
            subscription.status = "Active"

    else:
        new_plan_key = current_plan_key
        new_device_limit = current_device_limit or base_device_limit

        if extra_tenant_users > 0:
            expected_minimum_limit = target_plan_tenant_amount + extra_tenant_users

            if already_applied:
                if current_limit < expected_minimum_limit:
                    new_tenant_limit = expected_minimum_limit
                    added_from_plan = 0
                    added_from_addons = max(0, new_tenant_limit - current_limit)
                else:
                    new_tenant_limit = current_limit
                    added_from_plan = 0
                    added_from_addons = 0
            else:
                new_tenant_limit = current_limit + extra_tenant_users
                added_from_plan = 0
                added_from_addons = extra_tenant_users
        else:
            new_tenant_limit = current_limit
            added_from_plan = 0
            added_from_addons = 0

        if not getattr(subscription, "active_date", None):
            subscription.active_date = now_utc

        if hasattr(subscription, "renewal_date"):
            if billing_type == "monthly" and not getattr(subscription, "renewal_date", None):
                subscription.renewal_date = _add_one_calendar_month(
                    getattr(subscription, "active_date", None) or now_utc
                )
            elif billing_type != "monthly":
                subscription.renewal_date = None

    _log_debug(
        "🔥 SUBSCRIPTION UPDATE PREVIEW",
        current_plan_key=current_plan_key,
        current_limit=current_limit,
        current_device_limit=current_device_limit,
        target_plan_tenant_amount=target_plan_tenant_amount,
        base_device_limit=base_device_limit,
        plan_is_changing=plan_is_changing,
        already_applied=already_applied,
        is_addon_only_purchase=is_addon_only_purchase,
        is_one_time_license_purchase=is_one_time_license_purchase,
        new_plan_key=new_plan_key,
        new_tenant_limit=new_tenant_limit,
        new_device_limit=new_device_limit,
        added_from_plan=added_from_plan,
        added_from_addons=added_from_addons,
        active_date=getattr(subscription, "active_date", None),
        renewal_date=getattr(subscription, "renewal_date", None),
    )

    subscription.plan_key = new_plan_key
    subscription.device_limit = new_device_limit
    subscription.tenants_users_limit = new_tenant_limit
    subscription.is_active = True

    try:
        print("🔥 ABOUT TO SAVE SUBSCRIPTION")
        print("   subscription.user_id:", subscription.user_id)
        print("   subscription.plan_key:", subscription.plan_key)
        print("   subscription.tenants_users_limit:", subscription.tenants_users_limit)
        print("   subscription.device_limit:", subscription.device_limit)
        print("   subscription.active_date:", getattr(subscription, "active_date", None))
        print("   subscription.renewal_date:", getattr(subscription, "renewal_date", None))
        print("   subscription.cancel_at_period_end:", getattr(subscription, "cancel_at_period_end", None))

        print("🔥 DB COMMIT START")
        db.commit()
        print("✅ DB COMMIT OK")
    except Exception:
        db.rollback()
        print("❌ DB COMMIT FAILED - ROLLBACK DONE")
        traceback.print_exc()
        raise

    db.refresh(subscription)

    one_time_acceptance = None

    if is_one_time_license_purchase:
        try:
            one_time_acceptance = _save_one_time_license_acceptance(
                db=db,
                user_id=user_id,
                plan_key=plan_key,
                payment_intent_id=payment_intent_id,
                confirmed_at=now_utc,
            )
        except Exception:
            db.rollback()
            print("❌ FAILED TO SAVE ONE-TIME LICENSE ACCEPTANCE")
            traceback.print_exc()
            raise

        try:
            db.refresh(subscription)
        except Exception:
            pass

    print("🔥 DB SUBSCRIPTION AFTER UPDATE")
    print("   subscription.id:", getattr(subscription, "id", None))
    print("   subscription.user_id:", getattr(subscription, "user_id", None))
    print("   subscription.plan_key:", getattr(subscription, "plan_key", None))
    print("   subscription.tenants_users_limit:", getattr(subscription, "tenants_users_limit", None))
    print("   subscription.device_limit:", getattr(subscription, "device_limit", None))
    print("   subscription.active_date:", getattr(subscription, "active_date", None))
    print("   subscription.renewal_date:", getattr(subscription, "renewal_date", None))
    print("   subscription.cancel_at_period_end:", getattr(subscription, "cancel_at_period_end", None))

    if not already_applied:
        _mark_payment_intent_applied(payment_intent_id, metadata)
    else:
        print("✅ PAYMENT WAS ALREADY MARKED APPLIED, DB WAS RECONCILED ONLY")

    return {
        "ok": True,
        "alreadyApplied": already_applied,
        "added": added_from_plan + added_from_addons,
        "planKey": str(subscription.plan_key or "free").strip().lower(),
        "billingType": billing_type,
        "isAddonOnlyPurchase": is_addon_only_purchase,
        "isOneTimeLicensePurchase": is_one_time_license_purchase,
        "tenantsUsersLimit": int(subscription.tenants_users_limit or 0),
        "tenantUsersUsed": None,
        "activeDate": subscription.active_date.isoformat()
        if getattr(subscription, "active_date", None)
        else None,
        "renewalDate": subscription.renewal_date.isoformat()
        if getattr(subscription, "renewal_date", None)
        else None,
        "oneTimeAcceptanceId": getattr(one_time_acceptance, "id", None)
        if one_time_acceptance
        else None,
        "oneTimePaidAt": getattr(one_time_acceptance, "confirmed_at", None).isoformat()
        if one_time_acceptance and getattr(one_time_acceptance, "confirmed_at", None)
        else None,
        "message": "Payment applied successfully.",
    }