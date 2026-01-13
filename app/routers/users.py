from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.database import get_session
from app.models import User, UserUpdate, UserPrivate
from app.security import get_current_active_user

router = APIRouter(prefix="/users", tags=["Users"])


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
