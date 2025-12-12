from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import User
from auth_utils import hash_password, verify_password
from jwt_handler import create_access_token

# ✅ ADD PREFIX HERE
router = APIRouter(
    prefix="/auth",
    tags=["auth"]
)

# -------------------------------
# REQUEST MODELS
# -------------------------------
class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    company: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


# -------------------------------
# REGISTER USER
# -------------------------------
@router.post("/register")
def register(request: RegisterRequest, db: Session = Depends(get_db)):
    try:
        print(">>> Register request received:", request)

        # Check if email exists
        user_exists = db.query(User).filter(User.email == request.email).first()
        if user_exists:
            raise HTTPException(status_code=400, detail="Email already registered")

        # Create new user
        new_user = User(
            name=request.name,
            company=request.company,
            email=request.email,
            hashed_password=hash_password(request.password),
        )

        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        print(">>> User created successfully:", new_user.id)

        return {"message": "User created successfully"}

    except Exception as e:
        print("🔥🔥 REGISTER ERROR:", e)
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")


# -------------------------------
# LOGIN USER
# -------------------------------
@router.post("/login")
def login(request: LoginRequest, db: Session = Depends(get_db)):
    try:
        print(">>> Login attempt for:", request.email)

        user = db.query(User).filter(User.email == request.email).first()

        if not user or not verify_password(request.password, user.hashed_password):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        token = create_access_token({"sub": user.email})

        print(">>> Login successful for:", request.email)

        return {
            "access_token": token,
            "token_type": "bearer"
        }

    except Exception as e:
        print("🔥🔥 LOGIN ERROR:", e)
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")
