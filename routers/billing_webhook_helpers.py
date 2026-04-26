import stripe
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import UserSubscription, SubscriptionAgreementAcceptance

try:
    from models import OneTimePaymentHistory
except Exception:
    OneTimePaymentHistory = None

from routers.billing_common import (
    _apply_payment_effects,
    _describe_exception,
    _log_debug,
    _merge_metadata,
    _metadata_from_client_reference_id,
    _normalize_payment_metadata,
    _safe_metadata_dict,
)


def _utcnow():
    return datetime.now(timezone.utc)


def _safe_decimal(value, default="0.00"):
    try:
        return Decimal(str(value or default)).quantize(Decimal("0.01"))
    except Exception:
        return Decimal(default).quantize(Decimal("0.01"))


def _save_one_time_purchase_records(
    db: Session,
    *,
    session_id: str,
    payment_intent_id: str,
    customer_id: str,
    payment_status: str,
    metadata: dict,
):
    metadata = _normalize_payment_metadata(db, _safe_metadata_dict(metadata))

    if str(metadata.get("billing_type") or "").strip().lower() != "one_time":
        return {"ok": True, "ignored": True, "reason": "not_one_time"}

    raw_user_id = str(metadata.get("user_id") or "").strip()
    if not raw_user_id.isdigit():
        print("⚠️ Cannot save one-time purchase records: missing user_id")
        return {"ok": False, "reason": "missing_user_id"}

    user_id = int(raw_user_id)
    plan_key = str(metadata.get("plan_key") or "").strip().lower()
    user_email = str(metadata.get("user_email") or "").strip() or None
    now_utc = _utcnow()

    if not plan_key:
        print("⚠️ Cannot save one-time purchase records: missing plan_key")
        return {"ok": False, "reason": "missing_plan_key"}

    agreement = (
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

    if not agreement:
        agreement = SubscriptionAgreementAcceptance(
            user_id=user_id,
            plan_key=plan_key,
            billing_type="one_time",
            agreement_version="v1",
            confirmed=True,
            confirmed_at=now_utc,
        )
        db.add(agreement)
    else:
        agreement.confirmed = True
        agreement.confirmed_at = agreement.confirmed_at or now_utc

    if hasattr(agreement, "checkout_session_id") and session_id:
        agreement.checkout_session_id = session_id

    if hasattr(agreement, "payment_intent_id") and payment_intent_id:
        agreement.payment_intent_id = payment_intent_id

    history_id = None

    if OneTimePaymentHistory is not None:
        history = None

        if payment_intent_id:
            history = (
                db.query(OneTimePaymentHistory)
                .filter(
                    OneTimePaymentHistory.stripe_payment_intent_id
                    == payment_intent_id
                )
                .first()
            )

        if not history and session_id:
            history = (
                db.query(OneTimePaymentHistory)
                .filter(
                    OneTimePaymentHistory.stripe_checkout_session_id == session_id
                )
                .first()
            )

        if not history:
            history = OneTimePaymentHistory(
                user_id=user_id,
                user_email=user_email,
                plan_key=plan_key,
                billing_type="one_time",
                stripe_checkout_session_id=session_id or None,
                stripe_payment_intent_id=payment_intent_id or None,
                stripe_customer_id=customer_id or None,
                amount_usd=_safe_decimal(metadata.get("plan_amount_usd")),
                tax_amount_usd=_safe_decimal(metadata.get("tax_amount_usd")),
                total_usd=_safe_decimal(metadata.get("total_usd")),
                currency="usd",
                payment_status=payment_status or "paid",
                paid_at=now_utc,
                metadata_json=metadata,
            )
            db.add(history)

        db.commit()
        db.refresh(history)
        history_id = getattr(history, "id", None)
    else:
        db.commit()

    db.refresh(agreement)

    print("✅ ONE-TIME PURCHASE RECORDS SAVED")
    print("   agreement_id:", getattr(agreement, "id", None))
    print("   history_id:", history_id)
    print("   user_id:", user_id)
    print("   plan_key:", plan_key)
    print("   payment_intent_id:", payment_intent_id)

    return {
        "ok": True,
        "agreementId": getattr(agreement, "id", None),
        "historyId": history_id,
    }


def _cancel_all_billable_subscriptions_for_customer(customer_id: str):
    customer_id = str(customer_id or "").strip()
    if not customer_id:
        return {"ok": True, "cancelled": 0, "reason": "missing_customer_id"}

    statuses = ["active", "trialing", "past_due", "unpaid", "incomplete"]
    cancelled = 0
    cancelled_subscription_ids = []

    for status in statuses:
        subs = stripe.Subscription.list(customer=customer_id, status=status, limit=100)

        for sub in subs.data:
            sid = str(getattr(sub, "id", "") or "").strip()
            if not sid:
                continue

            print("🔥 ONE-TIME PURCHASE: CANCELING MONTHLY SUBSCRIPTION:", sid)
            print("   cleanup_mode: customer_id")
            print("   customer_id:", customer_id)

            stripe.Subscription.delete(
                sid,
                prorate=False,
            )

            cancelled += 1
            cancelled_subscription_ids.append(sid)

    result = {
        "ok": True,
        "customer_id": customer_id,
        "cancelled": cancelled,
        "cancelled_subscription_ids": cancelled_subscription_ids,
    }
    print("✅ ONE-TIME MONTHLY CLEANUP RESULT:", result)
    return result


def _cancel_all_billable_subscriptions_for_user_email(user_email: str):
    user_email = str(user_email or "").strip().lower()
    if not user_email:
        return {"ok": True, "cancelled": 0, "reason": "missing_user_email"}

    statuses = ["active", "trialing", "past_due", "unpaid", "incomplete"]
    cancelled = 0
    cancelled_subscription_ids = []
    customer_ids = []

    customers = stripe.Customer.list(email=user_email, limit=100)

    for customer in customers.data:
        customer_id = str(getattr(customer, "id", "") or "").strip()
        if not customer_id:
            continue

        customer_ids.append(customer_id)

        for status in statuses:
            subs = stripe.Subscription.list(
                customer=customer_id,
                status=status,
                limit=100,
            )

            for sub in subs.data:
                sid = str(getattr(sub, "id", "") or "").strip()
                if not sid:
                    continue

                print("🔥 ONE-TIME PURCHASE: CANCELING MONTHLY SUBSCRIPTION:", sid)
                print("   cleanup_mode: user_email")
                print("   user_email:", user_email)
                print("   customer_id:", customer_id)

                stripe.Subscription.delete(
                    sid,
                    prorate=False,
                )

                cancelled += 1
                cancelled_subscription_ids.append(sid)

    result = {
        "ok": True,
        "user_email": user_email,
        "customer_ids": customer_ids,
        "cancelled": cancelled,
        "cancelled_subscription_ids": cancelled_subscription_ids,
    }

    print("✅ ONE-TIME EMAIL CLEANUP RESULT:", result)
    return result


def _cancel_monthly_subscriptions_after_one_time_purchase(
    *,
    user_email: str,
    customer_id: str,
):
    email_result = None
    customer_result = None

    try:
        email_result = _cancel_all_billable_subscriptions_for_user_email(user_email)
    except Exception as e:
        print("❌ FAILED ONE-TIME EMAIL MONTHLY CLEANUP:", e)
        email_result = {
            "ok": False,
            "reason": "email_cleanup_failed",
            "error": str(e),
        }

    try:
        customer_result = _cancel_all_billable_subscriptions_for_customer(customer_id)
    except Exception as e:
        print("❌ FAILED ONE-TIME CUSTOMER MONTHLY CLEANUP:", e)
        customer_result = {
            "ok": False,
            "reason": "customer_cleanup_failed",
            "error": str(e),
        }

    total_cancelled = 0
    cancelled_subscription_ids = []

    for item in [email_result, customer_result]:
        if not isinstance(item, dict):
            continue

        try:
            total_cancelled += int(item.get("cancelled") or 0)
        except Exception:
            pass

        ids = item.get("cancelled_subscription_ids") or []
        if isinstance(ids, list):
            for sid in ids:
                sid = str(sid or "").strip()
                if sid and sid not in cancelled_subscription_ids:
                    cancelled_subscription_ids.append(sid)

    result = {
        "ok": True,
        "user_email": str(user_email or "").strip().lower(),
        "customer_id": str(customer_id or "").strip(),
        "cancelled": total_cancelled,
        "cancelled_subscription_ids": cancelled_subscription_ids,
        "email_cleanup": email_result,
        "customer_cleanup": customer_result,
    }

    print("✅ ONE-TIME FINAL MONTHLY CLEANUP RESULT:", result)
    return result


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


def _cancel_previous_active_subscriptions_for_same_customer(
    *,
    keep_subscription_id: str,
):
    keep_sid = str(keep_subscription_id or "").strip()
    if not keep_sid:
        return {"ok": True, "cancelled": 0, "kept": None}

    try:
        keep_sub = stripe.Subscription.retrieve(keep_sid)
        customer_id = str(getattr(keep_sub, "customer", "") or "").strip()

        if not customer_id:
            print("⚠️ No Stripe customer found for subscription:", keep_sid)
            return {"ok": True, "cancelled": 0, "kept": keep_sid}

        subs = stripe.Subscription.list(
            customer=customer_id,
            status="all",
            limit=100,
        )

        cancelled_count = 0
        skipped_count = 0

        for sub in subs.data:
            sid = str(getattr(sub, "id", "") or "").strip()
            status = str(getattr(sub, "status", "") or "").strip().lower()

            if not sid:
                continue

            if sid == keep_sid:
                print("✅ KEEPING NEW CURRENT SUBSCRIPTION:", sid)
                skipped_count += 1
                continue

            if status in {"canceled", "incomplete_expired"}:
                skipped_count += 1
                continue

            if status in {"active", "trialing", "past_due", "unpaid", "incomplete"}:
                print("🛑 CANCELING PREVIOUS SUBSCRIPTION FOR SAME CUSTOMER:", sid)
                print("   customer_id:", customer_id)
                print("   old_status:", status)
                print("   keeping:", keep_sid)

                stripe.Subscription.delete(
                    sid,
                    prorate=False,
                )

                cancelled_count += 1

        result = {
            "ok": True,
            "customer_id": customer_id,
            "cancelled": cancelled_count,
            "skipped": skipped_count,
            "kept": keep_sid,
        }

        print("✅ STRIPE PREVIOUS SUBSCRIPTION CLEANUP COMPLETE:", result)
        return result

    except stripe.error.StripeError as e:
        print(
            "❌ FAILED TO CANCEL PREVIOUS STRIPE SUBSCRIPTIONS",
            keep_sid,
            _describe_exception(e),
        )
        raise


def _extract_payment_intent_id_from_invoice(invoice_obj) -> str:
    if not invoice_obj:
        return ""

    payment_intent = getattr(invoice_obj, "payment_intent", None)
    if isinstance(payment_intent, str):
        return str(payment_intent or "").strip()

    return str(getattr(payment_intent, "id", "") or "").strip()


def _resolve_checkout_session_payment_intent(session_obj):
    invoice_id = ""

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


def _to_dt_utc_from_unix(ts_value):
    try:
        if ts_value is None:
            return None
        return datetime.fromtimestamp(int(ts_value), timezone.utc)
    except Exception:
        return None


def _save_subscription_state_from_stripe(
    db: Session,
    *,
    user_id: int,
    subscription_id: str,
):
    sid = str(subscription_id or "").strip()
    if not sid:
        print("ℹ️ No subscription_id to save into user_subscriptions")
        return

    try:
        stripe_sub = stripe.Subscription.retrieve(sid)
    except stripe.error.StripeError as e:
        print(
            "⚠️ FAILED TO RETRIEVE SUBSCRIPTION FOR DB SAVE",
            sid,
            _describe_exception(e),
        )
        return

    customer_id = str(getattr(stripe_sub, "customer", "") or "").strip()
    status = str(getattr(stripe_sub, "status", "") or "").strip()
    cancel_at_period_end = bool(getattr(stripe_sub, "cancel_at_period_end", False))
    current_period_start = _to_dt_utc_from_unix(
        getattr(stripe_sub, "current_period_start", None)
    )
    current_period_end = _to_dt_utc_from_unix(
        getattr(stripe_sub, "current_period_end", None)
    )
    latest_invoice_id = str(getattr(stripe_sub, "latest_invoice", "") or "").strip()

    stripe_price_id = None
    try:
        items = getattr(stripe_sub, "items", None)
        data = getattr(items, "data", None) or []
        if data:
            first_item = data[0]
            price_obj = getattr(first_item, "price", None)
            stripe_price_id = str(getattr(price_obj, "id", "") or "").strip() or None
    except Exception:
        stripe_price_id = None

    sub_row = (
        db.query(UserSubscription)
        .filter(UserSubscription.user_id == user_id)
        .first()
    )
    if not sub_row:
        print("⚠️ user_subscriptions row not found for user_id:", user_id)
        return

    sub_row.stripe_customer_id = customer_id or None
    sub_row.stripe_subscription_id = sid
    sub_row.stripe_price_id = stripe_price_id
    sub_row.subscription_status = status or None
    sub_row.cancel_at_period_end = cancel_at_period_end
    sub_row.current_period_start = current_period_start
    sub_row.current_period_end = current_period_end
    sub_row.last_invoice_id = latest_invoice_id or None

    if status in {"active", "trialing"}:
        sub_row.is_active = True
    elif status in {"canceled", "unpaid", "incomplete_expired"}:
        sub_row.is_active = False

    if current_period_end:
        sub_row.renewal_date = current_period_end

    try:
        db.commit()
        print("✅ STRIPE SUBSCRIPTION SAVED TO user_subscriptions")
        print("   user_id:", user_id)
        print("   stripe_customer_id:", sub_row.stripe_customer_id)
        print("   stripe_subscription_id:", sub_row.stripe_subscription_id)
        print("   stripe_price_id:", sub_row.stripe_price_id)
        print("   subscription_status:", sub_row.subscription_status)
        print("   cancel_at_period_end:", sub_row.cancel_at_period_end)
        print("   current_period_start:", sub_row.current_period_start)
        print("   current_period_end:", sub_row.current_period_end)
        print("   last_invoice_id:", sub_row.last_invoice_id)
    except Exception:
        db.rollback()
        print("❌ FAILED TO SAVE STRIPE SUBSCRIPTION INTO user_subscriptions")
        raise


def _process_customer_subscription_deleted(db: Session, subscription_obj):
    deleted_subscription_id = str(getattr(subscription_obj, "id", "") or "").strip()
    customer_id = str(getattr(subscription_obj, "customer", "") or "").strip()

    print("🔥 WEBHOOK customer.subscription.deleted")
    print("   deleted_subscription_id:", deleted_subscription_id)
    print("   customer_id:", customer_id)

    if not deleted_subscription_id:
        return {"ok": True, "ignored": True, "reason": "missing_subscription_id"}

    sub_row = None

    if customer_id:
        sub_row = (
            db.query(UserSubscription)
            .filter(UserSubscription.stripe_customer_id == customer_id)
            .first()
        )

    if not sub_row:
        sub_row = (
            db.query(UserSubscription)
            .filter(UserSubscription.stripe_subscription_id == deleted_subscription_id)
            .first()
        )

    if not sub_row:
        print("ℹ️ Deleted subscription ignored. No matching user_subscriptions row.")
        return {"ok": True, "ignored": True, "reason": "no_matching_subscription_row"}

    current_subscription_id = str(sub_row.stripe_subscription_id or "").strip()

    if current_subscription_id and current_subscription_id != deleted_subscription_id:
        print("✅ OLD SUBSCRIPTION DELETE IGNORED")
        print("   deleted_subscription_id:", deleted_subscription_id)
        print("   current_db_subscription_id:", current_subscription_id)
        print("   user_id:", sub_row.user_id)
        print("   reason: deleted subscription is not the current active DB subscription")
        return {
            "ok": True,
            "ignored": True,
            "reason": "deleted_subscription_is_not_current",
            "deleted_subscription_id": deleted_subscription_id,
            "current_db_subscription_id": current_subscription_id,
            "user_id": sub_row.user_id,
        }

    sub_row.subscription_status = "canceled"
    sub_row.cancel_at_period_end = False
    sub_row.is_active = False
    sub_row.stripe_subscription_id = None
    sub_row.stripe_price_id = None
    sub_row.current_period_start = None
    sub_row.current_period_end = None
    sub_row.last_invoice_id = None
    sub_row.renewal_date = None

    try:
        db.commit()
        print("✅ CURRENT SUBSCRIPTION MARKED CANCELED IN DB")
        print("   user_id:", sub_row.user_id)
        print("   deleted_subscription_id:", deleted_subscription_id)
        return {
            "ok": True,
            "updated": True,
            "user_id": sub_row.user_id,
            "deleted_subscription_id": deleted_subscription_id,
        }
    except Exception:
        db.rollback()
        print("❌ FAILED TO MARK SUBSCRIPTION CANCELED IN DB")
        raise


def _process_checkout_session_completed(db: Session, session_obj):
    extracted = _extract_checkout_session_data(session_obj)
    session_id = extracted["session_id"]
    payment_status = extracted["payment_status"]
    payment_intent_id = extracted["payment_intent_id"]
    payment_intent_source = extracted["payment_intent_source"]
    invoice_id = extracted["invoice_id"]
    subscription_id = extracted["subscription_id"]
    session_metadata = extracted["metadata"]
    customer_id = str(getattr(session_obj, "customer", "") or "").strip()

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
        customer_id=customer_id,
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
            print("❌ FINAL METADATA MISSING USER_ID")
            print("   session_metadata:", session_metadata)
            print("   intent_metadata:", {})
            print("   merged_metadata:", metadata)

            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid payment metadata: user_id. "
                    f"session_metadata={session_metadata} "
                    f"invoice_id={invoice_id} "
                    f"subscription_id={subscription_id}"
                ),
            )

        result = _apply_payment_effects(
            db=db,
            payment_intent_id="",
            metadata=metadata,
        )

        one_time_result = _save_one_time_purchase_records(
            db=db,
            session_id=session_id,
            payment_intent_id="",
            customer_id=customer_id,
            payment_status=payment_status,
            metadata=metadata,
        )

        if isinstance(result, dict):
            result["oneTimePurchaseRecords"] = one_time_result

        if str(metadata.get("billing_type") or "").strip().lower() == "one_time":
            try:
                cleanup_result = _cancel_monthly_subscriptions_after_one_time_purchase(
                    user_email=metadata.get("user_email"),
                    customer_id=customer_id,
                )
                if isinstance(result, dict):
                    result["stripe_one_time_cleanup"] = cleanup_result
            except Exception as e:
                print("❌ FAILED ONE-TIME MONTHLY CLEANUP:", e)

        if subscription_id:
            try:
                cleanup_result = _cancel_previous_active_subscriptions_for_same_customer(
                    keep_subscription_id=subscription_id,
                )
                print("✅ WEBHOOK SUBSCRIPTION CLEANUP RESULT:", cleanup_result)

                _save_subscription_state_from_stripe(
                    db=db,
                    user_id=int(metadata["user_id"]),
                    subscription_id=subscription_id,
                )

                if isinstance(result, dict):
                    result["stripe_subscription_cleanup"] = cleanup_result

            except Exception as e:
                print("❌ FAILED AFTER APPLY WHILE CLEANING/SAVING STRIPE SUBSCRIPTION:", e)

        return result

    intent = _retrieve_payment_intent_or_none(payment_intent_id)
    if not intent:
        raise HTTPException(
            status_code=500,
            detail="Failed to process checkout.session.completed: could not retrieve payment intent.",
        )

    intent_metadata = _safe_metadata_dict(getattr(intent, "metadata", None))

    if not customer_id:
        customer_id = str(getattr(intent, "customer", "") or "").strip()

    _log_debug(
        "🔎 WEBHOOK RETRIEVE CHECK",
        session_id=session_id,
        session_metadata_from_event=session_metadata,
        retrieved_intent_id=payment_intent_id,
        retrieved_intent_source=payment_intent_source,
        retrieved_intent_metadata=intent_metadata,
        invoice_id=invoice_id,
        subscription_id=subscription_id,
        customer_id=customer_id,
    )

    metadata = _merge_metadata(session_metadata, intent_metadata)

    if not metadata or not str(metadata.get("user_id") or "").strip():
        client_ref = getattr(session_obj, "client_reference_id", None)
        fallback = _metadata_from_client_reference_id(client_ref)

        print("⚠️ FALLBACK USING client_reference_id:", client_ref)
        print("⚠️ PARSED FALLBACK:", fallback)

        metadata = _merge_metadata(metadata, fallback)

    metadata = _normalize_payment_metadata(db, metadata)

    # ✅ IMPORTANT SAFER FALLBACK:
    # If intent metadata was missing/incomplete but the Checkout Session metadata
    # has the user_id, trust/re-normalize the Checkout Session metadata.
    if not str(metadata.get("user_id") or "").strip().isdigit():
        metadata = _normalize_payment_metadata(db, session_metadata)

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
        print("❌ FINAL METADATA MISSING USER_ID")
        print("   session_metadata:", session_metadata)
        print("   intent_metadata:", intent_metadata)
        print("   merged_metadata:", metadata)

        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid payment metadata: user_id. "
                f"session_metadata={session_metadata} "
                f"intent_metadata={intent_metadata} "
                f"merged_metadata={metadata}"
            ),
        )

    result = _apply_payment_effects(
        db=db,
        payment_intent_id=payment_intent_id,
        metadata=metadata,
    )

    one_time_result = _save_one_time_purchase_records(
        db=db,
        session_id=session_id,
        payment_intent_id=payment_intent_id,
        customer_id=customer_id,
        payment_status=payment_status,
        metadata=metadata,
    )

    if isinstance(result, dict):
        result["oneTimePurchaseRecords"] = one_time_result

    if str(metadata.get("billing_type") or "").strip().lower() == "one_time":
        try:
            cleanup_result = _cancel_monthly_subscriptions_after_one_time_purchase(
                user_email=metadata.get("user_email"),
                customer_id=customer_id,
            )
            if isinstance(result, dict):
                result["stripe_one_time_cleanup"] = cleanup_result
        except Exception as e:
            print("❌ FAILED ONE-TIME MONTHLY CLEANUP:", e)

    if subscription_id:
        try:
            cleanup_result = _cancel_previous_active_subscriptions_for_same_customer(
                keep_subscription_id=subscription_id,
            )
            print("✅ WEBHOOK SUBSCRIPTION CLEANUP RESULT:", cleanup_result)

            _save_subscription_state_from_stripe(
                db=db,
                user_id=int(metadata["user_id"]),
                subscription_id=subscription_id,
            )

            if isinstance(result, dict):
                result["stripe_subscription_cleanup"] = cleanup_result

        except Exception as e:
            print("❌ FAILED AFTER APPLY WHILE CLEANING/SAVING STRIPE SUBSCRIPTION:", e)

    return result


