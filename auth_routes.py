# auth_routes.py

from datetime import datetime, timedelta, timezone
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from database import get_db
from models import (
    User,
    PasswordResetCode,
    UserSubscription,
    UserActiveSession,
)
from auth_utils import (
    hash_password,
    verify_password,
    normalize_email,
    generate_reset_code,
    hash_reset_code,
    verify_reset_code,
    get_current_user,
)
from jwt_handler import create_access_token
from utils.email_service import send_reset_code_email

router = APIRouter(prefix="/auth", tags=["auth"])

# ✅ owner allowlist
PLATFORM_OWNER_EMAIL = "roquemartinez_8@hotmail.com"

# -------------------------------
# INTERNAL CONFIG
# -------------------------------
RESET_CODE_TTL_MINUTES = 10
RESET_CODE_MAX_ATTEMPTS = 5

# ✅ default starter subscription for every new account
DEFAULT_PLAN_KEY = "free"
DEFAULT_DEVICE_LIMIT = 1
DEFAULT_TENANT_USERS_LIMIT = 1

# ✅ session is considered alive if pinged recently
SESSION_TTL_SECONDS = 90


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
    browser_device_key: str


class SessionPingRequest(BaseModel):
    session_token: str
    browser_device_key: str


class LogoutRequest(BaseModel):
    session_token: str
    browser_device_key: str


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
def _now_utc():
    return datetime.now(timezone.utc)


def _session_cutoff():
    return _now_utc() - timedelta(seconds=SESSION_TTL_SECONDS)


def _clean_browser_device_key(value: str) -> str:
    return str(value or "").strip()


def _generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def is_platform_owner(user) -> bool:
    user_email = normalize_email(getattr(user, "email", ""))
    return user_email == normalize_email(PLATFORM_OWNER_EMAIL)


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


def _close_stale_sessions_for_user(db: Session, user_id: int):
    stale_rows = (
        db.query(UserActiveSession)
        .filter(UserActiveSession.user_id == user_id)
        .filter(UserActiveSession.is_active.is_(True))
        .filter(UserActiveSession.last_seen_at < _session_cutoff())
        .all()
    )

    now = _now_utc()
    for row in stale_rows:
        row.is_active = False
        row.closed_at = now


def _get_live_active_sessions_for_user(db: Session, user_id: int):
    _close_stale_sessions_for_user(db, user_id)
    db.flush()

    return (
        db.query(UserActiveSession)
        .filter(UserActiveSession.user_id == user_id)
        .filter(UserActiveSession.is_active.is_(True))
        .filter(UserActiveSession.last_seen_at >= _session_cutoff())
        .order_by(UserActiveSession.last_seen_at.desc(), UserActiveSession.id.desc())
        .all()
    )


def _get_request_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return str(request.client.host).strip()
    return None


