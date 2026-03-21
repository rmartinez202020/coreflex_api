# auth_routes.py

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from database import get_db
from models import User, PasswordResetCode
from auth_utils import (
    hash_password,
    verify_password,
    normalize_email,
    generate_reset_code,
    hash_reset_code,
    verify_reset_code,
)
from jwt_handler import create_access_token
from utils.email_service import send_reset_code_email

router = APIRouter(prefix="/auth", tags=["auth"])


# -------------------------------
# REQUEST MODELS
# -------------------------------
class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    company: str | None = None

    # ✅ NEW: control terms acceptance (must be true)
    accepted_control_terms: bool
    control_terms_version: str


class LoginRequest(BaseModel):
    email: str
    password: str


# ✅ NEW: forgot-password step 1
class ForgotPasswordRequest(BaseModel):
    email: EmailStr


# ✅ NEW: forgot-password step 2
class ResetPasswordRequest(BaseModel):
    email: EmailStr
    code: str
    new_password: str


# -------------------------------
# INTERNAL HELPERS
# -------------------------------
RESET_CODE_TTL_MINUTES = 10
RESET_CODE_MAX_ATTEMPTS = 5


def get_latest_active_reset_code(
    db: Session,
    user_id: int,
    email: str,
):
    return (
        db.query(PasswordResetCode)
        .filter(
            PasswordResetCode.user_id == user_id,
            PasswordResetCode.email == email,
            PasswordResetCode.used.is_(False),
        )
        .order_by(PasswordResetCode.created_at.desc(), PasswordResetCode.id.desc())
        .first()
    )


def invalidate_existing_reset_codes(
    db: Session,
    user_id: int,
    email: str,
):
    (
        db.query(PasswordResetCode)
        .filter(
            PasswordResetCode.user_id == user_id,
            PasswordResetCode.email == email,
            PasswordResetCode.used.is_(False),
        )
        .update({"used": True}, synchronize_session=False)
    )


