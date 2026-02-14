# routers/device_counters.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import text as sa_text
from typing import Optional
import uuid

from database import get_db
from auth_utils import get_current_user
from models import User, ZHC1921Device, ZHC1661Device, TP4000Device

router = APIRouter(prefix="/device-counters", tags=["Device Counters"])


# ----------------------------
# ✅ Pydantic Schemas
# ----------------------------
class CreatePlaceholderBody(BaseModel):
    widget_id: str = Field(..., min_length=1)
    dashboard_id: Optional[str] = None


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
        "dashboard_id": r["dashboard_id"],  # TEXT or None
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

    return ""


def _normalize_dashboard_id(dashboard_id: Optional[str]) -> Optional[str]:
    """
    ✅ device_counters.dashboard_id is TEXT (per your pgAdmin screenshot)
    - Treat "main" (and blanks) as NULL (main dashboard)
    - Otherwise store as trimmed string
    """
    s = (dashboard_id or "").strip()
    if not s:
        return None
    if s.lower() == "main":
        return None
    return s


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

    # ZHC1661 (CF-1600) - safe
    r1661 = (
        db.query(ZHC1661Device)
        .filter(ZHC1661Device.device_id == device_id)
        .filter(ZHC1661Device.claimed_by_user_id == user.id)
        .first()
    )
    if r1661:
        val = getattr(r1661, field, None)
        return _to01(val)

    # TP4000 - safe
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

