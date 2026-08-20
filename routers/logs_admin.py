# routers/logs_admin.py

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from jwt_handler import SECRET_KEY, ALGORITHM
from models import User
from routers.log_engine import read_logs


router = APIRouter(
    prefix="/admin/logs",
    tags=["Admin Logs"],
)


# ============================================================
# OWNER / ADMIN GATE
#
# This route verifies the platform-owner JWT directly.
# It does NOT use get_current_user().
# ============================================================

OWNER_EMAILS = {
    email.strip().lower()
    for email in str(os.getenv("COREFLEX_OWNER_EMAILS") or "").split(",")
    if email.strip()
}


def normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def _credentials_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_platform_owner(request: Request) -> str:
    """
    Verify that the caller is a configured CoreFlex platform owner.

    Requirements:
    - Authorization: Bearer <JWT>
    - JWT signature/expiration valid with SECRET_KEY + ALGORITHM
    - JWT "sub" email exists in COREFLEX_OWNER_EMAILS

    Returns:
        normalized owner email
    """

    if not OWNER_EMAILS:
        raise HTTPException(
            status_code=500,
            detail=(
                "Owner emails are not configured. "
                "Set COREFLEX_OWNER_EMAILS in env."
            ),
        )

    auth = str(request.headers.get("Authorization") or "").strip()

    if not auth:
        raise _credentials_exception()

    parts = auth.split()

    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise _credentials_exception()

    token = parts[1].strip()

    if not token or token.lower() in {"null", "undefined"}:
        raise _credentials_exception()

    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
        )
    except JWTError:
        raise _credentials_exception()

    owner_email = normalize_email(payload.get("sub"))

    if not owner_email:
        raise _credentials_exception()

    if owner_email not in OWNER_EMAILS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the platform owner can use Logs Administration.",
        )

    return owner_email


# ============================================================
# REQUEST SCHEMA
# ============================================================

class AdminLogsReadRequest(BaseModel):
    # Admin searches by EMAIL only.
    # The frontend never sends another user's database ID.
    email: str

    # Optional future support for one date.
    date: str | None = None


# ============================================================
# HELPERS
# ============================================================

def find_user_by_email(
    db: Session,
    email: str,
) -> User | None:
    """
    Resolve the requested CoreFlex account internally by email.
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
    owner_email: str = Depends(require_platform_owner),
):
    """
    Read another CoreFlex owner's Logs & Activity history by EMAIL.

    SECURITY DESIGN
    ---------------
    1. Caller presents a valid CoreFlex JWT.
    2. JWT "sub" must be listed in COREFLEX_OWNER_EMAILS.
    3. Browser sends only the TARGET user's email.
    4. Backend finds the target User row.
    5. Backend gets target_user.id internally.
    6. Only that trusted ID is passed to read_logs().
    7. Node-RED reads the existing user_<ID> log namespace.

    No browser-provided user_id is accepted.
    """

    # --------------------------------------------------------
    # VALIDATE TARGET EMAIL
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
        status_code_value = result.get("status_code")

        if result.get("error") == "Invalid log date. Expected YYYY-MM-DD":
            raise HTTPException(
                status_code=400,
                detail=result,
            )

        raise HTTPException(
            status_code=(
                502
                if not status_code_value
                else int(status_code_value)
            ),
            detail=result,
        )

    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

    return {
        **result,
        "user_email": normalize_email(
            getattr(target_user, "email", target_email)
        ),
        "requested_by": owner_email,
    }