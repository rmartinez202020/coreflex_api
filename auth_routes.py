# auth_routes.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import User
from auth_utils import hash_password, verify_password
from jwt_handler import create_access_token

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


# -------------------------------
# REGISTER USER
# -------------------------------
@router.post("/register")
def register(request: RegisterRequest, db: Session = Depends(get_db)):
    try:
        # ✅ Enforce acceptance (protects you legally + technically)
        if request.accepted_control_terms is not True:
            raise HTTPException(
                status_code=400,
                detail="You must accept the Control & Automation Acknowledgment to create an account.",
            )

        user_exists = db.query(User).filter(User.email == request.email).first()
        if user_exists:
            raise HTTPException(status_code=400, detail="Email already registered")

        new_user = User(
            name=request.name,
            company=request.company,
            email=request.email,
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
        print(">>> Login attempt:", request.email)

        user = db.query(User).filter(User.email == request.email).first()

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
