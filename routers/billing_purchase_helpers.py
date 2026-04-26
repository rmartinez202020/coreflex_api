from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import BillingAddon, BillingPlan, User, UserSubscription
from routers.billing_common import (
    NJ_SALES_TAX_RATE,
    money_to_cents,
    percent_display_2_from_rate,
    quantize_decimal,
    rate_display_2_from_percent,
    to_money_decimal,
)


def _utcnow():
    return datetime.now(timezone.utc)


def _get_or_create_user_subscription(db: Session, user_id: int) -> UserSubscription:
    row = (
        db.query(UserSubscription)
        .filter(UserSubscription.user_id == user_id)
        .first()
    )
    if row:
        if not getattr(row, "active_date", None):
            row.active_date = _utcnow()
            db.commit()
            db.refresh(row)

        return row

    now_utc = _utcnow()

    row = UserSubscription(
        user_id=user_id,
        plan_key="free",
        device_limit=1,
        tenants_users_limit=1,
        active_date=now_utc,
        renewal_date=None,
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
    plan_key = str(plan_key or "").strip().lower()
    billing_type = str(billing_type or "").strip().lower()
    extra_tenant_users = max(0, int(extra_tenant_users or 0))

    subscription = _get_or_create_user_subscription(db, current_user.id)
    current_plan_key = str(subscription.plan_key or "free").strip().lower()
    is_current_plan = current_plan_key == plan_key

    plan, addon = _resolve_plan_and_addon_for_purchase(
        db=db,
        plan_key=plan_key,
        billing_type=billing_type,
        extra_tenant_users=extra_tenant_users,
    )

    plan_price_usd = to_money_decimal(plan.price_usd)

    # ✅ IMPORTANT FIX:
    # Monthly current plan = no plan charge.
    # One-time license = charge the license even if it matches the current monthly plan.
    if billing_type == "monthly" and is_current_plan:
        plan_amount_usd = Decimal("0.00")
    elif billing_type == "one_time":
        plan_amount_usd = plan_price_usd
    else:
        plan_amount_usd = plan_price_usd

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
        "checkout_type": "tenant_user_addon_only" if extra_tenant_users > 0 else "",
        "force_one_time_payment": "true" if extra_tenant_users > 0 else "false",
        "do_not_create_subscription": "true" if extra_tenant_users > 0 else "false",
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
                    Decimal(str(ctx["addon_amount_usd"]))
                    / Decimal(str(ctx["addon_unit_price_usd"]))
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