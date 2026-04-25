# routers/billing.py
import calendar
import traceback
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from auth_utils import get_current_user
from database import get_db
from models import User, UserSubscription
from routers.billing_checkout import router as billing_checkout_router
from routers.billing_common import (
    STRIPE_WEBHOOK_SECRET,
    ensure_stripe_webhook_ready,
    _describe_exception,
    _log_debug,
)
from routers.billing_webhook_helpers import (
    _process_checkout_session_completed,
    _process_payment_intent_succeeded,
)

router = APIRouter(prefix="/billing", tags=["Billing"])
router.include_router(billing_checkout_router)


def _to_dt_utc_from_unix(ts_value):
    try:
        if ts_value is None:
            return None
        return datetime.fromtimestamp(int(ts_value), timezone.utc)
    except Exception:
        return None


def _ensure_utc(dt_value):
    if not dt_value:
        return None

    try:
        if dt_value.tzinfo is None:
            return dt_value.replace(tzinfo=timezone.utc)
        return dt_value.astimezone(timezone.utc)
    except Exception:
        return dt_value


def _add_one_month(dt_value):
    """
    Adds exactly one calendar month while preserving the day when possible.
    Example:
      Apr 15 -> May 15
      Jan 31 -> Feb 28/29
    """
    dt_value = _ensure_utc(dt_value)
    if not dt_value:
        return None

    year = dt_value.year
    month = dt_value.month + 1

    if month > 12:
        month = 1
        year += 1

    last_day = calendar.monthrange(year, month)[1]
    day = min(dt_value.day, last_day)

    return dt_value.replace(year=year, month=month, day=day)


def _get_cancel_benefits_expire_date(sub_row):
    """
    IMPORTANT BUSINESS RULE:
    If the user cancels a monthly plan, benefits expire one month after the
    original subscription active_date.

    This must NOT be recalculated from:
      - reactivation date
      - updated_at
      - cancellation date
      - Stripe subscription created date

    Example:
      active_date = Apr 15
      cancel Apr 24 -> expire May 15
      reactivate Apr 25
      cancel again Apr 29 -> still expire May 15
    """
    active_date = getattr(sub_row, "active_date", None)
    if active_date:
        return _add_one_month(active_date)

    current_period_start = getattr(sub_row, "current_period_start", None)
    if current_period_start:
        return _add_one_month(current_period_start)

    renewal_date = getattr(sub_row, "renewal_date", None)
    if renewal_date:
        return _ensure_utc(renewal_date)

    current_period_end = getattr(sub_row, "current_period_end", None)
    if current_period_end:
        return _ensure_utc(current_period_end)

    return None


