from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select, func, desc
from typing import List, Optional

from app.database import get_session
from app.models import (
    Driver,
    DriverUpdate,
    DriverPublic,
    DriverPrivate,
    DriverReview,
    Trip,
)
from app.security import get_current_active_driver

router = APIRouter(prefix="/drivers", tags=["Drivers"])


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
    return current_driver


@router.get("/", response_model=List[DriverPublic])
def read_drivers(session: Session = Depends(get_session)):
    """
    Get a list of all drivers with their public profiles.
    """
    drivers = session.exec(select(Driver)).all()
    public_drivers = []
    for driver in drivers:
        trip_count = session.exec(
            select(func.count(Trip.id)).where(Trip.driver_id == driver.id)
        ).one()
        public_drivers.append(
            DriverPublic(**driver.model_dump(), total_trips=trip_count)
        )
    return public_drivers


@router.get("/{driver_id}", response_model=DriverPublic)
def read_driver(driver_id: int, session: Session = Depends(get_session)):
    """
    Get a single driver's public profile.
    """
    driver = session.get(Driver, driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    trip_count = session.exec(
        select(func.count(Trip.id)).where(Trip.driver_id == driver_id)
    ).one()

    return DriverPublic(**driver.model_dump(), total_trips=trip_count)


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
