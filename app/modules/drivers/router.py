import json
import os
import shutil
import time
from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile
from fastapi.encoders import jsonable_encoder
from sqlmodel import Session, select, func, desc
from typing import List
import redis

from app.core.database import get_session, get_redis
from app.core.models import (
    Driver,
    DriverUpdate,
    DriverPublic,
    DriverPrivate,
    DriverReview,
    Trip,
)
from app.core.security import get_current_active_driver

router = APIRouter(prefix="/drivers", tags=["Drivers"])

os.makedirs("media/profile_pictures", exist_ok=True)


@router.get("/me", response_model=DriverPrivate)
def read_current_driver_profile(
    current_driver: Driver = Depends(get_current_active_driver),
):
    """
    Get the full profile for the currently authenticated driver.
    """
    return current_driver


@router.patch("/me", response_model=DriverPrivate)
def update_current_driver_profile(
    *,
    session: Session = Depends(get_session),
    current_driver: Driver = Depends(get_current_active_driver),
    driver_update: DriverUpdate,
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Update the profile for the currently authenticated driver.
    """
    update_data = driver_update.model_dump(exclude_unset=True)

    for key, value in update_data.items():
        setattr(current_driver, key, value)

    session.add(current_driver)
    session.commit()
    session.refresh(current_driver)

    # Invalidate cache
    if redis_client:
        redis_client.delete("drivers")
        redis_client.delete(f"driver_{current_driver.id}")

    return current_driver


@router.put("/me/profile-picture", response_model=DriverPrivate)
def update_driver_profile_picture(
    *,
    session: Session = Depends(get_session),
    current_driver: Driver = Depends(get_current_active_driver),
    file: UploadFile = File(...),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Update the profile picture for the currently authenticated driver.
    """
    timestamp = int(time.time())
    file_extension = os.path.splitext(file.filename)[1]
    file_path = (
        f"media/profile_pictures/driver_{current_driver.id}_{timestamp}{file_extension}"
    )

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    current_driver.profile_picture_url = f"/{file_path}"
    session.add(current_driver)
    session.commit()
    session.refresh(current_driver)

    # Invalidate cache
    if redis_client:
        redis_client.delete("drivers")
        redis_client.delete(f"driver_{current_driver.id}")

    return current_driver


@router.get("/", response_model=List[DriverPublic])
def read_drivers(
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Get a list of all drivers with their public profiles.
    """
    if redis_client:
        cached_drivers = redis_client.get("drivers")
        if cached_drivers:
            return json.loads(cached_drivers)

    drivers = session.exec(select(Driver)).all()
    public_drivers = []
    for driver in drivers:
        trip_count = session.exec(
            select(func.count(Trip.id)).where(Trip.driver_id == driver.id)
        ).one()
        public_drivers.append(
            DriverPublic(**driver.model_dump(), total_trips=trip_count)
        )

    if redis_client:
        # Correctly serialize the list of Pydantic models
        redis_client.set(
            "drivers", json.dumps(jsonable_encoder(public_drivers)), ex=3600
        )

    return public_drivers


@router.get("/{driver_id}", response_model=DriverPublic)
def read_driver(
    driver_id: int,
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Get a single driver's public profile.
    """
    if redis_client:
        cached_driver = redis_client.get(f"driver_{driver_id}")
        if cached_driver:
            return json.loads(cached_driver)

    driver = session.get(Driver, driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    trip_count = session.exec(
        select(func.count(Trip.id)).where(Trip.driver_id == driver_id)
    ).one()

    public_driver = DriverPublic(**driver.model_dump(), total_trips=trip_count)

    if redis_client:
        redis_client.set(
            f"driver_{driver_id}", public_driver.model_dump_json(), ex=3600
        )

    return public_driver


@router.get("/{driver_id}/reviews", response_model=List[DriverReview])
def get_driver_reviews(
    driver_id: int,
    session: Session = Depends(get_session),
    page: int = Query(1, gt=0),
    limit: int = Query(5, gt=0, le=50),
):
    """
    Get reviews for a specific driver with pagination.
    """
    driver = session.get(Driver, driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    offset = (page - 1) * limit
    reviews = session.exec(
        select(DriverReview)
        .where(DriverReview.driver_id == driver_id)
        .order_by(desc(DriverReview.created_at))
        .offset(offset)
        .limit(limit)
    ).all()
    return reviews
