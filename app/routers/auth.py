"""Authentication router: login, logout, current-user, password change, Google OAuth."""
from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import User
from app.services.auth import (
    clear_session_cookie,
    get_current_user_from_request,
    get_user_by_email,
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


@router.get("/google/login")
def google_login(request: Request):
    """Redirect to Google's OAuth consent screen."""
    s = get_settings()
    if not s.google_auth_enabled:
        raise HTTPException(503, "Google OAuth is not configured")
    redirect_uri = f"{s.app_base_url}/api/auth/google/callback"
    params = urllib.parse.urlencode({
        "client_id": s.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "prompt": "select_account",
    })
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@router.get("/google/callback")
async def google_callback(request: Request):
    """Handle Google's OAuth callback — exchange code, find/create user, set session."""
    import httpx

    s = get_settings()
    if not s.google_auth_enabled:
        raise HTTPException(503, "Google OAuth is not configured")

    code = request.query_params.get("code")
    if not code:
        return RedirectResponse(f"{s.app_base_url}/login?error=oauth_denied")

    redirect_uri = f"{s.app_base_url}/api/auth/google/callback"

    # Exchange authorization code for tokens
    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            return RedirectResponse(f"{s.app_base_url}/login?error=oauth_token_failed")
        tokens = token_resp.json()

        # Fetch user info from Google
        user_resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        if user_resp.status_code != 200:
            return RedirectResponse(f"{s.app_base_url}/login?error=oauth_userinfo_failed")
        google_user = user_resp.json()

    email = google_user.get("email", "").lower().strip()
    if not email:
        return RedirectResponse(f"{s.app_base_url}/login?error=oauth_no_email")

    # Find or create the user
    db: Session = next(get_db())
    try:
        user = get_user_by_email(db, email)
        if not user:
            # Auto-create new Google sign-ins as acquisitions role
            name = google_user.get("name") or email.split("@")[0]
            user = User(
                email=email,
                name=name,
                role="acquisitions",
                password_hash=None,  # OAuth users have no password
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        elif not user.active:
            return RedirectResponse(f"{s.app_base_url}/login?error=account_deactivated")

        # Set session cookie and redirect to dashboard
        response = RedirectResponse(s.app_base_url)
        set_session_cookie(response, user.id)
        return response
    finally:
        db.close()


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


@router.get("/google/config")
def google_config():
    """Return whether Google OAuth is enabled (for the login page UI)."""
    s = get_settings()
    return {"enabled": s.google_auth_enabled}

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