def _update_user_subscription_from_stripe_subscription(
    db: Session,
    *,
    stripe_subscription_obj,
):
    if not stripe_subscription_obj:
        print("ℹ️ No Stripe subscription object provided")
        return {"ok": True, "ignored": True, "reason": "missing_subscription_object"}

    subscription_id = str(getattr(stripe_subscription_obj, "id", "") or "").strip()
    customer_id = str(getattr(stripe_subscription_obj, "customer", "") or "").strip()
    status = str(getattr(stripe_subscription_obj, "status", "") or "").strip()
    cancel_at_period_end = bool(
        getattr(stripe_subscription_obj, "cancel_at_period_end", False)
    )
    current_period_start = _to_dt_utc_from_unix(
        getattr(stripe_subscription_obj, "current_period_start", None)
    )
    current_period_end = _to_dt_utc_from_unix(
        getattr(stripe_subscription_obj, "current_period_end", None)
    )
    latest_invoice_id = str(
        getattr(stripe_subscription_obj, "latest_invoice", "") or ""
    ).strip()

    stripe_price_id = None
    try:
        items = getattr(stripe_subscription_obj, "items", None)
        data = getattr(items, "data", None) or []
        if data:
            first_item = data[0]
            price_obj = getattr(first_item, "price", None)
            stripe_price_id = str(getattr(price_obj, "id", "") or "").strip() or None
    except Exception:
        stripe_price_id = None

    if not subscription_id:
        print("ℹ️ subscription.updated/deleted ignored because subscription_id is missing")
        return {"ok": True, "ignored": True, "reason": "missing_subscription_id"}

    sub_row = (
        db.query(UserSubscription)
        .filter(UserSubscription.stripe_subscription_id == subscription_id)
        .first()
    )
    if not sub_row and customer_id:
        sub_row = (
            db.query(UserSubscription)
            .filter(UserSubscription.stripe_customer_id == customer_id)
            .first()
        )

    if not sub_row:
        print("⚠️ No matching user_subscriptions row found for Stripe subscription")
        print("   stripe_subscription_id:", subscription_id)
        print("   stripe_customer_id:", customer_id)
        return {"ok": True, "ignored": True, "reason": "subscription_row_not_found"}

    sub_row.stripe_customer_id = customer_id or None
    sub_row.stripe_subscription_id = subscription_id
    sub_row.stripe_price_id = stripe_price_id
    sub_row.subscription_status = status or None
    sub_row.cancel_at_period_end = cancel_at_period_end
    sub_row.current_period_start = current_period_start
    sub_row.current_period_end = current_period_end
    sub_row.last_invoice_id = latest_invoice_id or None

    if cancel_at_period_end:
        cancel_expire_date = _get_cancel_benefits_expire_date(sub_row)
        if cancel_expire_date:
            sub_row.renewal_date = cancel_expire_date
        elif current_period_end:
            sub_row.renewal_date = current_period_end
    else:
        if current_period_end:
            sub_row.renewal_date = current_period_end

    if status in {"active", "trialing"}:
        sub_row.is_active = True
    elif status in {"canceled", "unpaid", "incomplete_expired"}:
        sub_row.is_active = False

    db.commit()
    db.refresh(sub_row)

    print("✅ user_subscriptions updated from Stripe subscription")
    print("   user_id:", sub_row.user_id)
    print("   stripe_subscription_id:", sub_row.stripe_subscription_id)
    print("   subscription_status:", sub_row.subscription_status)
    print("   cancel_at_period_end:", sub_row.cancel_at_period_end)
    print("   current_period_end:", sub_row.current_period_end)
    print("   renewal_date:", sub_row.renewal_date)

    return {
        "ok": True,
        "user_id": sub_row.user_id,
        "stripe_subscription_id": sub_row.stripe_subscription_id,
        "subscription_status": sub_row.subscription_status,
        "renewal_date": sub_row.renewal_date.isoformat()
        if sub_row.renewal_date
        else None,
    }


def _process_invoice_payment_failed(db: Session, invoice_obj):
    subscription_id = str(getattr(invoice_obj, "subscription", "") or "").strip()
    customer_id = str(getattr(invoice_obj, "customer", "") or "").strip()
    invoice_id = str(getattr(invoice_obj, "id", "") or "").strip()

    sub_row = None

    if subscription_id:
        sub_row = (
            db.query(UserSubscription)
            .filter(UserSubscription.stripe_subscription_id == subscription_id)
            .first()
        )

    if not sub_row and customer_id:
        sub_row = (
            db.query(UserSubscription)
            .filter(UserSubscription.stripe_customer_id == customer_id)
            .first()
        )

    if not sub_row:
        print("⚠️ invoice.payment_failed: no matching user_subscriptions row found")
        print("   subscription_id:", subscription_id)
        print("   customer_id:", customer_id)
        return {"ok": True, "ignored": True, "reason": "subscription_row_not_found"}

    sub_row.subscription_status = "past_due"
    sub_row.last_invoice_id = invoice_id or None
    sub_row.is_active = False

    db.commit()
    db.refresh(sub_row)

    print("⚠️ invoice.payment_failed updated user_subscriptions")
    print("   user_id:", sub_row.user_id)
    print("   stripe_subscription_id:", sub_row.stripe_subscription_id)
    print("   subscription_status:", sub_row.subscription_status)

    return {
        "ok": True,
        "user_id": sub_row.user_id,
        "subscription_status": sub_row.subscription_status,
    }


