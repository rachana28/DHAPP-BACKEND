import os
import shutil
from fastapi import APIRouter, Depends, File, UploadFile
from sqlmodel import Session
import time

from app.database import get_session
from app.models import User, UserUpdate, UserPrivate
from app.security import get_current_active_user

router = APIRouter(prefix="/users", tags=["Users"])

# Create media directory if it doesn't exist
os.makedirs("media/profile_pictures", exist_ok=True)


@router.get("/me", response_model=UserPrivate)
def read_current_user_profile(
    current_user: User = Depends(get_current_active_user),
):
    """
    Get the full profile for the currently authenticated user.
    """
    return current_user


@router.patch("/me", response_model=UserPrivate)
def update_current_user_profile(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_active_user),
    user_update: UserUpdate,
):
    """
    Update the profile for the currently authenticated user.
    """
    update_data = user_update.model_dump(exclude_unset=True)

    for key, value in update_data.items():
        setattr(current_user, key, value)

    session.add(current_user)
    session.commit()
    session.refresh(current_user)

    return current_user


@router.put("/me/profile-picture", response_model=UserPrivate)
def update_profile_picture(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_active_user),
    file: UploadFile = File(...),
):
    """
    Update the profile picture for the currently authenticated user.
    """
    timestamp = int(time.time())
    file_extension = os.path.splitext(file.filename)[1]
    # Sanitize filename if needed, but here we generate a new one
    file_path = f"media/profile_pictures/user_{current_user.id}_{timestamp}{file_extension}"

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # Update the user's avatar_url. The '/media' part will be used for serving.
    current_user.avatar_url = f"/{file_path}"
    session.add(current_user)
    session.commit()
    session.refresh(current_user)

    return current_user
