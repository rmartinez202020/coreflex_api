# routers/logs_admin.py

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth_utils import get_current_user
from database import get_db
from models import User
from routers.log_engine import read_logs


router = APIRouter(
    prefix="/admin/logs",
    tags=["Admin Logs"],
)


# ============================================================
# OWNER / ADMIN GATE
# Same security model used by billing_admin.py
#
# Render environment example:
# COREFLEX_OWNER_EMAILS=owner1@example.com,owner2@example.com
# ============================================================

OWNER_EMAILS = {
    email.strip().lower()
    for email in str(os.getenv("COREFLEX_OWNER_EMAILS") or "").split(",")
    if email.strip()
}


def require_owner_user(current_user: User) -> None:
    """
    Allow only configured CoreFlex platform owners/admins.

    The authenticated user's email comes from the verified JWT through
    get_current_user(). The browser cannot choose which account is treated
    as the owner/admin.
    """
    email = str(
        getattr(current_user, "email", "") or ""
    ).strip().lower()

    if not email:
        raise HTTPException(
            status_code=403,
            detail="User email not found.",
        )

    if not OWNER_EMAILS:
        raise HTTPException(
            status_code=500,
            detail=(
                "Owner emails are not configured. "
                "Set COREFLEX_OWNER_EMAILS in env."
            ),
        )

    if email not in OWNER_EMAILS:
        raise HTTPException(
            status_code=403,
            detail="Only owner/admin can use this route.",
        )


# ============================================================
# REQUEST SCHEMA
# ============================================================

class AdminLogsReadRequest(BaseModel):
    # Admin searches by EMAIL only.
    # The frontend never sends another user's database ID.
    email: str

    # Optional future support for one date.
    # Leaving it null/omitted uses the existing Log Engine behavior.
    date: str | None = None


# ============================================================
# HELPERS
# ============================================================

def normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def find_user_by_email(
    db: Session,
    email: str,
) -> User | None:
    """
    Resolve the requested CoreFlex account internally by email.

    PostgreSQL/SQLAlchemy ILIKE keeps the lookup case-insensitive.
    """
    clean_email = normalize_email(email)

    if not clean_email:
        return None

    return (
        db.query(User)
        .filter(User.email.ilike(clean_email))
        .first()
    )


# ============================================================
# ADMIN LOG READ
# POST /admin/logs/read
# ============================================================

@router.post("/read")
def read_user_logs_as_admin(
    body: AdminLogsReadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Read another CoreFlex owner's Logs & Activity history by EMAIL.

    SECURITY DESIGN
    ---------------
    1. The caller must be authenticated.
    2. The authenticated caller must be listed in COREFLEX_OWNER_EMAILS.
    3. The browser sends only the target user's email.
    4. The backend looks up the target User row.
    5. The backend obtains target_user.id internally.
    6. Only that trusted database ID is passed to read_logs().
    7. Node-RED continues reading the existing user_<ID> log namespace.

    No browser-provided user_id is accepted.
    """

    # --------------------------------------------------------
    # OWNER / ADMIN AUTHORIZATION
    # --------------------------------------------------------
    require_owner_user(current_user)

    # --------------------------------------------------------
    # VALIDATE EMAIL
    # --------------------------------------------------------
    target_email = normalize_email(body.email)

    if not target_email:
        raise HTTPException(
            status_code=400,
            detail="User email is required.",
        )

    # --------------------------------------------------------
    # FIND TARGET USER
    # --------------------------------------------------------
    target_user = find_user_by_email(
        db=db,
        email=target_email,
    )

    if not target_user:
        raise HTTPException(
            status_code=404,
            detail="User not found.",
        )

    # --------------------------------------------------------
    # READ LOGS USING TRUSTED INTERNAL USER ID
    # --------------------------------------------------------
    result = read_logs(
        user_id=target_user.id,
        date=body.date,
    )

    # --------------------------------------------------------
    # HANDLE LOG ENGINE / NODE-RED ERRORS
    # --------------------------------------------------------
    if not result.get("ok"):
        status_code = result.get("status_code")

        if result.get("error") == "Invalid log date. Expected YYYY-MM-DD":
            raise HTTPException(
                status_code=400,
                detail=result,
            )

        raise HTTPException(
            status_code=502 if not status_code else int(status_code),
            detail=result,
        )

    # --------------------------------------------------------
    # RESPONSE
    #
    # Return the email so the frontend can show which account
    # was loaded. We intentionally do not need to expose the ID.
    # --------------------------------------------------------
    return {
        **result,
        "user_email": normalize_email(
            getattr(target_user, "email", target_email)
        ),
    }