# routers/billing_admin.py
import os
from decimal import Decimal, ROUND_HALF_UP

import stripe
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth_utils import get_current_user
from database import get_db
from models import User, BillingPlan, BillingAddon

router = APIRouter(prefix="/admin/billing", tags=["Admin Billing"])


# =========================================================
# STRIPE CONFIG
# =========================================================
STRIPE_SECRET_KEY = str(os.getenv("STRIPE_SECRET_KEY") or "").strip()
STRIPE_DEFAULT_CURRENCY = str(os.getenv("STRIPE_DEFAULT_CURRENCY") or "usd").strip().lower()

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# =========================================================
# OWNER / ADMIN GATE
# Use env like:
# COREFLEX_OWNER_EMAILS=rmartinez@coreflexalliance.net,another@email.com
# =========================================================
OWNER_EMAILS = {
    e.strip().lower()
    for e in str(os.getenv("COREFLEX_OWNER_EMAILS") or "").split(",")
    if e.strip()
}


# =========================================================
# REQUEST SCHEMAS
# =========================================================
class SyncOneResponse(BaseModel):
    ok: bool
    item_type: str
    item_id: int
    stripe_product_id: str | None = None
    stripe_price_id: str | None = None
    message: str


# =========================================================
# HELPERS
# =========================================================
def require_owner_user(current_user: User) -> None:
    email = str(getattr(current_user, "email", "") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=403, detail="User email not found.")

    if not OWNER_EMAILS:
        raise HTTPException(
            status_code=500,
            detail="Owner emails are not configured. Set COREFLEX_OWNER_EMAILS in env.",
        )

    if email not in OWNER_EMAILS:
        raise HTTPException(status_code=403, detail="Only owner/admin can use this route.")


def ensure_stripe_ready() -> None:
    if not STRIPE_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="Stripe is not configured. Missing STRIPE_SECRET_KEY.",
        )


def normalize_billing_type(value: str) -> str:
    v = str(value or "").strip().lower()
    if v not in {"monthly", "one_time"}:
        raise HTTPException(status_code=400, detail="billing_type must be monthly or one_time.")
    return v


def normalize_currency(value: str | None) -> str:
    v = str(value or STRIPE_DEFAULT_CURRENCY or "usd").strip().lower()
    return v or "usd"


def amount_to_cents(value) -> int:
    try:
        dec = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid price amount.")

    if dec <= 0:
        raise HTTPException(status_code=400, detail="Price must be greater than zero.")

    return int(dec * 100)


def plan_display_name(plan: BillingPlan) -> str:
    return str(plan.plan_name or plan.plan_key or "Plan").strip()


def addon_display_name(addon: BillingAddon) -> str:
    key = str(addon.addon_key or "addon").strip()
    if key == "tenant_user":
        return "Additional Tenant-User"
    return key.replace("_", " ").title()


def ensure_plan_product(plan: BillingPlan) -> str:
    existing_product_id = str(plan.stripe_product_id or "").strip()
    if existing_product_id:
        return existing_product_id

    product = stripe.Product.create(
        name=plan_display_name(plan),
        metadata={
            "source": "coreflex_billing_plans",
            "plan_id": str(plan.id),
            "plan_key": str(plan.plan_key or ""),
        },
    )
    return str(product.id)


def ensure_addon_product(addon: BillingAddon) -> str:
    existing_product_id = str(getattr(addon, "stripe_product_id", "") or "").strip()
    if existing_product_id:
        return existing_product_id

    product = stripe.Product.create(
        name=addon_display_name(addon),
        metadata={
            "source": "coreflex_billing_addons",
            "addon_id": str(addon.id),
            "addon_key": str(addon.addon_key or ""),
        },
    )
    return str(product.id)


def create_plan_price(plan: BillingPlan, product_id: str) -> str:
    billing_type = normalize_billing_type(plan.billing_type)
    currency = normalize_currency(getattr(plan, "currency", None))
    unit_amount = amount_to_cents(plan.price_usd)

    price_kwargs = {
        "product": product_id,
        "unit_amount": unit_amount,
        "currency": currency,
        "metadata": {
            "source": "coreflex_billing_plans",
            "plan_id": str(plan.id),
            "plan_key": str(plan.plan_key or ""),
            "billing_type": billing_type,
        },
    }

    if billing_type == "monthly":
        price_kwargs["recurring"] = {"interval": "month"}

    price = stripe.Price.create(**price_kwargs)
    return str(price.id)


def create_addon_price(addon: BillingAddon, product_id: str) -> str:
    billing_type = normalize_billing_type(addon.billing_type)
    currency = normalize_currency(getattr(addon, "currency", None))
    unit_amount = amount_to_cents(addon.price_usd)

    price_kwargs = {
        "product": product_id,
        "unit_amount": unit_amount,
        "currency": currency,
        "metadata": {
            "source": "coreflex_billing_addons",
            "addon_id": str(addon.id),
            "addon_key": str(addon.addon_key or ""),
            "billing_type": billing_type,
        },
    }

    if billing_type == "monthly":
        price_kwargs["recurring"] = {"interval": "month"}

    price = stripe.Price.create(**price_kwargs)
    return str(price.id)


