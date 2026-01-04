# jwt_handler.py
from datetime import datetime, timedelta, timezone
from jose import jwt
import secrets
import os

# ✅ Use env var if available (Render), fallback to your dev value
SECRET_KEY = os.getenv("SECRET_KEY", "CORE_FLEX_SECRET_CHANGE_ME")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours


def create_access_token(data: dict, expires_minutes: int = ACCESS_TOKEN_EXPIRE_MINUTES):
    """
    Creates a JWT with:
      - sub (email) from `data`
      - iat: issued-at (unix timestamp)
      - exp: expiry (unix timestamp)
      - jti: random unique id (guarantees uniqueness)
    """
    to_encode = data.copy()

    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=expires_minutes)

    to_encode.update({
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": secrets.token_hex(16),
    })

    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
