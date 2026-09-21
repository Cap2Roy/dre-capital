"""User management router — admin only: list, create, update, deactivate users."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services.auth import (
    create_user,
    get_user_by_email,
    hash_password,
    require_admin,
    require_user,
)

router = APIRouter(prefix="/api/users", tags=["users"])


class UserInfo(BaseModel):
    id: str
    email: str
    name: str
    role: str
    active: bool

    class Config:
        from_attributes = True


class CreateUserRequest(BaseModel):
    email: str
    name: str
    password: str
    role: str = "acquisitions"  # acquisitions | manager | admin


class UpdateUserRequest(BaseModel):
    name: str | None = None
    role: str | None = None
    active: bool | None = None
    password: str | None = None  # set to reset password


@router.get("", response_model=list[UserInfo])
def list_users(
    request: Request,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """List all users.  Admin only."""
    return db.execute(select(User).order_by(User.created_at)).scalars().all()


@router.post("", response_model=UserInfo, status_code=201)
def create_new_user(
    body: CreateUserRequest,
    request: Request,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Create a new user.  Admin only."""
    if body.role not in ("acquisitions", "manager", "admin"):
        raise HTTPException(status_code=400, detail="Role must be acquisitions, manager, or admin")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    try:
        user = create_user(db, body.email, body.name, body.password, body.role)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return UserInfo.model_validate(user)


@router.patch("/{user_id}", response_model=UserInfo)
def update_user(
    user_id: str,
    body: UpdateUserRequest,
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(require_user),
):
    """Update a user.  Admin can update anyone; others can only update themselves (name only)."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    is_admin = current.role == "admin"
    is_self = current.id == user_id

    if not is_admin and not is_self:
        raise HTTPException(status_code=403, detail="Cannot modify other users")

    if is_admin:
        # Admin can change everything
        if body.name is not None:
            user.name = body.name
        if body.role is not None:
            if body.role not in ("acquisitions", "manager", "admin"):
                raise HTTPException(status_code=400, detail="Invalid role")
            user.role = body.role
        if body.active is not None:
            user.active = body.active
        if body.password is not None:
            if len(body.password) < 8:
                raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
            user.password_hash = hash_password(body.password)
    elif is_self:
        # Non-admin self: can only change name
        if body.name is not None:
            user.name = body.name
        # Silently ignore other fields
    db.commit()
    db.refresh(user)
    return UserInfo.model_validate(user)


@router.delete("/{user_id}")
def delete_user(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Deactivate a user (soft delete — preserves call history).  Admin only."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin" and _admin.id == user_id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    user.active = False
    db.commit()
    return {"ok": True, "message": "User deactivated"}

@router.post("/{user_id}/deactivate")
def deactivate_user_post(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Deactivate a user via POST (frontend convenience).  Admin only."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin" and _admin.id == user_id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    user.active = False
    db.commit()
    return {"ok": True, "message": "User deactivated"}
