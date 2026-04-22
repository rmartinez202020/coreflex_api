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