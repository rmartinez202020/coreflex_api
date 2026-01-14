from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from database import get_db
from models import User, UserProfile
from auth_utils import get_current_user

router = APIRouter(prefix="/profile", tags=["User Profile"])


# =========================
# Schemas
# =========================
class ProfileOut(BaseModel):
    full_name: str | None = None
    role_position: str | None = None
    email: str | None = None
    company: str | None = None
    company_address: str | None = None

class ProfileSave(BaseModel):
    full_name: str | None = None
    role_position: str | None = None
    email: EmailStr | None = None
    company: str | None = None
    company_address: str | None = None


# =========================
# GET current user's profile
# =========================
@router.get("", response_model=ProfileOut)
def get_my_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = (
        db.query(UserProfile)
        .filter(UserProfile.user_id == current_user.id)
        .first()
    )

    # If not created yet, return empty fields so UI still works
    if not profile:
        return ProfileOut(
            full_name=None,
            role_position=None,
            email=current_user.email,  # optional: prefill
            company=current_user.company,
            company_address=None,
        )

    return ProfileOut(
        full_name=profile.full_name,
        role_position=profile.role_position,
        email=profile.email,
        company=profile.company,
        company_address=profile.company_address,
    )


# =========================
# SAVE (upsert) profile
# =========================
@router.put("", response_model=ProfileOut)
def save_my_profile(
    payload: ProfileSave,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = (
        db.query(UserProfile)
        .filter(UserProfile.user_id == current_user.id)
        .first()
    )

    if not profile:
        profile = UserProfile(user_id=current_user.id)
        db.add(profile)

    # Update profile fields
    profile.full_name = payload.full_name
    profile.role_position = payload.role_position
    profile.email = payload.email
    profile.company = payload.company
    profile.company_address = payload.company_address

    # Optional: keep users table in sync for convenience
    # (You can remove these 2 lines if you want users table immutable)
    if payload.company is not None:
        current_user.company = payload.company

    db.commit()
    db.refresh(profile)

    return ProfileOut(
        full_name=profile.full_name,
        role_position=profile.role_position,
        email=profile.email,
        company=profile.company,
        company_address=profile.company_address,
    )
