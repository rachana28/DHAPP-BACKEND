from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from sqlmodel import Session, select, func
import redis

from app.core import cache
from app.core.database import get_session, get_redis
from app.core.models import (
    AvailabilityUpdate,
    Mechanic,
    MechanicUpdate,
    MechanicPublic,
    MechanicPrivate,
    MechanicTrip,
)
from app.core.security import get_current_active_mechanic
from app.utils.storage import upload_profile_picture_to_r2, upload_document_to_r2
from app.utils.id_generator import get_by_reference

router = APIRouter(prefix="/mechanics", tags=["Mechanics"])


def _mechanic_me_key(current_mechanic: Mechanic) -> str:
    return cache.me_key("mechanic", current_mechanic.user_id)


@router.get("/me", response_model=MechanicPrivate)
def read_current_mechanic_profile(
    current_mechanic: Mechanic = Depends(get_current_active_mechanic),
):
    key = _mechanic_me_key(current_mechanic)
    cached = cache.cache_get_json(key)
    if cached is not None:
        return cached
    data = MechanicPrivate.model_validate(
        current_mechanic, from_attributes=True
    ).model_dump(mode="json")
    cache.cache_set_json(key, data, cache.ME_CACHE_TTL)
    return data


@router.patch("/me/availability", response_model=MechanicPrivate)
def set_mechanic_availability(
    *,
    session: Session = Depends(get_session),
    current_mechanic: Mechanic = Depends(get_current_active_mechanic),
    body: AvailabilityUpdate,
):
    """Online/offline toggle. When offline the mechanic is excluded from geo
    dispatch (alongside the admin `status` check) without touching that status."""
    current_mechanic.is_online = body.is_online
    session.add(current_mechanic)
    session.commit()
    session.refresh(current_mechanic)
    cache.cache_delete(_mechanic_me_key(current_mechanic))
    return current_mechanic


@router.patch("/me", response_model=MechanicPrivate)
def update_current_mechanic_profile(
    *,
    session: Session = Depends(get_session),
    current_mechanic: Mechanic = Depends(get_current_active_mechanic),
    mechanic_update: MechanicUpdate,
    redis_client: redis.Redis = Depends(get_redis),
):
    update_data = mechanic_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(current_mechanic, key, value)

    if current_mechanic.status == "rejected":
        current_mechanic.status = "pending_approval"

    session.add(current_mechanic)
    session.commit()
    session.refresh(current_mechanic)
    cache.cache_delete(_mechanic_me_key(current_mechanic))
    return current_mechanic


@router.put("/me/profile-picture", response_model=MechanicPrivate)
async def update_profile_picture(
    *,
    session: Session = Depends(get_session),
    current_mechanic: Mechanic = Depends(get_current_active_mechanic),
    file: UploadFile = File(...),
):
    """
    Update the profile picture for the currently authenticated mechanic by uploading to Cloudflare R2.
    """
    # Upload to R2 and get the public URL, using "mechanic" as the folder name
    public_url = await upload_profile_picture_to_r2(
        file, "mechanic", str(current_mechanic.id)
    )

    # Save the R2 URL to the database
    current_mechanic.profile_picture_url = public_url
    session.add(current_mechanic)
    session.commit()
    session.refresh(current_mechanic)

    cache.cache_delete(_mechanic_me_key(current_mechanic))
    return current_mechanic


@router.get("/{mechanic_id}", response_model=MechanicPublic)
def read_mechanic(
    mechanic_id: str,
    session: Session = Depends(get_session),
):
    mechanic = get_by_reference(session, Mechanic, mechanic_id)
    if not mechanic:
        raise HTTPException(status_code=404, detail="Mechanic not found")

    trip_count = session.exec(
        select(func.count(MechanicTrip.id)).where(
            MechanicTrip.mechanic_id == mechanic.id
        )
    ).one()

    return MechanicPublic(**mechanic.model_dump(), total_trips=trip_count)


@router.post("/me/documents")
async def upload_verification_document(
    *,
    session: Session = Depends(get_session),
    current_mechanic: Mechanic = Depends(get_current_active_mechanic),
    file: UploadFile = File(...),
):
    """
    Upload KYC documents, licenses, or garage photos for admin approval.
    """
    # Reuse your R2 storage utility, but change the prefix folder
    public_url = await upload_document_to_r2(
        file, "kyc_documents", str(current_mechanic.id)
    )

    # Append the new document URL to the JSON array safely
    current_docs = current_mechanic.verification_documents or []

    # Create a new list to trigger SQLAlchemy's JSON mutation detection
    current_mechanic.verification_documents = [*current_docs, public_url]

    if current_mechanic.status == "rejected":
        current_mechanic.status = "pending_approval"

    session.add(current_mechanic)
    session.commit()
    session.refresh(current_mechanic)

    cache.cache_delete(_mechanic_me_key(current_mechanic))
    return {
        "message": "Document uploaded successfully",
        "documents": current_mechanic.verification_documents,
    }