def _find_active_subscription_for_customer(customer_id: str, deleted_subscription_id: str = ""):
    customer_id = str(customer_id or "").strip()
    deleted_subscription_id = str(deleted_subscription_id or "").strip()

    if not customer_id:
        return None

    active_statuses = ["active", "trialing", "past_due", "unpaid", "incomplete"]

    for status in active_statuses:
        try:
            subs = stripe.Subscription.list(
                customer=customer_id,
                status=status,
                limit=100,
            )
        except stripe.error.StripeError as e:
            print("❌ FAILED TO LIST CUSTOMER SUBSCRIPTIONS")
            print("   customer_id:", customer_id)
            print("   status:", status)
            print("   error:", str(e))
            raise

        for sub in subs.data:
            sid = str(getattr(sub, "id", "") or "").strip()
            if not sid:
                continue
            if deleted_subscription_id and sid == deleted_subscription_id:
                continue
            return sub

    return None


@router.post("/cancel-subscription")
def cancel_subscription(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_row = (
        db.query(UserSubscription)
        .filter(UserSubscription.user_id == current_user.id)
        .first()
    )

    if not sub_row:
        raise HTTPException(status_code=404, detail="Subscription record not found.")

    stripe_subscription_id = str(
        getattr(sub_row, "stripe_subscription_id", "") or ""
    ).strip()
    if not stripe_subscription_id:
        raise HTTPException(
            status_code=400,
            detail="No active Stripe subscription is linked to this user.",
        )

    current_status = str(
        getattr(sub_row, "subscription_status", "") or ""
    ).strip().lower()
    if current_status in {"canceled", "incomplete_expired"}:
        raise HTTPException(
            status_code=400,
            detail="Subscription is already canceled.",
        )

    cancel_expire_date = _get_cancel_benefits_expire_date(sub_row)

    if bool(getattr(sub_row, "cancel_at_period_end", False)):
        if cancel_expire_date and sub_row.renewal_date != cancel_expire_date:
            sub_row.renewal_date = cancel_expire_date
            db.commit()
            db.refresh(sub_row)

        return {
            "ok": True,
            "alreadyScheduled": True,
            "message": "Subscription is already scheduled to cancel at period end.",
            "subscriptionStatus": sub_row.subscription_status,
            "cancelAtPeriodEnd": sub_row.cancel_at_period_end,
            "renewalDate": sub_row.renewal_date.isoformat()
            if sub_row.renewal_date
            else None,
        }

    try:
        stripe_subscription = stripe.Subscription.modify(
            stripe_subscription_id,
            cancel_at_period_end=True,
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)}")

    _update_user_subscription_from_stripe_subscription(
        db=db,
        stripe_subscription_obj=stripe_subscription,
    )

    sub_row = (
        db.query(UserSubscription)
        .filter(UserSubscription.user_id == current_user.id)
        .first()
    )

    cancel_expire_date = _get_cancel_benefits_expire_date(sub_row)

    if cancel_expire_date:
        sub_row.renewal_date = cancel_expire_date
        sub_row.cancel_at_period_end = True
        db.commit()
        db.refresh(sub_row)

    current_period_end = _to_dt_utc_from_unix(
        getattr(stripe_subscription, "current_period_end", None)
    )

    return {
        "ok": True,
        "message": "Subscription will cancel at the end of the current billing period.",
        "subscriptionId": stripe_subscription_id,
        "subscriptionStatus": str(
            getattr(stripe_subscription, "status", "") or ""
        ).strip(),
        "cancelAtPeriodEnd": bool(
            getattr(stripe_subscription, "cancel_at_period_end", False)
        ),
        "currentPeriodEnd": current_period_end.isoformat()
        if current_period_end
        else None,
        "renewalDate": sub_row.renewal_date.isoformat()
        if sub_row.renewal_date
        else None,
        "benefitsExpireDate": sub_row.renewal_date.isoformat()
        if sub_row.renewal_date
        else None,
    }


