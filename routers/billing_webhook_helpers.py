import stripe
from fastapi import HTTPException
from sqlalchemy.orm import Session

from routers.billing_common import (
    _apply_payment_effects,
    _describe_exception,
    _log_debug,
    _merge_metadata,
    _metadata_from_client_reference_id,
    _normalize_payment_metadata,
    _safe_metadata_dict,
)


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


def _extract_checkout_session_data(session_obj):
    session_id = str(getattr(session_obj, "id", "") or "").strip()
    payment_status = str(
        getattr(session_obj, "payment_status", "") or ""
    ).strip().lower()

    metadata = _safe_metadata_dict(getattr(session_obj, "metadata", None))
    if not metadata:
        metadata = _metadata_from_client_reference_id(
            getattr(session_obj, "client_reference_id", None)
        )

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

    if not session_metadata:
        try:
            ref = str(getattr(session_obj, "client_reference_id", "") or "").strip()
            print("⚠️ FALLBACK USING client_reference_id:", ref)
            session_metadata = _metadata_from_client_reference_id(ref)
            print("✅ REBUILT METADATA FROM client_reference_id:", session_metadata)
        except Exception as e:
            print("❌ FAILED TO PARSE client_reference_id:", e)
            session_metadata = {}

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
        print("⚠️ checkout.session.completed has no resolved payment_intent_id")
        print("⚠️ continuing with session metadata fallback")
        print("   invoice_id:", invoice_id)
        print("   subscription_id:", subscription_id)

        metadata = _normalize_payment_metadata(db, session_metadata)

        _log_debug(
            "🔥 FINAL CHECKOUT METADATA (NO PAYMENT INTENT)",
            session_id=session_id,
            payment_intent_id=payment_intent_id,
            payment_intent_source=payment_intent_source,
            session_metadata=session_metadata,
            merged_metadata=metadata,
            invoice_id=invoice_id,
            subscription_id=subscription_id,
        )

        if not str(metadata.get("user_id") or "").strip().isdigit():
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid payment metadata: user_id. "
                    f"session_metadata={session_metadata} "
                    f"invoice_id={invoice_id} "
                    f"subscription_id={subscription_id}"
                ),
            )

        return _apply_payment_effects(
            db=db,
            payment_intent_id="",
            metadata=metadata,
        )

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

    if not metadata or not str(metadata.get("user_id") or "").strip():
        client_ref = getattr(session_obj, "client_reference_id", None)
        fallback = _metadata_from_client_reference_id(client_ref)

        print("⚠️ FALLBACK USING client_reference_id:", client_ref)
        print("⚠️ PARSED FALLBACK:", fallback)

        metadata = _merge_metadata(metadata, fallback)

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