def _process_payment_intent_succeeded(db: Session, intent_obj):
    payment_intent_id = str(getattr(intent_obj, "id", "") or "").strip()
    customer_id = str(getattr(intent_obj, "customer", "") or "").strip()

    metadata = _safe_metadata_dict(getattr(intent_obj, "metadata", None))
    metadata = _normalize_payment_metadata(db, metadata)

    print("🔥 PAYMENT INTENT METADATA FINAL:", metadata)

    _log_debug(
        "🔥 WEBHOOK payment_intent.succeeded",
        payment_intent_id=payment_intent_id,
        customer_id=customer_id,
        metadata=metadata,
    )

    if not payment_intent_id:
        print("ℹ️ payment_intent.succeeded ignored because id is missing")
        return {"ok": True, "ignored": True, "reason": "missing_payment_intent_id"}

    if not metadata:
        print("ℹ️ payment_intent.succeeded ignored because metadata is missing")
        return {"ok": True, "ignored": True, "reason": "missing_metadata"}

    print("🔥 PAYMENT INTENT REQUIRED METADATA CHECK")
    print("   user_id:", metadata.get("user_id"))
    print("   plan_key:", metadata.get("plan_key"))
    print("   billing_type:", metadata.get("billing_type"))

    result = _apply_payment_effects(
        db=db,
        payment_intent_id=payment_intent_id,
        metadata=metadata,
    )

    one_time_result = _save_one_time_purchase_records(
        db=db,
        session_id="",
        payment_intent_id=payment_intent_id,
        customer_id=customer_id,
        payment_status="paid",
        metadata=metadata,
    )

    if isinstance(result, dict):
        result["oneTimePurchaseRecords"] = one_time_result

    if str(metadata.get("billing_type") or "").strip().lower() == "one_time":
        try:
            cleanup_result = _cancel_monthly_subscriptions_after_one_time_purchase(
                user_email=metadata.get("user_email"),
                customer_id=customer_id,
            )
            if isinstance(result, dict):
                result["stripe_one_time_cleanup"] = cleanup_result
        except Exception as e:
            print("❌ FAILED ONE-TIME MONTHLY CLEANUP:", e)

    return result