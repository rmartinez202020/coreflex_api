# routers/billing.py
import traceback

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db
from routers.billing_checkout import router as billing_checkout_router
from routers.billing_common import (
    STRIPE_WEBHOOK_SECRET,
    ensure_stripe_webhook_ready,
    _log_debug,
    _describe_exception,
    _process_checkout_session_completed,
    _process_payment_intent_succeeded,
    _safe_metadata_dict,
    _normalize_payment_metadata,
    _apply_payment_effects,
)

router = APIRouter(prefix="/billing", tags=["Billing"])
router.include_router(billing_checkout_router)


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