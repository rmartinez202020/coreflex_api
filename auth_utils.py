from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str):
    # bcrypt only accepts passwords ≤ 72 bytes
    safe_password = password[:72]
    return pwd_context.hash(safe_password)

def verify_password(plain_password: str, hashed_password: str):
    # truncate before verify (bcrypt requirement)
    safe_plain = plain_password[:72]
    return pwd_context.verify(safe_plain, hashed_password)
