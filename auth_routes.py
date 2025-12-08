from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import User
from auth_utils import hash_password, verify_password
from jwt_handler import create_access_token

router = APIRouter()

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

    return {"message": "User created successfully"}


# -------------------------------
# LOGIN USER
# -------------------------------
@router.post("/login")
def login(request: LoginRequest, db: Session = Depends(get_db)):
    # Find user
    user = db.query(User).filter(User.email == request.email).first()

    # Validate credentials
    if not user or not verify_password(request.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Generate JWT token
    token = create_access_token({"sub": user.email})

    return {"access_token": token, "token_type": "bearer"}