def _get_request_user_agent(request: Request) -> str | None:
    value = request.headers.get("user-agent")
    return str(value).strip() if value else None


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
            accepted_control_terms=True,
            control_terms_version=request.control_terms_version,
            control_terms_accepted_at=None,
        )

        from sqlalchemy.sql import func

        new_user.control_terms_accepted_at = func.now()

        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        # ✅ Every newly registered user starts with the FREE plan
        existing_subscription = (
            db.query(UserSubscription)
            .filter(UserSubscription.user_id == new_user.id)
            .first()
        )

        if not existing_subscription:
            new_subscription = UserSubscription(
                user_id=new_user.id,
                plan_key=DEFAULT_PLAN_KEY,
                device_limit=DEFAULT_DEVICE_LIMIT,
                tenants_users_limit=DEFAULT_TENANT_USERS_LIMIT,
                active_date=func.now(),
                renewal_date=None,
                is_active=True,
                created_at=func.now(),
                updated_at=func.now(),
            )
            db.add(new_subscription)
            db.commit()

        return {"message": "User created successfully"}

    except HTTPException:
        raise

    except Exception as e:
        db.rollback()
        print("🔥 REGISTER ERROR:", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# -------------------------------
# LOGIN USER (UPDATED)
# ✅ same browser_device_key = allowed
# ✅ different browser_device_key with live session = blocked
# -------------------------------
@router.post("/login")
def login(
    body: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        clean_email = normalize_email(body.email)
        browser_device_key = _clean_browser_device_key(body.browser_device_key)

        if not browser_device_key:
            raise HTTPException(
                status_code=400,
                detail="Browser device key is required.",
            )

        print(">>> Login attempt:", clean_email)

        user = db.query(User).filter(User.email == clean_email).first()

        if not user or not verify_password(body.password, user.hashed_password):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        live_sessions = _get_live_active_sessions_for_user(db, user.id)

        same_browser_session = None
        conflicting_session = None

        for sess in live_sessions:
            if _clean_browser_device_key(sess.browser_device_key) == browser_device_key:
                same_browser_session = sess
            else:
                conflicting_session = sess
                break

        if conflicting_session:
            db.commit()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Account already active. This account is currently signed in "
                    "from another browser or device. Please sign out from that "
                    "session and try again."
                ),
            )

        now = _now_utc()
        ip_address = _get_request_ip(request)
        user_agent = _get_request_user_agent(request)

        if same_browser_session:
            same_browser_session.last_seen_at = now
            same_browser_session.is_active = True
            same_browser_session.closed_at = None
            same_browser_session.ip_address = ip_address
            same_browser_session.user_agent = user_agent
            session_token = same_browser_session.session_token
        else:
            session_token = _generate_session_token()
            new_session = UserActiveSession(
                user_id=user.id,
                browser_device_key=browser_device_key,
                session_token=session_token,
                is_active=True,
                created_at=now,
                last_seen_at=now,
                closed_at=None,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            db.add(new_session)

        token = create_access_token(
            {
                "sub": user.email,
                "user_id": user.id,
            }
        )

        db.commit()

        return {
            "access_token": token,
            "token_type": "bearer",
            "session_token": session_token,
            "browser_device_key": browser_device_key,
        }

    except HTTPException:
        raise

    except Exception as e:
        db.rollback()
        print("🔥 LOGIN ERROR:", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# -------------------------------
# SESSION PING
# ✅ keeps same-browser session alive
# -------------------------------
@router.post("/session/ping")
def session_ping(
    body: SessionPingRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        session_token = str(body.session_token or "").strip()
        browser_device_key = _clean_browser_device_key(body.browser_device_key)

        if not session_token:
            raise HTTPException(status_code=400, detail="Session token is required.")

        if not browser_device_key:
            raise HTTPException(
                status_code=400,
                detail="Browser device key is required.",
            )

        row = (
            db.query(UserActiveSession)
            .filter(UserActiveSession.user_id == current_user.id)
            .filter(UserActiveSession.session_token == session_token)
            .filter(UserActiveSession.browser_device_key == browser_device_key)
            .filter(UserActiveSession.is_active.is_(True))
            .first()
        )

        if not row:
            raise HTTPException(status_code=404, detail="Active session not found.")

        row.last_seen_at = _now_utc()
        row.closed_at = None
        db.commit()

        return {"ok": True, "detail": "Session refreshed."}

    except HTTPException:
        raise

    except Exception as e:
        db.rollback()
        print("🔥 SESSION PING ERROR:", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# -------------------------------
# LOGOUT
# ✅ closes only this current browser session
# -------------------------------
@router.post("/logout")
def logout(
    body: LogoutRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        session_token = str(body.session_token or "").strip()
        browser_device_key = _clean_browser_device_key(body.browser_device_key)

        if not session_token:
            raise HTTPException(status_code=400, detail="Session token is required.")

        if not browser_device_key:
            raise HTTPException(
                status_code=400,
                detail="Browser device key is required.",
            )

        row = (
            db.query(UserActiveSession)
            .filter(UserActiveSession.user_id == current_user.id)
            .filter(UserActiveSession.session_token == session_token)
            .filter(UserActiveSession.browser_device_key == browser_device_key)
            .filter(UserActiveSession.is_active.is_(True))
            .first()
        )

        if row:
            row.is_active = False
            row.closed_at = _now_utc()
            row.last_seen_at = _now_utc()
            db.commit()
        else:
            db.commit()

        return {"ok": True, "detail": "Logged out successfully."}

    except HTTPException:
        raise

    except Exception as e:
        db.rollback()
        print("🔥 LOGOUT ERROR:", e)
        raise HTTPException(status_code=500, detail="Internal server error")


# -------------------------------
# OWNER-ONLY: LIST REGISTERED USERS
# ✅ excludes hashed_password
# -------------------------------
@router.get("/business-users")
def get_business_users(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        if not is_platform_owner(current_user):
            raise HTTPException(status_code=403, detail="Owner access required")

        users = db.query(User).order_by(User.id.asc()).all()

        return {
            "users": [
                {
                    "id": u.id,
                    "name": u.name,
                    "company": u.company,
                    "email": u.email,
                    "control_terms_accepted_at": u.control_terms_accepted_at,
                    "control_terms_version": u.control_terms_version,
                    "accepted_control_terms": bool(u.accepted_control_terms),
                }
                for u in users
            ]
        }

    except HTTPException:
        raise

    except Exception as e:
        print("🔥 BUSINESS USERS ERROR:", e)
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

        generic_message = (
            "If an account exists for this email, a temporary code has been sent."
        )

        user = db.query(User).filter(User.email == clean_email).first()

        if not user:
            return {"message": generic_message}

        invalidate_existing_reset_codes(db, user.id, clean_email)

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

        if reset_row.expires_at < datetime.utcnow():
            reset_row.used = True
            db.commit()
            raise HTTPException(
                status_code=400,
                detail="The reset code is invalid or has expired",
            )

        if int(reset_row.attempt_count or 0) >= RESET_CODE_MAX_ATTEMPTS:
            reset_row.used = True
            db.commit()
            raise HTTPException(
                status_code=400,
                detail="Too many invalid attempts. Please request a new reset code",
            )

        if not verify_reset_code(code, reset_row.code_hash):
            reset_row.attempt_count = int(reset_row.attempt_count or 0) + 1

            if reset_row.attempt_count >= RESET_CODE_MAX_ATTEMPTS:
                reset_row.used = True

            db.commit()

            raise HTTPException(
                status_code=400,
                detail="The reset code is invalid or has expired",
            )

        user.hashed_password = hash_password(new_password)
        reset_row.used = True
        invalidate_existing_reset_codes(db, user.id, clean_email)

        db.commit()

        return {"message": "Password reset successfully"}

    except HTTPException:
        raise

    except Exception as e:
        print("🔥 RESET PASSWORD ERROR:", e)
        raise HTTPException(status_code=500, detail="Internal server error")