# -------------------------------
# REGISTER USER
# -------------------------------
@router.post("/register")
def register(request: RegisterRequest, db: Session = Depends(get_db)):
    try:
        # ✅ normalize email
        clean_email = normalize_email(request.email)

        # ✅ Enforce acceptance (protects you legally + technically)
        if request.accepted_control_terms is not True:
            raise HTTPException(
                status_code=400,
                detail="You must accept the Control & Automation Acknowledgment to create an account.",
            )

        user_exists = db.query(User).filter(User.email == clean_email).first()
        if user_exists:
            raise HTTPException(status_code=400, detail="Email already registered")

        new_user = User(
            name=request.name,
            company=request.company,
            email=clean_email,
            hashed_password=hash_password(request.password),
            # 🔐 Store acceptance fields
            accepted_control_terms=True,
            control_terms_version=request.control_terms_version,
            control_terms_accepted_at=None,  # will set below using DB time
        )

        # ✅ Use DB timestamp (safer than app server time)
        # Set on the instance before commit so it persists.
        from sqlalchemy.sql import func

        new_user.control_terms_accepted_at = func.now()

        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        return {"message": "User created successfully"}

    except HTTPException:
        raise  # ✅ let FastAPI handle it

    except Exception as e:
        print("🔥 REGISTER ERROR:", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# -------------------------------
# LOGIN USER (UPDATED)
# -------------------------------
@router.post("/login")
def login(request: LoginRequest, db: Session = Depends(get_db)):
    try:
        clean_email = normalize_email(request.email)
        print(">>> Login attempt:", clean_email)

        user = db.query(User).filter(User.email == clean_email).first()

        if not user or not verify_password(request.password, user.hashed_password):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        # ✅ IMPORTANT: include BOTH email and user_id in token
        # This lets the frontend safely detect user changes and reset state.
        token = create_access_token(
            {
                "sub": user.email,
                "user_id": user.id,
            }
        )

        return {
            "access_token": token,
            "token_type": "bearer",
        }

    except HTTPException:
        raise  # ✅ let FastAPI handle it

    except Exception as e:
        print("🔥 LOGIN ERROR:", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# -------------------------------
# FORGOT PASSWORD
# STEP 1 → user enters email
# Backend creates a temporary code and emails it
# -------------------------------
@router.post("/forgot-password")
def forgot_password(request: ForgotPasswordRequest, db: Session = Depends(get_db)):
    try:
        clean_email = normalize_email(request.email)

        # ✅ Security-friendly generic response
        generic_message = (
            "If an account exists for this email, a temporary code has been sent."
        )

        user = db.query(User).filter(User.email == clean_email).first()

        # ✅ Do not reveal whether email exists
        if not user:
            return {"message": generic_message}

        # ✅ Invalidate any previous unused reset codes for this user/email
        invalidate_existing_reset_codes(db, user.id, clean_email)

        # ✅ Generate raw code + hash
        raw_code = generate_reset_code()
        code_hash = hash_reset_code(raw_code)
        expires_at = datetime.utcnow() + timedelta(minutes=RESET_CODE_TTL_MINUTES)

        reset_row = PasswordResetCode(
            user_id=user.id,
            email=clean_email,
            code_hash=code_hash,
            expires_at=expires_at,
            used=False,
            attempt_count=0,
        )

        db.add(reset_row)
        db.commit()

        # ✅ REAL email service (Resend)
        send_reset_code_email(
            to_email=clean_email,
            code=raw_code,
            expires_minutes=RESET_CODE_TTL_MINUTES,
        )

        return {"message": generic_message}

    except HTTPException:
        raise

    except Exception as e:
        print("🔥 FORGOT PASSWORD ERROR:", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# -------------------------------
# RESET PASSWORD
# STEP 2 → user enters email + code + new password
# -------------------------------
@router.post("/reset-password")
def reset_password(request: ResetPasswordRequest, db: Session = Depends(get_db)):
    try:
        clean_email = normalize_email(request.email)
        code = str(request.code or "").strip()
        new_password = str(request.new_password or "")

        if not code:
            raise HTTPException(status_code=400, detail="Reset code is required")

        if len(new_password) < 6:
            raise HTTPException(
                status_code=400,
                detail="New password must be at least 6 characters long",
            )

        user = db.query(User).filter(User.email == clean_email).first()
        if not user:
            raise HTTPException(
                status_code=400,
                detail="Invalid or expired reset code",
            )

        reset_row = get_latest_active_reset_code(db, user.id, clean_email)
        if not reset_row:
            raise HTTPException(
                status_code=400,
                detail="Invalid or expired reset code",
            )

        # ✅ Expired
        if reset_row.expires_at < datetime.utcnow():
            reset_row.used = True
            db.commit()
            raise HTTPException(
                status_code=400,
                detail="The reset code is invalid or has expired",
            )

        # ✅ Too many attempts
        if int(reset_row.attempt_count or 0) >= RESET_CODE_MAX_ATTEMPTS:
            reset_row.used = True
            db.commit()
            raise HTTPException(
                status_code=400,
                detail="Too many invalid attempts. Please request a new reset code",
            )

        # ✅ Wrong code
        if not verify_reset_code(code, reset_row.code_hash):
            reset_row.attempt_count = int(reset_row.attempt_count or 0) + 1

            if reset_row.attempt_count >= RESET_CODE_MAX_ATTEMPTS:
                reset_row.used = True

            db.commit()

            raise HTTPException(
                status_code=400,
                detail="The reset code is invalid or has expired",
            )

        # ✅ Success → update password
        user.hashed_password = hash_password(new_password)

        # ✅ mark current row as used
        reset_row.used = True

        # ✅ also invalidate any other still-open codes just in case
        invalidate_existing_reset_codes(db, user.id, clean_email)

        db.commit()

        return {"message": "Password reset successfully"}

    except HTTPException:
        raise

    except Exception as e:
        print("🔥 RESET PASSWORD ERROR:", e)
        raise HTTPException(status_code=500, detail="Internal server error")