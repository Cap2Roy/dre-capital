"""Authentication router: login, logout, current-user, password change."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services.auth import (
    SESSION_COOKIE,
    authenticate_user,
    clear_session_cookie,
    create_user,
    get_current_user_from_request,
    hash_password,
    set_session_cookie,
    update_user_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class UserInfo(BaseModel):
    id: str
    email: str
    name: str
    role: str
    active: bool

    class Config:
        from_attributes = True


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response):
    """Authenticate and set session cookie."""
    db: Session = next(get_db())
    try:
        user = authenticate_user(db, body.email, body.password)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        set_session_cookie(response, user.id)
        return UserInfo.model_validate(user)
    finally:
        db.close()


@router.post("/logout")
def logout(response: Response):
    """Clear session cookie."""
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me", response_model=UserInfo)
def get_me(request: Request):
    """Get the current logged-in user."""
    db: Session = next(get_db())
    try:
        user = get_current_user_from_request(request, db)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return UserInfo.model_validate(user)
    finally:
        db.close()


@router.post("/change-password")
def change_password(body: ChangePasswordRequest, request: Request):
    """Change the current user's password."""
    db: Session = next(get_db())
    try:
        user = get_current_user_from_request(request, db)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        # Verify current password
        from app.services.auth import verify_password
        if not verify_password(body.current_password, user.password_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        if len(body.new_password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        update_user_password(db, user, body.new_password)
        return {"ok": True}
    finally:
        db.close()
