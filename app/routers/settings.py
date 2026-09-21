"""Settings router — admin only: get/update API keys and call cadence."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.auth import require_admin
from app.services.settings import get_all_settings, set_setting

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingItem(BaseModel):
    key: str
    value: str


class UpdateSettingsRequest(BaseModel):
    settings: list[SettingItem]


@router.get("")
def list_settings(
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """List all settings (secrets masked). Admin only."""
    return get_all_settings(db)


@router.put("")
def update_settings(
    body: UpdateSettingsRequest,
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """Update settings. Admin only.

    For secret fields, sending a masked value (all dots) or empty string
    is treated as 'keep current'.  Send the real value to overwrite.
    """
    updated: list[str] = []
    for item in body.settings:
        # Skip empty or masked-dots values for secrets — keep existing
        val = item.value
        if val and set(val) == {"\u2022"}:
            continue  # all dots = keep current
        if not val:
            # Empty string = clear the setting
            set_setting(db, item.key, "")
        else:
            set_setting(db, item.key, val)
        updated.append(item.key)
    db.commit()
    return {"ok": True, "updated": updated}
