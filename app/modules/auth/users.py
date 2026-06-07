from fastapi import APIRouter, Depends, File, UploadFile
from sqlmodel import Session

from app.core import cache
from app.core.database import get_session
from app.core.models import User, UserUpdate, UserPrivate
from app.core.security import get_current_active_user
from app.utils.storage import upload_profile_picture_to_r2

router = APIRouter(prefix="/users", tags=["Users"])


def _me_key(current_user: User) -> str:
    return cache.me_key("user", current_user.id)


@router.get("/me", response_model=UserPrivate)
def read_current_user_profile(
    current_user: User = Depends(get_current_active_user),
):
    """
    Get the full profile for the currently authenticated user.

    Cached in Redis (profile rarely changes between writes); invalidated on any
    PATCH / picture upload below.
    """
    key = _me_key(current_user)
    cached = cache.cache_get_json(key)
    if cached is not None:
        return cached
    data = UserPrivate.model_validate(current_user, from_attributes=True).model_dump(
        mode="json"
    )
    cache.cache_set_json(key, data, cache.ME_CACHE_TTL)
    return data


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

    cache.cache_delete(_me_key(current_user))
    return current_user


@router.put("/me/profile-picture", response_model=UserPrivate)
async def update_profile_picture(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_active_user),
    file: UploadFile = File(...),
):
    """
    Update the profile picture for the currently authenticated user by uploading to Cloudflare R2.
    """
    # Upload to R2 and get the public URL
    public_url = await upload_profile_picture_to_r2(file, "user", str(current_user.id))

    # Save the R2 URL to the database
    current_user.avatar_url = public_url
    session.add(current_user)
    session.commit()
    session.refresh(current_user)

    cache.cache_delete(_me_key(current_user))
    return current_user
