import os
import shutil
import time
from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile
from sqlmodel import Session, select, func, desc
from typing import List
import redis

from app.core.database import get_session, get_redis
from app.core.models import (
    TowTruckDriver,
    TowTruckDriverUpdate,
    TowTruckDriverPublic,
    TowTruckDriverPrivate,
    TowTruckDriverReview,
    Trip,
)
from app.core.security import get_current_active_tow_truck_driver

router = APIRouter(prefix="/tow-truck-drivers", tags=["Tow Truck Drivers"])

os.makedirs("media/profile_pictures", exist_ok=True)


@router.get("/me", response_model=TowTruckDriverPrivate)
def read_current_tow_driver_profile(
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
):
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

    session.add(current_driver)
    session.commit()
    session.refresh(current_driver)
    return current_driver


@router.put("/me/profile-picture", response_model=TowTruckDriverPrivate)
def update_profile_picture(
    *,
    session: Session = Depends(get_session),
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
    file: UploadFile = File(...),
):
    timestamp = int(time.time())
    file_extension = os.path.splitext(file.filename)[1]
    file_path = (
        f"media/profile_pictures/tow_{current_driver.id}_{timestamp}{file_extension}"
    )

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    current_driver.profile_picture_url = f"/{file_path}"
    session.add(current_driver)
    session.commit()
    session.refresh(current_driver)
    return current_driver


@router.get("/{driver_id}", response_model=TowTruckDriverPublic)
def read_tow_driver(
    driver_id: int,
    session: Session = Depends(get_session),
):
    driver = session.get(TowTruckDriver, driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    trip_count = session.exec(
        select(func.count(Trip.id)).where(Trip.tow_truck_driver_id == driver_id)
    ).one()

    return TowTruckDriverPublic(**driver.model_dump(), total_trips=trip_count)


@router.get("/{driver_id}/reviews", response_model=List[TowTruckDriverReview])
def get_tow_driver_reviews(
    driver_id: int,
    session: Session = Depends(get_session),
    page: int = Query(1, gt=0),
    limit: int = Query(5, gt=0, le=50),
):
    driver = session.get(TowTruckDriver, driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    offset = (page - 1) * limit
    reviews = session.exec(
        select(TowTruckDriverReview)
        .where(TowTruckDriverReview.driver_id == driver_id)
        .order_by(desc(TowTruckDriverReview.created_at))
        .offset(offset)
        .limit(limit)
    ).all()
    return reviews
