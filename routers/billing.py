# routers/billing.py
import traceback
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db
from models import UserSubscription
from routers.billing_checkout import router as billing_checkout_router
from routers.billing_common import (
    STRIPE_WEBHOOK_SECRET,
    ensure_stripe_webhook_ready,
    _describe_exception,
    _log_debug,
    _normalize_payment_metadata,
    _safe_metadata_dict,
    _apply_payment_effects,
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

    return {
        "ok": True,
        "user_id": sub_row.user_id,
        "stripe_subscription_id": sub_row.stripe_subscription_id,
        "subscription_status": sub_row.subscription_status,
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

            payment_intent_id = ""
            raw_payment_intent = getattr(invoice_obj, "payment_intent", None)

            if isinstance(raw_payment_intent, str):
                payment_intent_id = str(raw_payment_intent or "").strip()
            else:
                payment_intent_id = str(
                    getattr(raw_payment_intent, "id", "") or ""
                ).strip()

            if not payment_intent_id:
                print("ℹ️ invoice paid event ignored because payment_intent is missing")
                return_value = {
                    "ok": True,
                    "ignored": True,
                    "reason": "missing_payment_intent",
                }
            else:
                intent = stripe.PaymentIntent.retrieve(payment_intent_id)
                metadata = _safe_metadata_dict(getattr(intent, "metadata", None))
                metadata = _normalize_payment_metadata(db, metadata)

                return_value = _apply_payment_effects(
                    db=db,
                    payment_intent_id=payment_intent_id,
                    metadata=metadata,
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
            return_value = _update_user_subscription_from_stripe_subscription(
                db=db,
                stripe_subscription_obj=data_object,
            )
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