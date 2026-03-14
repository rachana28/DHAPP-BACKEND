from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select
from app.core.database import get_redis, get_session
from app.core.models import LocationUpdate, User, Trip, TowTruckDriver
from app.core.security import get_current_user
from sqlalchemy.orm import selectinload
import redis
import json
import asyncio

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
    if current_user.role != "tow_truck_driver":
        raise HTTPException(403, "Tracking is only enabled for Tow Truck Drivers.")

    if not location.trip_id:
        raise HTTPException(400, "Active trip ID is required for location updates.")

    driver = session.exec(
        select(TowTruckDriver).where(TowTruckDriver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(404, "Tow Driver profile not found.")

    trip = session.get(Trip, location.trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found.")

    if trip.tow_truck_driver_id != driver.id:
        raise HTTPException(
            403, "You are not authorized to update location for this trip."
        )

    if trip.status not in ["accepted", "in_progress", "arrived"]:
        raise HTTPException(400, "Tracking is not allowed for inactive trips.")

    data = {
        "lat": location.latitude,
        "lng": location.longitude,
        "heading": location.heading,
        "speed": location.speed,
        "role": current_user.role,
        "user_id": str(current_user.id),
        "trip_id": location.trip_id,
        "updated_at": "now",
    }

    if redis_client:
        redis_client.set(f"loc:{current_user.id}", json.dumps(data), ex=300)
        redis_client.set(f"loc:trip:{location.trip_id}", json.dumps(data), ex=300)

    return {"status": "ok"}


@router.get("/{trip_id}")
def get_trip_location(
    trip_id: int,
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
):
    """Fallback HTTP endpoint for getting current location"""
    if redis_client:
        direct_trip_data = redis_client.get(f"loc:trip:{trip_id}")
        if direct_trip_data:
            return json.loads(direct_trip_data)

    statement = (
        select(Trip)
        .where(Trip.id == trip_id)
        .options(selectinload(Trip.tow_truck_driver))
    )
    trip = session.exec(statement).first()

    if not trip:
        raise HTTPException(404, "Trip not found")

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


@router.websocket("/ws/{trip_id}")
async def tracking_websocket(
    websocket: WebSocket,
    trip_id: int,
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    WebSocket Endpoint for Industry-Standard Real-Time Live Tracking.
    Pushes location data strictly when it updates.
    """
    await websocket.accept()

    try:
        last_data = None
        while True:
            data = None
            if redis_client:
                # Try getting the cached location directly
                direct_trip_data = redis_client.get(f"loc:trip:{trip_id}")
                if direct_trip_data:
                    data = direct_trip_data
                else:
                    # Fallback to fetching trip -> driver -> cached location
                    statement = (
                        select(Trip)
                        .where(Trip.id == trip_id)
                        .options(selectinload(Trip.tow_truck_driver))
                    )
                    trip = session.exec(statement).first()
                    if trip and trip.tow_truck_driver_id and trip.tow_truck_driver:
                        data = redis_client.get(f"loc:{trip.tow_truck_driver.user_id}")

            if data:
                # Decode bytes if needed
                data_str = data.decode("utf-8") if isinstance(data, bytes) else data
                # Only push if location has changed (saves bandwidth + routing recalculations)
                if data_str != last_data:
                    await websocket.send_text(data_str)
                    last_data = data_str

            # Poll frequency control
            await asyncio.sleep(2)

    except WebSocketDisconnect:
        pass
    except Exception:
        await websocket.close()