@router.post("/create-placeholder")
def create_placeholder_counter(
    body: CreatePlaceholderBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    ✅ Called immediately on widget drop.
    Creates a row in device_counters even before device/tag config.
    Safe defaults:
      - enabled = false
      - device_id = ""
      - field = "di1"
    """
    widget_id = (body.widget_id or "").strip()
    if not widget_id:
        raise HTTPException(status_code=400, detail="widget_id is required")

    dash = _normalize_dashboard_id(body.dashboard_id)

    # if already exists, return it (idempotent)
    if body.dashboard_id is not None:
        if dash is None:
            find_q = sa_text("""
                SELECT *
                FROM public.device_counters
                WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id IS NULL
                LIMIT 1
            """)
            found = db.execute(find_q, {"user_id": user.id, "widget_id": widget_id}).mappings().first()
        else:
            find_q = sa_text("""
                SELECT *
                FROM public.device_counters
                WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id = :dashboard_id
                LIMIT 1
            """)
            found = db.execute(
                find_q,
                {"user_id": user.id, "widget_id": widget_id, "dashboard_id": dash},
            ).mappings().first()
    else:
        find_q = sa_text("""
            SELECT *
            FROM public.device_counters
            WHERE user_id = :user_id AND widget_id = :widget_id
            LIMIT 1
        """)
        found = db.execute(find_q, {"user_id": user.id, "widget_id": widget_id}).mappings().first()

    if found:
        return _row_to_dict(found)

    new_id = str(uuid.uuid4())

    ins_q = sa_text("""
        INSERT INTO public.device_counters
          (id, user_id, dashboard_id, widget_id, device_id, field, count, prev01, enabled, created_at, updated_at)
        VALUES
          (:id, :user_id, :dashboard_id, :widget_id, :device_id, :field, 0, 0, :enabled, NOW(), NOW())
        RETURNING *
    """)

    row = db.execute(
        ins_q,
        {
            "id": new_id,
            "user_id": user.id,
            "dashboard_id": dash,      # TEXT or None
            "widget_id": widget_id,
            "device_id": "",           # placeholder
            "field": "di1",            # placeholder
            "enabled": False,          # placeholder disabled
        },
    ).mappings().first()
    db.commit()
    return _row_to_dict(row)


@router.get("/")
def list_counters(
    dashboard_id: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # If param provided (even "main"), filter. Else list all user counters.
    if dashboard_id is not None:
        dash = _normalize_dashboard_id(dashboard_id)

        if dash is None:
            q = sa_text("""
                SELECT *
                FROM public.device_counters
                WHERE user_id = :user_id AND dashboard_id IS NULL
                ORDER BY created_at ASC
            """)
            rows = db.execute(q, {"user_id": user.id}).mappings().all()
        else:
            q = sa_text("""
                SELECT *
                FROM public.device_counters
                WHERE user_id = :user_id AND dashboard_id = :dashboard_id
                ORDER BY created_at ASC
            """)
            rows = db.execute(q, {"user_id": user.id, "dashboard_id": dash}).mappings().all()
    else:
        q = sa_text("""
            SELECT *
            FROM public.device_counters
            WHERE user_id = :user_id
            ORDER BY created_at ASC
        """)
        rows = db.execute(q, {"user_id": user.id}).mappings().all()

    return [_row_to_dict(r) for r in rows]


@router.get("/by-dashboard/{dashboard_id}")
def list_counters_by_dashboard(
    dashboard_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = (dashboard_id or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail="dashboard_id is required")

    dash = _normalize_dashboard_id(s)

    if dash is None:
        q = sa_text("""
            SELECT *
            FROM public.device_counters
            WHERE user_id = :user_id AND dashboard_id IS NULL
            ORDER BY created_at ASC
        """)
        rows = db.execute(q, {"user_id": user.id}).mappings().all()
    else:
        q = sa_text("""
            SELECT *
            FROM public.device_counters
            WHERE user_id = :user_id AND dashboard_id = :dashboard_id
            ORDER BY created_at ASC
        """)
        rows = db.execute(q, {"user_id": user.id, "dashboard_id": dash}).mappings().all()

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

    if dashboard_id is not None:
        dash = _normalize_dashboard_id(dashboard_id)

        if dash is None:
            q = sa_text("""
                SELECT *
                FROM public.device_counters
                WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id IS NULL
                LIMIT 1
            """)
            row = db.execute(q, {"user_id": user.id, "widget_id": widget_id}).mappings().first()
        else:
            q = sa_text("""
                SELECT *
                FROM public.device_counters
                WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id = :dashboard_id
                LIMIT 1
            """)
            row = db.execute(
                q,
                {"user_id": user.id, "widget_id": widget_id, "dashboard_id": dash},
            ).mappings().first()
    else:
        q = sa_text("""
            SELECT *
            FROM public.device_counters
            WHERE user_id = :user_id AND widget_id = :widget_id
            LIMIT 1
        """)
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

    if not widget_id or not device_id or not field_raw:
        raise HTTPException(status_code=400, detail="widget_id, device_id, field are required")

    field_norm = _normalize_field(field_raw)
    if not field_norm:
        raise HTTPException(status_code=400, detail="field must be di1..di6 (or legacy in1..in6)")

    dash = _normalize_dashboard_id(body.dashboard_id) if body.dashboard_id is not None else None

    # check if exists
    if body.dashboard_id is not None:
        if dash is None:
            find_q = sa_text("""
                SELECT id
                FROM public.device_counters
                WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id IS NULL
                LIMIT 1
            """)
            found = db.execute(find_q, {"user_id": user.id, "widget_id": widget_id}).mappings().first()
        else:
            find_q = sa_text("""
                SELECT id
                FROM public.device_counters
                WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id = :dashboard_id
                LIMIT 1
            """)
            found = db.execute(
                find_q,
                {"user_id": user.id, "widget_id": widget_id, "dashboard_id": dash},
            ).mappings().first()
    else:
        find_q = sa_text("""
            SELECT id
            FROM public.device_counters
            WHERE user_id = :user_id AND widget_id = :widget_id
            LIMIT 1
        """)
        found = db.execute(find_q, {"user_id": user.id, "widget_id": widget_id}).mappings().first()

    if found:
        upd_q = sa_text("""
            UPDATE public.device_counters
            SET device_id = :device_id,
                field = :field,
                enabled = :enabled,
                updated_at = NOW()
            WHERE id = :id AND user_id = :user_id
            RETURNING *
        """)
        row = db.execute(
            upd_q,
            {
                "id": found["id"],
                "user_id": user.id,
                "device_id": device_id,
                "field": field_norm,
                "enabled": body.enabled,
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

    ins_q = sa_text("""
        INSERT INTO public.device_counters
          (id, user_id, dashboard_id, widget_id, device_id, field, count, prev01, enabled, created_at, updated_at)
        VALUES
          (:id, :user_id, :dashboard_id, :widget_id, :device_id, :field, 0, :prev01, :enabled, NOW(), NOW())
        RETURNING *
    """)
    row = db.execute(
        ins_q,
        {
            "id": new_id,
            "user_id": user.id,
            "dashboard_id": dash,  # TEXT or None
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
    if not widget_id:
        raise HTTPException(status_code=400, detail="widget_id is required")

    dash = _normalize_dashboard_id(body.dashboard_id) if body.dashboard_id is not None else None

    # fetch row first to know device_id + field
    if body.dashboard_id is not None:
        if dash is None:
            get_q = sa_text("""
                SELECT *
                FROM public.device_counters
                WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id IS NULL
                LIMIT 1
            """)
            row = db.execute(get_q, {"user_id": user.id, "widget_id": widget_id}).mappings().first()
        else:
            get_q = sa_text("""
                SELECT *
                FROM public.device_counters
                WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id = :dashboard_id
                LIMIT 1
            """)
            row = db.execute(
                get_q,
                {"user_id": user.id, "widget_id": widget_id, "dashboard_id": dash},
            ).mappings().first()
    else:
        get_q = sa_text("""
            SELECT *
            FROM public.device_counters
            WHERE user_id = :user_id AND widget_id = :widget_id
            LIMIT 1
        """)
        row = db.execute(get_q, {"user_id": user.id, "widget_id": widget_id}).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Counter not found")

    device_id = row["device_id"]
    field = row["field"]

    cur01 = _get_current_field_value(db, user, device_id, field)
    if cur01 is None:
        cur01 = 0

    # update
    if body.dashboard_id is not None:
        if dash is None:
            q = sa_text("""
                UPDATE public.device_counters
                SET count = 0,
                    prev01 = :prev01,
                    updated_at = NOW()
                WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id IS NULL
                RETURNING *
            """)
            updated = db.execute(
                q,
                {"user_id": user.id, "widget_id": widget_id, "prev01": int(cur01)},
            ).mappings().first()
        else:
            q = sa_text("""
                UPDATE public.device_counters
                SET count = 0,
                    prev01 = :prev01,
                    updated_at = NOW()
                WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id = :dashboard_id
                RETURNING *
            """)
            updated = db.execute(
                q,
                {
                    "user_id": user.id,
                    "widget_id": widget_id,
                    "dashboard_id": dash,
                    "prev01": int(cur01),
                },
            ).mappings().first()
    else:
        q = sa_text("""
            UPDATE public.device_counters
            SET count = 0,
                prev01 = :prev01,
                updated_at = NOW()
            WHERE user_id = :user_id AND widget_id = :widget_id
            RETURNING *
        """)
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
    if not widget_id:
        raise HTTPException(status_code=400, detail="widget_id is required")

    dash = _normalize_dashboard_id(dashboard_id) if dashboard_id is not None else None

    if dashboard_id is not None:
        if dash is None:
            q = sa_text("""
                DELETE FROM public.device_counters
                WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id IS NULL
                RETURNING id
            """)
            row = db.execute(q, {"user_id": user.id, "widget_id": widget_id}).mappings().first()
        else:
            q = sa_text("""
                DELETE FROM public.device_counters
                WHERE user_id = :user_id AND widget_id = :widget_id AND dashboard_id = :dashboard_id
                RETURNING id
            """)
            row = db.execute(
                q,
                {"user_id": user.id, "widget_id": widget_id, "dashboard_id": dash},
            ).mappings().first()
    else:
        q = sa_text("""
            DELETE FROM public.device_counters
            WHERE user_id = :user_id AND widget_id = :widget_id
            RETURNING id
        """)
        row = db.execute(q, {"user_id": user.id, "widget_id": widget_id}).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Counter not found")

    db.commit()
    return {"ok": True, "deleted_id": str(row["id"])}
