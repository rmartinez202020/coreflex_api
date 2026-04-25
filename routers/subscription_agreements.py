from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from auth_utils import get_current_user
from database import get_db
from models import User, SubscriptionAgreementAcceptance

router = APIRouter(
    prefix="/subscription-agreements",
    tags=["Subscription Agreements"],
)


def _serialize_agreement(row: SubscriptionAgreementAcceptance):
    return {
        "id": row.id,
        "user_id": row.user_id,
        "plan_key": row.plan_key,
        "billing_type": row.billing_type,
        "agreement_version": row.agreement_version,
        "confirmed": row.confirmed,
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
        "checkout_session_id": getattr(row, "checkout_session_id", None),
        "payment_intent_id": getattr(row, "payment_intent_id", None),
        "ip_address": row.ip_address,
        "user_agent": row.user_agent,
    }


@router.get("/me")
def get_my_subscription_agreements(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(SubscriptionAgreementAcceptance)
        .filter(SubscriptionAgreementAcceptance.user_id == current_user.id)
        .order_by(SubscriptionAgreementAcceptance.confirmed_at.desc())
        .all()
    )

    return {
        "ok": True,
        "items": [_serialize_agreement(row) for row in rows],
    }


@router.get("/accepted")
def get_my_accepted_subscription_agreements(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(SubscriptionAgreementAcceptance)
        .filter(
            SubscriptionAgreementAcceptance.user_id == current_user.id,
            SubscriptionAgreementAcceptance.confirmed.is_(True),
        )
        .order_by(SubscriptionAgreementAcceptance.confirmed_at.desc())
        .all()
    )

    return {
        "ok": True,
        "items": [_serialize_agreement(row) for row in rows],
    }


@router.post("/confirm")
def confirm_subscription_agreement(
    payload: dict,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    plan_key = str(payload.get("planKey") or "").strip().lower()
    billing_type = str(payload.get("billingType") or "").strip().lower()
    agreement_version = str(payload.get("agreementVersion") or "v1").strip()
    confirmed = bool(payload.get("confirmed", True))

    if not plan_key:
        raise HTTPException(status_code=400, detail="Missing planKey.")

    if billing_type not in {"monthly", "one_time"}:
        raise HTTPException(status_code=400, detail="Invalid billingType.")

    if not confirmed:
        raise HTTPException(status_code=400, detail="Agreement must be confirmed.")

    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    row = SubscriptionAgreementAcceptance(
        user_id=current_user.id,
        plan_key=plan_key,
        billing_type=billing_type,
        agreement_version=agreement_version,
        confirmed=True,
        confirmed_at=datetime.now(timezone.utc),
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.add(row)
    db.commit()
    db.refresh(row)

    print("✅ SUBSCRIPTION AGREEMENT SAVED")
    print("   id:", row.id)
    print("   user_id:", row.user_id)
    print("   plan_key:", row.plan_key)
    print("   billing_type:", row.billing_type)
    print("   confirmed_at:", row.confirmed_at)

    return {
        "ok": True,
        "id": row.id,
        "message": "Agreement saved successfully.",
    }