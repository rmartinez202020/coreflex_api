# routers/tag_explorer.py

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import (
    TagExplorerTag,
    User,
    ZHC1921Device,
    ZHC1661Device,
    TP4000Device,
)
from auth_utils import get_current_user


router = APIRouter(prefix="/tag-explorer", tags=["Tag Explorer"])


# =========================================================
# Supported device models / tag points
# =========================================================

MODEL_ALIASES = {
    "cf-2000": "zhc1921",
    "cf2000": "zhc1921",
    "zhc1921": "zhc1921",

    "cf-1600": "zhc1661",
    "cf1600": "zhc1661",
    "zhc1661": "zhc1661",

    "tp-4000": "tp4000",
    "tp4000": "tp4000",
}

MODEL_DISPLAY_NAMES = {
    "zhc1921": "CF-2000",
    "zhc1661": "CF-1600",
    "tp4000": "TP-4000",
}

ALLOWED_TAGS = {
    "zhc1921": {
        "DI-1", "DI-2", "DI-3", "DI-4", "DI-5", "DI-6",
        "DO-1", "DO-2", "DO-3", "DO-4",
        "AI-1", "AI-2", "AI-3", "AI-4",
    },
    "zhc1661": {
        "AI-1", "AI-2", "AI-3", "AI-4",
        "AO-1", "AO-2",
    },
    "tp4000": {
        "TE-101", "TE-102", "TE-103", "TE-104",
        "TE-105", "TE-106", "TE-107", "TE-108",
    },
}


# =========================================================
# Request body
# =========================================================

class TagExplorerSaveBody(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    math_formula: str | None = Field(default=None, max_length=500)
    unit: str | None = Field(default=None, max_length=100)
    group_name: str | None = Field(default=None, max_length=160)


# =========================================================
# Helpers
# =========================================================

def _clean_optional(value: str | None) -> str | None:
    """
    Trim editable text fields.
    Empty strings are stored as NULL.
    """
    if value is None:
        return None

    value = value.strip()
    return value if value else None


def _normalize_device_model(device_model: str) -> str:
    value = (device_model or "").strip().lower()

    normalized = MODEL_ALIASES.get(value)
    if not normalized:
        raise HTTPException(
            status_code=400,
            detail="Unsupported device model",
        )

    return normalized


def _normalize_device_id(device_id: str) -> str:
    value = (device_id or "").strip()

    if not value:
        raise HTTPException(status_code=400, detail="device_id is required")

    # Current CoreFlex device IDs are numeric.
    if not value.isdigit():
        raise HTTPException(
            status_code=400,
            detail="device_id must be numeric",
        )

    return value


def _normalize_tag(device_model: str, tag: str) -> str:
    value = (tag or "").strip().upper()

    if not value:
        raise HTTPException(status_code=400, detail="tag is required")

    allowed = ALLOWED_TAGS.get(device_model, set())

    if value not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Tag {value} is not valid for {MODEL_DISPLAY_NAMES[device_model]}",
        )

    return value


def _get_claimed_device(
    db: Session,
    *,
    user_id: int,
    device_model: str,
    device_id: str,
):
    """
    Security boundary for Tag Explorer.

    The frontend is NEVER trusted to decide ownership.
    The requested device must exist in its actual device table and
    claimed_by_user_id must match the authenticated user.
    """

    if device_model == "zhc1921":
        model = ZHC1921Device
    elif device_model == "zhc1661":
        model = ZHC1661Device
    elif device_model == "tp4000":
        model = TP4000Device
    else:
        raise HTTPException(status_code=400, detail="Unsupported device model")

    row = (
        db.query(model)
        .filter(model.device_id == device_id)
        .filter(model.claimed_by_user_id == user_id)
        .first()
    )

    if not row:
        # Use 404 instead of exposing whether another user owns the device.
        raise HTTPException(
            status_code=404,
            detail="Device not found in your claimed devices",
        )

    return row


def _serialize(row: TagExplorerTag) -> dict:
    normalized_model = _normalize_device_model(row.device_model)

    return {
        "id": row.id,
        "device_model": MODEL_DISPLAY_NAMES[normalized_model],
        "device_model_key": normalized_model,
        "device_id": row.device_id,
        "tag": row.tag,
        "description": row.description or "",
        "math_formula": row.math_formula or "",
        "unit": row.unit or "",
        "group_name": row.group_name or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# =========================================================
# USER: load saved Tag Explorer configuration
#
# GET /tag-explorer
#
# Returns ONLY configuration rows belonging to the authenticated
# user AND only while the corresponding device is still claimed
# by that same user.
# =========================================================

@router.get("")
@router.get("/")
def list_my_tag_explorer_tags(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(TagExplorerTag)
        .filter(TagExplorerTag.user_id == current_user.id)
        .order_by(
            TagExplorerTag.device_model.asc(),
            TagExplorerTag.device_id.asc(),
            TagExplorerTag.tag.asc(),
        )
        .all()
    )

    visible_rows = []

    for row in rows:
        try:
            normalized_model = _normalize_device_model(row.device_model)

            _get_claimed_device(
                db,
                user_id=current_user.id,
                device_model=normalized_model,
                device_id=row.device_id,
            )
        except HTTPException:
            # Keep saved metadata in PostgreSQL, but do not expose it while
            # the device is no longer claimed by this user.
            continue

        visible_rows.append(_serialize(row))

    return visible_rows


# =========================================================
# USER: save/update one Tag Explorer row
#
# PUT /tag-explorer/{device_model}/{device_id}/{tag}
#
# Examples:
# PUT /tag-explorer/CF-2000/1921251024070670/AI-1
# PUT /tag-explorer/zhc1921/1921251024070670/AI-1
#
# Upsert behavior:
# - first Save  -> INSERT
# - later Save  -> UPDATE same row
#
# Ownership is verified BEFORE insert/update.
# =========================================================

@router.put("/{device_model}/{device_id}/{tag}")
def save_tag_explorer_tag(
    device_model: str,
    device_id: str,
    tag: str,
    body: TagExplorerSaveBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    normalized_model = _normalize_device_model(device_model)
    normalized_device_id = _normalize_device_id(device_id)
    normalized_tag = _normalize_tag(normalized_model, tag)

    # Critical security check:
    # user may only save metadata for a device claimed by that user.
    _get_claimed_device(
        db,
        user_id=current_user.id,
        device_model=normalized_model,
        device_id=normalized_device_id,
    )

    row = (
        db.query(TagExplorerTag)
        .filter(TagExplorerTag.user_id == current_user.id)
        .filter(TagExplorerTag.device_model == normalized_model)
        .filter(TagExplorerTag.device_id == normalized_device_id)
        .filter(TagExplorerTag.tag == normalized_tag)
        .first()
    )

    created = row is None

    if row is None:
        row = TagExplorerTag(
            user_id=current_user.id,
            device_model=normalized_model,
            device_id=normalized_device_id,
            tag=normalized_tag,
        )

    row.description = _clean_optional(body.description)
    row.math_formula = _clean_optional(body.math_formula)
    row.unit = _clean_optional(body.unit)
    row.group_name = _clean_optional(body.group_name)

    db.add(row)

    try:
        db.commit()
        db.refresh(row)
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Could not save Tag Explorer configuration",
        )

    return {
        "ok": True,
        "created": created,
        "tag_config": _serialize(row),
    }