# =========================================================
# ROUTES
# =========================================================
@router.get("/plans")
def list_billing_plans(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_owner_user(current_user)

    rows = (
        db.query(BillingPlan)
        .order_by(BillingPlan.sort_order.asc(), BillingPlan.id.asc())
        .all()
    )

    return [
        {
            "id": r.id,
            "plan_key": r.plan_key,
            "plan_name": r.plan_name,
            "billing_type": r.billing_type,
            "price_usd": float(r.price_usd) if r.price_usd is not None else None,
            "currency": getattr(r, "currency", "usd"),
            "device_limit": r.device_limit,
            "tenant_user_limit": r.tenant_user_limit,
            "data_history_days": r.data_history_days,
            "sort_order": r.sort_order,
            "is_active": r.is_active,
            "stripe_product_id": r.stripe_product_id,
            "stripe_price_id": r.stripe_price_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


@router.get("/addons")
def list_billing_addons(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_owner_user(current_user)

    rows = db.query(BillingAddon).order_by(BillingAddon.id.asc()).all()

    return [
        {
            "id": r.id,
            "addon_key": r.addon_key,
            "billing_type": r.billing_type,
            "price_usd": float(r.price_usd) if r.price_usd is not None else None,
            "currency": getattr(r, "currency", "usd"),
            "is_active": r.is_active,
            "stripe_product_id": getattr(r, "stripe_product_id", None),
            "stripe_price_id": r.stripe_price_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


@router.post("/plans/{plan_id}/sync-to-stripe", response_model=SyncOneResponse)
def sync_plan_to_stripe(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_owner_user(current_user)
    ensure_stripe_ready()

    plan = db.query(BillingPlan).filter(BillingPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Billing plan not found.")

    if not plan.is_active:
        raise HTTPException(status_code=400, detail="Billing plan is inactive.")

    try:
        product_id = ensure_plan_product(plan)
        price_id = create_plan_price(plan, product_id)

        plan.stripe_product_id = product_id
        plan.stripe_price_id = price_id
        db.add(plan)
        db.commit()
        db.refresh(plan)

        return SyncOneResponse(
            ok=True,
            item_type="plan",
            item_id=plan.id,
            stripe_product_id=plan.stripe_product_id,
            stripe_price_id=plan.stripe_price_id,
            message="Billing plan synced to Stripe successfully.",
        )
    except stripe.error.StripeError as e:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")


@router.post("/addons/{addon_id}/sync-to-stripe", response_model=SyncOneResponse)
def sync_addon_to_stripe(
    addon_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_owner_user(current_user)
    ensure_stripe_ready()

    addon = db.query(BillingAddon).filter(BillingAddon.id == addon_id).first()
    if not addon:
        raise HTTPException(status_code=404, detail="Billing addon not found.")

    if not addon.is_active:
        raise HTTPException(status_code=400, detail="Billing addon is inactive.")

    try:
        product_id = ensure_addon_product(addon)
        price_id = create_addon_price(addon, product_id)

        # if your table does not yet have stripe_product_id, add that column first
        if hasattr(addon, "stripe_product_id"):
            addon.stripe_product_id = product_id
        addon.stripe_price_id = price_id

        db.add(addon)
        db.commit()
        db.refresh(addon)

        return SyncOneResponse(
            ok=True,
            item_type="addon",
            item_id=addon.id,
            stripe_product_id=product_id,
            stripe_price_id=addon.stripe_price_id,
            message="Billing addon synced to Stripe successfully.",
        )
    except stripe.error.StripeError as e:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")


@router.post("/sync-all")
def sync_all_billing_to_stripe(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_owner_user(current_user)
    ensure_stripe_ready()

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

    synced_plans = []
    synced_addons = []

    try:
        for plan in plans:
            product_id = ensure_plan_product(plan)
            price_id = create_plan_price(plan, product_id)

            plan.stripe_product_id = product_id
            plan.stripe_price_id = price_id
            db.add(plan)

            synced_plans.append(
                {
                    "id": plan.id,
                    "plan_key": plan.plan_key,
                    "billing_type": plan.billing_type,
                    "stripe_product_id": product_id,
                    "stripe_price_id": price_id,
                }
            )

        for addon in addons:
            product_id = ensure_addon_product(addon)
            price_id = create_addon_price(addon, product_id)

            if hasattr(addon, "stripe_product_id"):
                addon.stripe_product_id = product_id
            addon.stripe_price_id = price_id
            db.add(addon)

            synced_addons.append(
                {
                    "id": addon.id,
                    "addon_key": addon.addon_key,
                    "billing_type": addon.billing_type,
                    "stripe_product_id": product_id,
                    "stripe_price_id": price_id,
                }
            )

        db.commit()

        return {
            "ok": True,
            "plans_synced": synced_plans,
            "addons_synced": synced_addons,
            "message": "All active billing plans and addons synced to Stripe.",
        }
    except stripe.error.StripeError as e:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")