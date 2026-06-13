"""Driver profile API.

Self-service endpoints for the authenticated driver (profile read/update,
profile picture, labelled KYC documents, bank payout details, passbook) plus
the public driver directory (list, detail, reviews) consumed by the user app.
/me responses are short-TTL cached and invalidated on every write.
"""

import json
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from fastapi.encoders import jsonable_encoder
from sqlmodel import Session, select, func
from typing import List
import redis

from app.core import cache
from app.core.database import get_session, get_redis
from app.core.models import (
    Driver,
    DriverUpdate,
    DriverPublic,
    DriverPrivate,
    Trip,
    ProviderBankDetailsUpdate,
    PROVIDER_DOCUMENT_TYPES,
)
from app.core.security import get_current_active_driver
from app.utils.storage import upload_profile_picture_to_r2, upload_document_to_r2
from app.utils.id_generator import get_by_reference

router = APIRouter(prefix="/drivers", tags=["Drivers"])


def _driver_me_key(current_driver: Driver) -> str:
    return cache.me_key("driver", current_driver.user_id)


@router.get("/me", response_model=DriverPrivate)
def read_current_driver_profile(
    current_driver: Driver = Depends(get_current_active_driver),
):
    """Get the full profile for the currently authenticated driver."""
    key = _driver_me_key(current_driver)
    cached = cache.cache_get_json(key)
    if cached is not None:
        return cached
    data = DriverPrivate.model_validate(
        current_driver, from_attributes=True
    ).model_dump(mode="json")
    cache.cache_set_json(key, data, cache.ME_CACHE_TTL)
    return data


@router.patch("/me", response_model=DriverPrivate)
def update_current_driver_profile(
    *,
    session: Session = Depends(get_session),
    current_driver: Driver = Depends(get_current_active_driver),
    driver_update: DriverUpdate,
    redis_client: redis.Redis = Depends(get_redis),
):
    """Update the profile for the currently authenticated driver."""
    update_data = driver_update.model_dump(exclude_unset=True)

    for key, value in update_data.items():
        setattr(current_driver, key, value)

    if current_driver.status == "rejected":
        current_driver.status = "pending_approval"

    session.add(current_driver)
    session.commit()
    session.refresh(current_driver)

    if redis_client:
        redis_client.delete("drivers")
        redis_client.delete(f"driver_{current_driver.id}")
    cache.cache_delete(_driver_me_key(current_driver))

    return current_driver


@router.put("/me/profile-picture", response_model=DriverPrivate)
async def update_driver_profile_picture(
    *,
    session: Session = Depends(get_session),
    current_driver: Driver = Depends(get_current_active_driver),
    file: UploadFile = File(...),
):
    """Update the profile picture for the currently authenticated user by uploading to Cloudflare R2."""
    public_url = await upload_profile_picture_to_r2(
        file, "driver", str(current_driver.id)
    )

    current_driver.profile_picture_url = public_url
    session.add(current_driver)
    session.commit()
    session.refresh(current_driver)

    cache.cache_delete(_driver_me_key(current_driver))
    return current_driver


@router.get("/", response_model=List[DriverPublic])
def read_drivers(
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Get a list of all drivers with their public profiles."""
    if redis_client:
        cached_drivers = redis_client.get("drivers")
        if cached_drivers:
            return json.loads(cached_drivers)

    drivers = session.exec(select(Driver).where(Driver.status == "available")).all()
    public_drivers = []
    for driver in drivers:
        trip_count = session.exec(
            select(func.count(Trip.id)).where(Trip.driver_id == driver.id)
        ).one()
        public_drivers.append(
            DriverPublic(**driver.model_dump(), total_trips=trip_count)
        )

    if redis_client:
        redis_client.set(
            "drivers", json.dumps(jsonable_encoder(public_drivers)), ex=3600
        )

    return public_drivers


@router.get("/{driver_id}", response_model=DriverPublic)
def read_driver(
    driver_id: str,
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Get a single driver's public profile."""
    if redis_client:
        cached_driver = redis_client.get(f"driver_{driver_id}")
        if cached_driver:
            return json.loads(cached_driver)

    driver = get_by_reference(session, Driver, driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    trip_count = session.exec(
        select(func.count(Trip.id)).where(Trip.driver_id == driver.id)
    ).one()

    public_driver = DriverPublic(**driver.model_dump(), total_trips=trip_count)

    if redis_client:
        redis_client.set(
            f"driver_{driver_id}", public_driver.model_dump_json(), ex=3600
        )

    return public_driver


@router.post("/me/documents")
async def upload_verification_document(
    *,
    session: Session = Depends(get_session),
    current_driver: Driver = Depends(get_current_active_driver),
    doc_type: str = Form(...),
    file: UploadFile = File(...),
):
    """Upload a labelled KYC document (doc_type from the fixed checklist)."""
    doc_type = (doc_type or "").strip().lower()
    if doc_type not in PROVIDER_DOCUMENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"doc_type must be one of {list(PROVIDER_DOCUMENT_TYPES)}",
        )

    public_url = await upload_document_to_r2(
        file, f"kyc_documents/{doc_type}", str(current_driver.id)
    )

    current_docs = dict(current_driver.verification_documents or {})
    current_docs[doc_type] = public_url
    current_driver.verification_documents = current_docs

    if current_driver.status == "rejected":
        current_driver.status = "pending_approval"

    session.add(current_driver)
    session.commit()
    session.refresh(current_driver)

    cache.cache_delete(_driver_me_key(current_driver))
    return {
        "message": f"{doc_type} document uploaded successfully",
        "documents": current_driver.verification_documents,
    }


@router.get("/me/documents")
def list_verification_documents(
    current_driver: Driver = Depends(get_current_active_driver),
):
    """Return the labelled documents and a present/missing checklist."""
    docs = current_driver.verification_documents or {}
    return {
        "documents": docs,
        "checklist": {
            dt: (dt in docs and bool(docs[dt])) for dt in PROVIDER_DOCUMENT_TYPES
        },
        "missing": [dt for dt in PROVIDER_DOCUMENT_TYPES if not docs.get(dt)],
    }


@router.post("/me/bank-details", response_model=DriverPrivate)
def set_bank_details(
    *,
    session: Session = Depends(get_session),
    current_driver: Driver = Depends(get_current_active_driver),
    bank: ProviderBankDetailsUpdate,
):
    """Set/replace the driver's bank payout details (required for approval)."""
    current_driver.bank_name = bank.bank_name
    current_driver.bank_account_number = bank.bank_account_number
    current_driver.bank_ifsc = bank.bank_ifsc
    current_driver.bank_account_holder_name = bank.bank_account_holder_name
    if current_driver.status == "rejected":
        current_driver.status = "pending_approval"
    session.add(current_driver)
    session.commit()
    session.refresh(current_driver)
    cache.cache_delete(_driver_me_key(current_driver))
    return current_driver


@router.post("/me/passbook", response_model=DriverPrivate)
async def upload_passbook(
    *,
    session: Session = Depends(get_session),
    current_driver: Driver = Depends(get_current_active_driver),
    file: UploadFile = File(...),
):
    """Upload the bank passbook/cancelled-cheque document (required for approval)."""
    public_url = await upload_document_to_r2(
        file, "kyc_documents/passbook", str(current_driver.id)
    )
    current_driver.passbook_document_url = public_url
    if current_driver.status == "rejected":
        current_driver.status = "pending_approval"
    session.add(current_driver)
    session.commit()
    session.refresh(current_driver)
    cache.cache_delete(_driver_me_key(current_driver))
    return current_driver
