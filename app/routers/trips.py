import redis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select
from typing import List

from app.database import get_session, get_redis
from app.models import Trip, TripCreate, TripUpdate, User, Driver
from app.security import get_current_user

router = APIRouter(prefix="/trips", tags=["Trips"])


@router.post("/", response_model=Trip)
def create_trip(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    trip_in: TripCreate,
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Create a new trip (booking request).
    """
    if current_user.id != trip_in.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only create trips for yourself.",
        )

    # Verify driver exists
    driver = session.get(Driver, trip_in.driver_id)
    if not driver:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Driver with id {trip_in.driver_id} not found.",
        )

    db_trip = Trip.model_validate(trip_in)
    session.add(db_trip)
    session.commit()
    session.refresh(db_trip)

    # Invalidate cache
    if redis_client:
        redis_client.delete("drivers")
        redis_client.delete(f"driver_{trip_in.driver_id}")

    return db_trip


@router.get("/my-bookings", response_model=List[Trip])
def get_my_bookings_as_driver(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Get all trip requests for the currently logged-in driver.
    """
    if current_user.role != "driver":
        raise HTTPException(status_code=403, detail="Not authorized")

    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()

    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    trips = session.exec(select(Trip).where(Trip.driver_id == driver.id)).all()
    return trips


@router.patch("/{trip_id}", response_model=Trip)
def update_trip_status(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    trip_id: int,
    trip_update: TripUpdate,
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Update trip status (e.g., accept, cancel).
    Accessible by either the user who booked or the driver.
    """
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    # Check if the current user is the driver for the trip
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()

    is_driver_of_trip = driver and driver.id == trip.driver_id
    is_user_of_trip = current_user.id == trip.user_id

    if not is_driver_of_trip and not is_user_of_trip:
        raise HTTPException(
            status_code=403, detail="Not authorized to update this trip"
        )

    update_data = trip_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(trip, key, value)

    session.add(trip)
    session.commit()
    session.refresh(trip)

    # Invalidate cache
    if redis_client:
        redis_client.delete("drivers")
        redis_client.delete(f"driver_{trip.driver_id}")

    return trip
