# auth_utils.py

from __future__ import annotations

import hashlib
import secrets
import string
import time
from threading import RLock
from types import SimpleNamespace

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
# 🔐 PASSWORD RESET HELPERS
# ========================================

RESET_CODE_LENGTH = 6
RESET_CODE_ALPHABET = string.digits


def normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def generate_reset_code(length: int = RESET_CODE_LENGTH) -> str:
    # ✅ numeric 6-digit code like 483921
    return "".join(secrets.choice(RESET_CODE_ALPHABET) for _ in range(length))


def hash_reset_code(code: str) -> str:
    raw = str(code or "").strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_reset_code(plain_code: str, code_hash: str) -> bool:
    if not plain_code or not code_hash:
        return False
    return hash_reset_code(plain_code) == str(code_hash).strip()


# ========================================
# 👤 CURRENT USER DEPENDENCY (JWT)
# ========================================

# ✅ Cache token -> (user_id, email, expires_at_epoch)
# - Avoid caching ORM User objects (bound to a DB session)
# - Still validates JWT signature every request
_TOKEN_USER_CACHE: dict[str, tuple[int, str, float]] = {}
_CACHE_LOCK = RLock()

# Safe defaults
DEFAULT_CACHE_TTL_SEC = 300  # 5 minutes
MAX_CACHE_ENTRIES = 10000  # protect memory


def _credentials_exception():
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _parse_bearer_token(request: Request) -> str:
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth:
        raise _credentials_exception()

    parts = auth.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise _credentials_exception()

    token = parts[1].strip()
    if not token or token.lower() in {"null", "undefined"}:
        raise _credentials_exception()

    return token


def _parse_bearer_token_optional(request: Request) -> str | None:
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth:
        return None

    parts = auth.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None

    token = parts[1].strip()
    if not token or token.lower() in {"null", "undefined"}:
        return None

    return token


def _cache_get(token: str):
    now = time.time()
    with _CACHE_LOCK:
        v = _TOKEN_USER_CACHE.get(token)
        if not v:
            return None
        user_id, email, exp = v
        if exp <= now:
            # expired cache entry
            _TOKEN_USER_CACHE.pop(token, None)
            return None
        return user_id, email


def _cache_set(token: str, user_id: int, email: str, ttl_sec: float):
    now = time.time()
    exp = now + max(1.0, float(ttl_sec or DEFAULT_CACHE_TTL_SEC))

    with _CACHE_LOCK:
        # simple guard against unbounded growth
        if len(_TOKEN_USER_CACHE) >= MAX_CACHE_ENTRIES:
            # cheap eviction: drop ~10% oldest-ish by random iteration order
            # (good enough; keeps code simple)
            for i, k in enumerate(list(_TOKEN_USER_CACHE.keys())):
                _TOKEN_USER_CACHE.pop(k, None)
                if i >= max(1, MAX_CACHE_ENTRIES // 10):
                    break

        _TOKEN_USER_CACHE[token] = (int(user_id), str(email), exp)


def _build_user_like_from_token(
    token: str,
    db: Session,
    *,
    strict: bool,
):
    """
    Shared JWT -> lightweight user resolver.
    strict=True  => raise 401 on invalid/missing user
    strict=False => return None on invalid/missing user
    """
    if not token:
        if strict:
            raise _credentials_exception()
        return None

    # 1) Always decode/verify JWT signature
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        if strict:
            raise _credentials_exception()
        return None

    email: str = normalize_email(payload.get("sub"))
    if not email:
        if strict:
            raise _credentials_exception()
        return None

    # Optional optimization: uid embedded in JWT
    uid = payload.get("uid") or payload.get("user_id")
    if uid is not None:
        try:
            uid_int = int(uid)
            exp_claim = payload.get("exp")
            ttl = DEFAULT_CACHE_TTL_SEC
            if exp_claim:
                ttl = max(1, float(exp_claim) - time.time())
            _cache_set(token, uid_int, email, ttl)
            return SimpleNamespace(id=uid_int, email=email)
        except Exception:
            pass

    # 2) Cache hit
    cached = _cache_get(token)
    if cached:
        user_id, cached_email = cached
        if cached_email == email:
            return SimpleNamespace(id=user_id, email=email)

    # 3) Cache miss -> DB lookup
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        if strict:
            raise _credentials_exception()
        return None

    ttl = DEFAULT_CACHE_TTL_SEC
    exp_claim = payload.get("exp")
    if exp_claim:
        try:
            ttl = max(1, float(exp_claim) - time.time())
        except Exception:
            ttl = DEFAULT_CACHE_TTL_SEC

    _cache_set(token, int(user.id), normalize_email(user.email), ttl)
    return SimpleNamespace(id=int(user.id), email=normalize_email(user.email))


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    ✅ Strict auth:
      - Requires bearer token
      - Validates JWT signature every request
      - Avoids DB lookup when token is already cached
    Returns a lightweight "user-like" object with .id and .email at minimum.
    """
    token = _parse_bearer_token(request)
    return _build_user_like_from_token(token, db, strict=True)


def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    ✅ Optional auth:
      - Returns None if no bearer token is present
      - Returns None if token is invalid
      - Returns lightweight user-like object on success

    Use this on mixed routes that support:
      1) owner JWT auth
      2) public tenant/header-based access
    """
    token = _parse_bearer_token_optional(request)
    return _build_user_like_from_token(token, db, strict=False)