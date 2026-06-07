from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile
from sqlmodel import Session, select, func, desc
from typing import List
import redis

from app.core import cache
from app.core.database import get_session, get_redis
from app.core.models import (
    AvailabilityUpdate,
    TowTruckDriver,
    TowTruckDriverUpdate,
    TowTruckDriverPublic,
    TowTruckDriverPrivate,
    TowTruckDriverReview,
    TowTrip,
)
from app.core.security import get_current_active_tow_truck_driver
from app.utils.storage import upload_profile_picture_to_r2, upload_document_to_r2
from app.utils.id_generator import get_by_reference

router = APIRouter(prefix="/tow-truck-drivers", tags=["Tow Truck Drivers"])


def _tow_me_key(current_driver: TowTruckDriver) -> str:
    return cache.me_key("tow_driver", current_driver.user_id)


@router.get("/me", response_model=TowTruckDriverPrivate)
def read_current_tow_driver_profile(
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
):
    key = _tow_me_key(current_driver)
    cached = cache.cache_get_json(key)
    if cached is not None:
        return cached
    data = TowTruckDriverPrivate.model_validate(
        current_driver, from_attributes=True
    ).model_dump(mode="json")
    cache.cache_set_json(key, data, cache.ME_CACHE_TTL)
    return data


@router.patch("/me/availability", response_model=TowTruckDriverPrivate)
def set_tow_driver_availability(
    *,
    session: Session = Depends(get_session),
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
    body: AvailabilityUpdate,
):
    """Online/offline toggle. When offline the driver is excluded from geo
    dispatch (alongside the admin `status` check) without touching that status."""
    current_driver.is_online = body.is_online
    session.add(current_driver)
    session.commit()
    session.refresh(current_driver)
    cache.cache_delete(_tow_me_key(current_driver))
    return current_driver


@router.patch("/me", response_model=TowTruckDriverPrivate)
def update_current_tow_driver_profile(
    *,
    session: Session = Depends(get_session),
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
    driver_update: TowTruckDriverUpdate,
    redis_client: redis.Redis = Depends(get_redis),
):
    update_data = driver_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(current_driver, key, value)

    if current_driver.status == "rejected":
        current_driver.status = "pending_approval"

    session.add(current_driver)
    session.commit()
    session.refresh(current_driver)
    cache.cache_delete(_tow_me_key(current_driver))
    return current_driver


@router.put("/me/profile-picture", response_model=TowTruckDriverPrivate)
async def update_profile_picture(
    *,
    session: Session = Depends(get_session),
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
    file: UploadFile = File(...),
):
    """
    Update the profile picture for the currently authenticated user by uploading to Cloudflare R2.
    """
    # Upload to R2 and get the public URL
    public_url = await upload_profile_picture_to_r2(
        file, "tow_truck_driver", str(current_driver.id)
    )

    # Save the R2 URL to the database
    current_driver.profile_picture_url = public_url
    session.add(current_driver)
    session.commit()
    session.refresh(current_driver)

    cache.cache_delete(_tow_me_key(current_driver))
    return current_driver


@router.get("/{driver_id}", response_model=TowTruckDriverPublic)
def read_tow_driver(
    driver_id: str,
    session: Session = Depends(get_session),
):
    driver = get_by_reference(session, TowTruckDriver, driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    trip_count = session.exec(
        select(func.count(TowTrip.id)).where(TowTrip.tow_truck_driver_id == driver.id)
    ).one()

    return TowTruckDriverPublic(**driver.model_dump(), total_trips=trip_count)


@router.get("/{driver_id}/reviews", response_model=List[TowTruckDriverReview])
def get_tow_driver_reviews(
    driver_id: str,
    session: Session = Depends(get_session),
    page: int = Query(1, gt=0),
    limit: int = Query(5, gt=0, le=50),
):
    driver = get_by_reference(session, TowTruckDriver, driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    offset = (page - 1) * limit
    reviews = session.exec(
        select(TowTruckDriverReview)
        .where(TowTruckDriverReview.driver_id == driver.id)
        .order_by(desc(TowTruckDriverReview.created_at))
        .offset(offset)
        .limit(limit)
    ).all()
    return reviews


@router.post("/me/documents")
async def upload_verification_document(
    *,
    session: Session = Depends(get_session),
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
    file: UploadFile = File(...),
):
    """
    Upload KYC documents, licenses, or garage photos for admin approval.
    """
    # Reuse your R2 storage utility, but change the prefix folder
    public_url = await upload_document_to_r2(
        file, "kyc_documents", str(current_driver.id)
    )

    # Append the new document URL to the JSON array safely
    current_docs = current_driver.verification_documents or []

    # Create a new list to trigger SQLAlchemy's JSON mutation detection
    current_driver.verification_documents = [*current_docs, public_url]

    if current_driver.status == "rejected":
        current_driver.status = "pending_approval"

    session.add(current_driver)
    session.commit()
    session.refresh(current_driver)

    cache.cache_delete(_tow_me_key(current_driver))
    return {
        "message": "Document uploaded successfully",
        "documents": current_driver.verification_documents,
    }
