# auth_utils.py

from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from jose import JWTError, jwt

from database import get_db
from models import User
from jwt_handler import SECRET_KEY, ALGORITHM

# ========================================
# 🔐 PASSWORD HASHING
# ========================================

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str):
    # bcrypt only accepts passwords ≤ 72 bytes
    safe_password = password[:72]
    return pwd_context.hash(safe_password)


def verify_password(plain_password: str, hashed_password: str):
    # truncate before verify (bcrypt requirement)
    safe_plain = plain_password[:72]
    return pwd_context.verify(safe_plain, hashed_password)


# ========================================
# 👤 CURRENT USER DEPENDENCY (JWT)
# ========================================

def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
):
    # ✅ Always handle missing/invalid headers safely (never crash -> no 500)
    auth = request.headers.get("Authorization") or ""
    auth = auth.strip()

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not auth:
        raise credentials_exception

    parts = auth.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise credentials_exception

    token = parts[1].strip()
    if not token or token.lower() in {"null", "undefined"}:
        raise credentials_exception

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = (payload.get("sub") or "").strip().lower()
        if not email:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise credentials_exception

    return user
