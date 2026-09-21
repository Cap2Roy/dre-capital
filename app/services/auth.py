"""Authentication: password hashing, session cookies, role-based access.

Uses itsdangerous (already a dependency) for signed session cookies —
no external session store needed.  Roles: acquisitions, manager, admin.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
import bcrypt
from app.config import get_settings
from app.database import get_db
from app.models import User

settings = get_settings()

# Session cookie name + max age (8 hours)
SESSION_COOKIE = "dre_session"
SESSION_MAX_AGE = 8 * 3600  # 8 hours in seconds

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="dre-session")


# ── Password hashing ───────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    pw_bytes = password.encode("utf-8")
    # bcrypt has a 72-byte limit; truncate to stay safe
    pw_bytes = pw_bytes[:72]
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode("utf-8"))
    except Exception:
        return False


# ── Session tokens ──────────────────────────────────────────────────────────

def create_session_token(user_id: str) -> str:
    """Sign a user_id + timestamp token for the session cookie."""
    return _serializer.dumps({"uid": user_id, "ts": datetime.now(timezone.utc).isoformat()})


def decode_session_token(token: str) -> Optional[dict]:
    """Validate and decode a session token.  Returns payload or None."""
    try:
        return _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


# ── Current user dependencies ────────────────────────────────────────────────

def get_current_user_from_request(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Optional[User]:
    """Extract the user from the session cookie, or None if not logged in."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    payload = decode_session_token(token)
    if not payload:
        return None
    user = db.get(User, payload.get("uid"))
    if not user or not user.active:
        return None
    return user


def require_user(request: Request) -> User:
    """Dependency: require an authenticated user (set by auth middleware)."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


def require_admin(request: Request) -> User:
    """Dependency: require an admin user (set by auth middleware)."""
    user = require_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


def set_session_cookie(response, user_id: str) -> None:
    """Attach the session cookie to a response."""
    token = create_session_token(user_id)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=not settings.is_sqlite,  # HTTPS in prod, HTTP in dev
    )


def clear_session_cookie(response) -> None:
    """Remove the session cookie."""
    response.delete_cookie(key=SESSION_COOKIE)


# ── User CRUD helpers ──────────────────────────────────────────────────────

def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.execute(select(User).where(User.email == email)).scalar_one_or_none()


def authenticate_user(db: Session, email: str, password: str) -> Optional[User]:
    user = get_user_by_email(db, email)
    if not user or not user.active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def create_user(
    db: Session,
    email: str,
    name: str,
    password: str,
    role: str = "acquisitions",
) -> User:
    """Create a new user.  Raises ValueError if email already exists."""
    if get_user_by_email(db, email):
        raise ValueError(f"User with email {email} already exists")
    user = User(
        email=email,
        name=name,
        role=role,
        password_hash=hash_password(password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def update_user_password(db: Session, user: User, new_password: str) -> None:
    user.password_hash = hash_password(new_password)
    db.commit()


def deactivate_user(db: Session, user: User) -> None:
    user.active = False
    db.commit()


def reactivate_user(db: Session, user: User) -> None:
    user.active = True
    db.commit()