@router.post("/reactivate-subscription")
def reactivate_subscription(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_row = (
        db.query(UserSubscription)
        .filter(UserSubscription.user_id == current_user.id)
        .first()
    )

    if not sub_row:
        raise HTTPException(status_code=404, detail="Subscription record not found.")

    stripe_subscription_id = str(
        getattr(sub_row, "stripe_subscription_id", "") or ""
    ).strip()
    if not stripe_subscription_id:
        raise HTTPException(
            status_code=400,
            detail="No active Stripe subscription is linked to this user.",
        )

    current_status = str(
        getattr(sub_row, "subscription_status", "") or ""
    ).strip().lower()
    if current_status in {"canceled", "incomplete_expired"}:
        raise HTTPException(
            status_code=400,
            detail="This subscription has already ended and cannot be reactivated.",
        )

    if not bool(getattr(sub_row, "cancel_at_period_end", False)):
        return {
            "ok": True,
            "alreadyActive": True,
            "message": "Subscription is already active and set to renew normally.",
            "subscriptionStatus": sub_row.subscription_status,
            "cancelAtPeriodEnd": sub_row.cancel_at_period_end,
            "renewalDate": sub_row.renewal_date.isoformat()
            if sub_row.renewal_date
            else None,
        }

    original_active_date = getattr(sub_row, "active_date", None)

    try:
        stripe_subscription = stripe.Subscription.modify(
            stripe_subscription_id,
            cancel_at_period_end=False,
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)}")

    _update_user_subscription_from_stripe_subscription(
        db=db,
        stripe_subscription_obj=stripe_subscription,
    )

    sub_row = (
        db.query(UserSubscription)
        .filter(UserSubscription.user_id == current_user.id)
        .first()
    )

    if original_active_date and getattr(sub_row, "active_date", None) != original_active_date:
        sub_row.active_date = original_active_date

    sub_row.cancel_at_period_end = False
    db.commit()
    db.refresh(sub_row)

    current_period_end = _to_dt_utc_from_unix(
        getattr(stripe_subscription, "current_period_end", None)
    )

    return {
        "ok": True,
        "message": "Subscription reactivated successfully. Your plan will continue to renew normally.",
        "subscriptionId": stripe_subscription_id,
        "subscriptionStatus": str(
            getattr(stripe_subscription, "status", "") or ""
        ).strip(),
        "cancelAtPeriodEnd": bool(
            getattr(stripe_subscription, "cancel_at_period_end", False)
        ),
        "currentPeriodEnd": current_period_end.isoformat()
        if current_period_end
        else None,
        "renewalDate": sub_row.renewal_date.isoformat()
        if sub_row.renewal_date
        else None,
        "activeDate": sub_row.active_date.isoformat()
        if getattr(sub_row, "active_date", None)
        else None,
    }


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

        elif event_type in {"invoice.payment_succeeded", "invoice_payment.paid"}:
            invoice_obj = data_object
            subscription_id = str(getattr(invoice_obj, "subscription", "") or "").strip()

            if not subscription_id:
                print("ℹ️ invoice paid event ignored because subscription_id is missing")
                return_value = {
                    "ok": True,
                    "ignored": True,
                    "reason": "missing_subscription_id",
                }
            else:
                try:
                    stripe_subscription_obj = stripe.Subscription.retrieve(
                        subscription_id
                    )
                    return_value = _update_user_subscription_from_stripe_subscription(
                        db=db,
                        stripe_subscription_obj=stripe_subscription_obj,
                    )
                except stripe.error.StripeError as e:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Stripe error: {str(e)}",
                    )

            _log_debug(
                f"✅ WEBHOOK {event_type} PROCESSED",
                event_id=event_id,
                result=return_value,
            )

        elif event_type == "invoice.payment_failed":
            return_value = _process_invoice_payment_failed(
                db=db,
                invoice_obj=data_object,
            )
            _log_debug(
                "⚠️ WEBHOOK invoice.payment_failed PROCESSED",
                event_id=event_id,
                result=return_value,
            )

        elif event_type == "customer.subscription.updated":
            return_value = _update_user_subscription_from_stripe_subscription(
                db=db,
                stripe_subscription_obj=data_object,
            )
            _log_debug(
                "✅ WEBHOOK customer.subscription.updated PROCESSED",
                event_id=event_id,
                result=return_value,
            )

        elif event_type == "customer.subscription.deleted":
            stripe_subscription_obj = data_object

            deleted_subscription_id = str(
                getattr(stripe_subscription_obj, "id", "") or ""
            ).strip()
            customer_id = str(
                getattr(stripe_subscription_obj, "customer", "") or ""
            ).strip()

            print("⚠️ customer.subscription.deleted received")
            print("   deleted_subscription_id:", deleted_subscription_id)
            print("   customer_id:", customer_id)

            active_subscription = _find_active_subscription_for_customer(
                customer_id=customer_id,
                deleted_subscription_id=deleted_subscription_id,
            )

            if active_subscription:
                active_subscription_id = str(
                    getattr(active_subscription, "id", "") or ""
                ).strip()

                print("✅ CUSTOMER STILL HAS AN ACTIVE SUBSCRIPTION")
                print("   active_subscription_id:", active_subscription_id)
                print("   deleted_subscription_id:", deleted_subscription_id)
                print("   ACTION: Do NOT downgrade to Free.")

                return_value = _update_user_subscription_from_stripe_subscription(
                    db=db,
                    stripe_subscription_obj=active_subscription,
                )

                return_value = {
                    **(return_value or {}),
                    "ignoredDeletedSubscription": True,
                    "reason": "customer_has_another_active_subscription",
                    "deletedSubscriptionId": deleted_subscription_id,
                    "keptSubscriptionId": active_subscription_id,
                }

                _log_debug(
                    "✅ WEBHOOK customer.subscription.deleted IGNORED OLD SUBSCRIPTION",
                    event_id=event_id,
                    result=return_value,
                )

            else:
                print("🔥 NO OTHER ACTIVE SUBSCRIPTION FOUND")
                print("   ACTION: downgrade user to Free")

                sub_row = None

                if deleted_subscription_id:
                    sub_row = (
                        db.query(UserSubscription)
                        .filter(
                            UserSubscription.stripe_subscription_id
                            == deleted_subscription_id
                        )
                        .first()
                    )

                if not sub_row and customer_id:
                    sub_row = (
                        db.query(UserSubscription)
                        .filter(UserSubscription.stripe_customer_id == customer_id)
                        .first()
                    )

                if sub_row:
                    print("🔥 DOWNGRADING USER TO FREE PLAN")
                    print("   user_id:", sub_row.user_id)
                    print("   old_plan_key:", sub_row.plan_key)
                    print("   old_device_limit:", sub_row.device_limit)
                    print("   old_tenants_users_limit:", sub_row.tenants_users_limit)

                    sub_row.plan_key = "free"
                    sub_row.device_limit = 1
                    sub_row.tenants_users_limit = 1
                    sub_row.subscription_status = "canceled"
                    sub_row.is_active = True
                    sub_row.stripe_subscription_id = None
                    sub_row.stripe_price_id = None
                    sub_row.cancel_at_period_end = False
                    sub_row.current_period_start = None
                    sub_row.current_period_end = None
                    sub_row.renewal_date = None

                    db.commit()
                    db.refresh(sub_row)

                    print("✅ USER DOWNGRADED TO FREE")
                    print("   user_id:", sub_row.user_id)
                    print("   new_plan_key:", sub_row.plan_key)
                    print("   new_device_limit:", sub_row.device_limit)
                    print("   new_tenants_users_limit:", sub_row.tenants_users_limit)

                    return_value = {
                        "ok": True,
                        "downgradedToFree": True,
                        "planKey": sub_row.plan_key,
                        "deviceLimit": sub_row.device_limit,
                        "tenantsUsersLimit": sub_row.tenants_users_limit,
                    }
                else:
                    print(
                        "⚠️ customer.subscription.deleted: could not find subscription row to downgrade"
                    )
                    return_value = {
                        "ok": True,
                        "ignored": True,
                        "downgradedToFree": False,
                        "reason": "subscription_row_not_found_for_downgrade",
                    }

                _log_debug(
                    "✅ WEBHOOK customer.subscription.deleted PROCESSED",
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