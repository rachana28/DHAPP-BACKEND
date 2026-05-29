from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile
from sqlmodel import Session, select, func, desc
from typing import List
import redis

from app.core.database import get_session, get_redis
from app.core.models import (
    Mechanic,
    MechanicUpdate,
    MechanicPublic,
    MechanicPrivate,
    MechanicReview,
    MechanicTrip,
)
from app.core.security import get_current_active_mechanic
from app.utils.storage import upload_profile_picture_to_r2, upload_document_to_r2
from app.utils.id_generator import get_by_reference

router = APIRouter(prefix="/mechanics", tags=["Mechanics"])


@router.get("/me", response_model=MechanicPrivate)
def read_current_mechanic_profile(
    current_mechanic: Mechanic = Depends(get_current_active_mechanic),
):
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


@router.get("/{mechanic_id}/reviews", response_model=List[MechanicReview])
def get_mechanic_reviews(
    mechanic_id: str,
    session: Session = Depends(get_session),
    page: int = Query(1, gt=0),
    limit: int = Query(5, gt=0, le=50),
):
    mechanic = get_by_reference(session, Mechanic, mechanic_id)
    if not mechanic:
        raise HTTPException(status_code=404, detail="Mechanic not found")

    offset = (page - 1) * limit
    reviews = session.exec(
        select(MechanicReview)
        .where(MechanicReview.mechanic_id == mechanic.id)
        .order_by(desc(MechanicReview.created_at))
        .offset(offset)
        .limit(limit)
    ).all()
    return reviews


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

    return {
        "message": "Document uploaded successfully",
        "documents": current_mechanic.verification_documents,
    }
