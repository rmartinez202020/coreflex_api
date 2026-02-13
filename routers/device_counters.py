# routers/device_counters.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Optional
import uuid

from database import get_db
from auth_utils import get_current_user
from models import User, ZHC1921Device, ZHC1661Device, TP4000Device

router = APIRouter(prefix="/device-counters", tags=["Device Counters"])


# ----------------------------
# ✅ Pydantic Schemas
# ----------------------------
class UpsertCounterBody(BaseModel):
    widget_id: str = Field(..., min_length=1)
    device_id: str = Field(..., min_length=1)
    field: str = Field(..., min_length=1)  # di1..di6 OR in1..in6 (legacy)

    dashboard_id: Optional[str] = None
    enabled: bool = True


class ResetCounterBody(BaseModel):
    widget_id: str = Field(..., min_length=1)
    dashboard_id: Optional[str] = None


# ----------------------------
# ✅ Helpers
# ----------------------------
def _row_to_dict(r):
    return {
        "id": str(r["id"]),
        "user_id": r["user_id"],
        "dashboard_id": r["dashboard_id"],
        "widget_id": r["widget_id"],
        "device_id": r["device_id"],
        "field": r["field"],
        "count": r["count"],
        "prev01": r["prev01"],
        "enabled": r["enabled"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


def _to01(v) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if v > 0 else 0
    s = str(v).strip().lower()
    if s in {"1", "true", "on", "yes"}:
        return 1
    if s in {"0", "false", "off", "no"}:
        return 0
    try:
        n = float(s)
        return 1 if n > 0 else 0
    except Exception:
        return 1 if s else 0


def _normalize_field(field: str) -> str:
    """
    ✅ Normalize to DI fields so backend + tick are consistent.
    Accepts: di1..di6 OR in1..in6 (legacy)
    Returns: di1..di6 or "" if invalid
    """
    f = (field or "").strip().lower()
    if not f:
        return ""

    # legacy in1..in6 -> di1..di6
    if f.startswith("in") and len(f) == 3 and f[2].isdigit():
        f = "di" + f[2]

    if f in {"di1", "di2", "di3", "di4", "di5", "di6"}:
        return f

    # If later you want to support other fields, expand here
    return ""


def _get_current_field_value(db: Session, user: User, device_id: str, field: str) -> Optional[int]:
    """
    Reads the CURRENT value of the counter's selected field from the user's claimed devices.
    Returns 0/1 or None if not found.
    """
    device_id = (device_id or "").strip()
    field = _normalize_field(field)

    if not device_id or not field:
        return None

    # ZHC1921 (CF-2000)
    r1921 = (
        db.query(ZHC1921Device)
        .filter(ZHC1921Device.device_id == device_id)
        .filter(ZHC1921Device.claimed_by_user_id == user.id)
        .first()
    )
    if r1921:
        val = getattr(r1921, field, None)  # di1..di6
        return _to01(val)

    # ZHC1661 (CF-1600) - not DI-based typically, but safe
    r1661 = (
        db.query(ZHC1661Device)
        .filter(ZHC1661Device.device_id == device_id)
        .filter(ZHC1661Device.claimed_by_user_id == user.id)
        .first()
    )
    if r1661:
        val = getattr(r1661, field, None)
        return _to01(val)

    # TP4000 - not DI-based, but safe
    rtp = (
        db.query(TP4000Device)
        .filter(TP4000Device.device_id == device_id)
        .filter(TP4000Device.claimed_by_user_id == user.id)
        .first()
    )
    if rtp:
        val = getattr(rtp, field, None)
        return _to01(val)

    return None


# ----------------------------
# ✅ ROUTES
# ----------------------------

@router.get("/")
def list_counters(
    dashboard_id: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if dashboard_id:
        q = """
        SELECT *
        FROM public.device_counters
        WHERE user_id = :user_id AND dashboard_id = :dashboard_id
        ORDER BY created_at ASC
        """
        rows = db.execute(q, {"user_id": user.id, "dashboard_id": dashboard_id}).mappings().all()
    else:
        q = """
        SELECT *
        FROM public.device_counters
        WHERE user_id = :user_id
        ORDER BY created_at ASC
        """
        rows = db.execute(q, {"user_id": user.id}).mappings().all()

    return [_row_to_dict(r) for r in rows]


@router.get("/by-dashboard/{dashboard_id}")
def list_counters_by_dashboard(
    dashboard_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dashboard_id = (dashboard_id or "").strip()
    if not dashboard_id:
        raise HTTPException(status_code=400, detail="dashboard_id is required")

    q = """
    SELECT *
    FROM public.device_counters
    WHERE user_id = :user_id AND dashboard_id = :dashboard_id
    ORDER BY created_at ASC
    """
    rows = db.execute(q, {"user_id": user.id, "dashboard_id": dashboard_id}).mappings().all()
    return [_row_to_dict(r) for r in rows]


@router.get("/by-widget/{widget_id}")
def get_counter_by_widget(
    widget_id: str,
    dashboard_id: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    widget_id = (widget_id or "").strip()
    if not widget_id:
        raise HTTPException(status_code=400, detail="widget_id is required")

    if dashboard_id:
        q = """
        SELECT *
        FROM public.device_counters
        WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id = :dashboard_id
        LIMIT 1
        """
        row = db.execute(
            q,
            {"user_id": user.id, "widget_id": widget_id, "dashboard_id": dashboard_id},
        ).mappings().first()
    else:
        q = """
        SELECT *
        FROM public.device_counters
        WHERE user_id = :user_id AND widget_id = :widget_id
        LIMIT 1
        """
        row = db.execute(q, {"user_id": user.id, "widget_id": widget_id}).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Counter not found")

    return _row_to_dict(row)


@router.post("/upsert")
def upsert_counter(
    body: UpsertCounterBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    widget_id = body.widget_id.strip()
    device_id = body.device_id.strip()
    field_raw = (body.field or "").strip()
    dashboard_id = body.dashboard_id.strip() if body.dashboard_id else None

    if not widget_id or not device_id or not field_raw:
        raise HTTPException(status_code=400, detail="widget_id, device_id, field are required")

    field_norm = _normalize_field(field_raw)
    if not field_norm:
        raise HTTPException(status_code=400, detail="field must be di1..di6 (or legacy in1..in6)")

    # check if exists
    if dashboard_id:
        find_q = """
        SELECT id
        FROM public.device_counters
        WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id = :dashboard_id
        LIMIT 1
        """
        found = db.execute(
            find_q,
            {"user_id": user.id, "widget_id": widget_id, "dashboard_id": dashboard_id},
        ).mappings().first()
    else:
        find_q = """
        SELECT id
        FROM public.device_counters
        WHERE user_id = :user_id AND widget_id = :widget_id
        LIMIT 1
        """
        found = db.execute(find_q, {"user_id": user.id, "widget_id": widget_id}).mappings().first()

    if found:
        upd_q = """
        UPDATE public.device_counters
        SET device_id = :device_id,
            field = :field,
            enabled = :enabled,
            dashboard_id = COALESCE(:dashboard_id, dashboard_id),
            updated_at = NOW()
        WHERE id = :id AND user_id = :user_id
        RETURNING *
        """
        row = db.execute(
            upd_q,
            {
                "id": found["id"],
                "user_id": user.id,
                "device_id": device_id,
                "field": field_norm,
                "enabled": body.enabled,
                "dashboard_id": dashboard_id,
            },
        ).mappings().first()
        db.commit()
        return _row_to_dict(row)

    # create new row
    new_id = str(uuid.uuid4())

    # ✅ init prev01 to current state (prevents first-tick phantom edge)
    cur01 = _get_current_field_value(db, user, device_id, field_norm)
    if cur01 is None:
        cur01 = 0

    ins_q = """
    INSERT INTO public.device_counters
      (id, user_id, dashboard_id, widget_id, device_id, field, count, prev01, enabled, created_at, updated_at)
    VALUES
      (:id, :user_id, :dashboard_id, :widget_id, :device_id, :field, 0, :prev01, :enabled, NOW(), NOW())
    RETURNING *
    """
    row = db.execute(
        ins_q,
        {
            "id": new_id,
            "user_id": user.id,
            "dashboard_id": dashboard_id,
            "widget_id": widget_id,
            "device_id": device_id,
            "field": field_norm,
            "prev01": int(cur01),
            "enabled": body.enabled,
        },
    ).mappings().first()
    db.commit()
    return _row_to_dict(row)


@router.post("/reset")
def reset_counter(
    body: ResetCounterBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    widget_id = body.widget_id.strip()
    dashboard_id = body.dashboard_id.strip() if body.dashboard_id else None
    if not widget_id:
        raise HTTPException(status_code=400, detail="widget_id is required")

    # fetch row first to know device_id + field
    if dashboard_id:
        get_q = """
        SELECT *
        FROM public.device_counters
        WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id = :dashboard_id
        LIMIT 1
        """
        row = db.execute(
            get_q,
            {"user_id": user.id, "widget_id": widget_id, "dashboard_id": dashboard_id},
        ).mappings().first()
    else:
        get_q = """
        SELECT *
        FROM public.device_counters
        WHERE user_id = :user_id AND widget_id = :widget_id
        LIMIT 1
        """
        row = db.execute(get_q, {"user_id": user.id, "widget_id": widget_id}).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Counter not found")

    device_id = row["device_id"]
    field = row["field"]  # already normalized in DB

    cur01 = _get_current_field_value(db, user, device_id, field)
    if cur01 is None:
        cur01 = 0

    # update
    if dashboard_id:
        q = """
        UPDATE public.device_counters
        SET count = 0,
            prev01 = :prev01,
            updated_at = NOW()
        WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id = :dashboard_id
        RETURNING *
        """
        updated = db.execute(
            q,
            {
                "user_id": user.id,
                "widget_id": widget_id,
                "dashboard_id": dashboard_id,
                "prev01": int(cur01),
            },
        ).mappings().first()
    else:
        q = """
        UPDATE public.device_counters
        SET count = 0,
            prev01 = :prev01,
            updated_at = NOW()
        WHERE user_id = :user_id AND widget_id = :widget_id
        RETURNING *
        """
        updated = db.execute(
            q,
            {"user_id": user.id, "widget_id": widget_id, "prev01": int(cur01)},
        ).mappings().first()

    db.commit()
    return _row_to_dict(updated)


@router.delete("/")
def delete_counter(
    widget_id: str,
    dashboard_id: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    widget_id = (widget_id or "").strip()
    dashboard_id = dashboard_id.strip() if dashboard_id else None
    if not widget_id:
        raise HTTPException(status_code=400, detail="widget_id is required")

    if dashboard_id:
        q = """
        DELETE FROM public.device_counters
        WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id = :dashboard_id
        RETURNING id
        """
        row = db.execute(
            q,
            {"user_id": user.id, "widget_id": widget_id, "dashboard_id": dashboard_id},
        ).mappings().first()
    else:
        q = """
        DELETE FROM public.device_counters
        WHERE user_id = :user_id AND widget_id = :widget_id
        RETURNING id
        """
        row = db.execute(q, {"user_id": user.id, "widget_id": widget_id}).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Counter not found")

    db.commit()
    return {"ok": True, "deleted_id": str(row["id"])}
