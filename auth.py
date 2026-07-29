import os
from datetime import datetime, timedelta

import bcrypt
from jose import jwt, JWTError
from sqlalchemy import func
from sqlalchemy.orm import Session
from models import Admin

SECRET_KEY = os.getenv("JWT_SECRET_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60  # 1 hour


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain_password, hashed_password):
    # A malformed or legacy hash must fail the login, not crash the request
    try:
        return bcrypt.checkpw(
            str(plain_password).encode("utf-8"),
            str(hashed_password).encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


def authenticate_admin(db: Session, username: str, password: str):
    # Matched without regard to case or surrounding spaces: an account created
    # as "Henry" could not be used by typing "henry", and a username autofilled
    # with a trailing space matched nothing at all.
    cleaned = (username or "").strip()
    if not cleaned:
        return False

    admin = (
        db.query(Admin)
        .filter(func.lower(Admin.username) == cleaned.lower())
        .first()
    )
    if not admin:
        return False
    if not verify_password(password, admin.password_hash):
        return False
    return admin


def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta if expires_delta else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt
