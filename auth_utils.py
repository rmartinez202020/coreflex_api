# auth_utils.py

from __future__ import annotations

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
# 👤 CURRENT USER DEPENDENCY (JWT)
# ========================================

# ✅ Cache token -> (user_id, email, expires_at_epoch)
# - Avoid caching ORM User objects (bound to a DB session)
# - Still validates JWT signature every request
_TOKEN_USER_CACHE: dict[str, tuple[int, str, float]] = {}
_CACHE_LOCK = RLock()

# Safe defaults
DEFAULT_CACHE_TTL_SEC = 300        # 5 minutes
MAX_CACHE_ENTRIES = 10000          # protect memory


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


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    ✅ Fast path:
      - Validate JWT signature every request
      - Avoid DB lookup when token is already cached
    Returns a lightweight "user-like" object with .id and .email at minimum.
    (This is enough for your routers: claimed_by_user_id checks, is_owner(email), etc.)
    """
    token = _parse_bearer_token(request)

    # 1) Always decode/verify JWT signature (security stays intact)
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise _credentials_exception()

    email: str = (payload.get("sub") or "").strip().lower()
    if not email:
        raise _credentials_exception()

    # If token has "uid", we can skip DB entirely (optional optimization)
    uid = payload.get("uid") or payload.get("user_id")
    if uid is not None:
        try:
            uid_int = int(uid)
            # cache based on JWT exp if present
            exp_claim = payload.get("exp")
            ttl = DEFAULT_CACHE_TTL_SEC
            if exp_claim:
                ttl = max(1, float(exp_claim) - time.time())
            _cache_set(token, uid_int, email, ttl)
            return SimpleNamespace(id=uid_int, email=email)
        except Exception:
            # fall back to normal lookup below
            pass

    # 2) Cache hit (no DB)
    cached = _cache_get(token)
    if cached:
        user_id, cached_email = cached
        # if email mismatch somehow, do not trust cache
        if cached_email == email:
            return SimpleNamespace(id=user_id, email=email)

    # 3) Cache miss → one DB lookup, then cache it
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise _credentials_exception()

    # TTL: prefer JWT exp if present, else DEFAULT_CACHE_TTL_SEC
    ttl = DEFAULT_CACHE_TTL_SEC
    exp_claim = payload.get("exp")
    if exp_claim:
        try:
            ttl = max(1, float(exp_claim) - time.time())
        except Exception:
            ttl = DEFAULT_CACHE_TTL_SEC

    _cache_set(token, int(user.id), user.email.lower().strip(), ttl)

    # return lightweight user-like object (avoid returning ORM object tied to session)
    return SimpleNamespace(id=int(user.id), email=user.email.lower().strip())