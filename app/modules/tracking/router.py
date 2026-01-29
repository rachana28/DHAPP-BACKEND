from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from app.core.database import get_redis, get_session
from app.core.models import LocationUpdate, User, Trip, TowTruckDriver
from app.core.security import get_current_user
from sqlalchemy.orm import selectinload
import redis
import json

router = APIRouter(prefix="/tracking", tags=["Live Tracking"])


@router.post("/update")
def update_location(
    location: LocationUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Updates location ONLY for Tow Truck Drivers and ONLY if there is an active trip.
    """
    # 1. Strict Role Check: Only Tow Truck Drivers allowed
    if current_user.role != "tow_truck_driver":
        raise HTTPException(403, "Tracking is only enabled for Tow Truck Drivers.")

    # 2. Validate Trip Context
    if not location.trip_id:
        raise HTTPException(400, "Active trip ID is required for location updates.")

    # 3. Verify Trip Ownership & Status in DB
    # We need to find the driver profile first to check ownership
    driver = session.exec(
        select(TowTruckDriver).where(TowTruckDriver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(404, "Tow Driver profile not found.")

    trip = session.get(Trip, location.trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found.")

    # Check if this driver owns the trip
    if trip.tow_truck_driver_id != driver.id:
        raise HTTPException(
            403, "You are not authorized to update location for this trip."
        )

    # Check if trip is strictly active (accepted or in_progress)
    if trip.status not in ["accepted", "in_progress", "arrived"]:
        raise HTTPException(400, "Tracking is not allowed for inactive trips.")

    # 4. Store Data in Redis
    data = {
        "lat": location.latitude,
        "lng": location.longitude,
        "heading": location.heading,
        "speed": location.speed,
        "role": current_user.role,
        "user_id": current_user.id,
        "trip_id": location.trip_id,
        "updated_at": "now",
    }

    if redis_client:
        # Store by User ID
        redis_client.set(f"loc:{current_user.id}", json.dumps(data), ex=300)
        # Store by Trip ID (Optimization for frontend lookups)
        redis_client.set(f"loc:trip:{location.trip_id}", json.dumps(data), ex=300)

    return {"status": "ok"}


@router.get("/{trip_id}")
def get_trip_location(
    trip_id: int,
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
):
    # Try direct trip location lookup first
    if redis_client:
        direct_trip_data = redis_client.get(f"loc:trip:{trip_id}")
        if direct_trip_data:
            return json.loads(direct_trip_data)

    # Fallback: DB Lookup
    statement = (
        select(Trip)
        .where(Trip.id == trip_id)
        .options(selectinload(Trip.tow_truck_driver))
    )
    trip = session.exec(statement).first()

    if not trip:
        raise HTTPException(404, "Trip not found")

    # Only track Tow Drivers (as per requirement)
    target_user_id = None
    if trip.tow_truck_driver_id and trip.tow_truck_driver:
        target_user_id = trip.tow_truck_driver.user_id

    if not target_user_id:
        return {"status": "waiting_for_driver", "detail": "No tow driver assigned yet"}

    if redis_client:
        data = redis_client.get(f"loc:{target_user_id}")
        if data:
            return json.loads(data)

    return {"status": "no_location_data"}
