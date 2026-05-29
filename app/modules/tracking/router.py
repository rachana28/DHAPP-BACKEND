from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select
from app.core.database import get_redis, get_session
from app.core.models import (
    LocationUpdate,
    User,
    TowTrip,
    MechanicTrip,
    TowTruckDriver,
    Mechanic,
)
from app.core.security import get_current_user
from app.utils.id_generator import get_by_reference
from sqlalchemy.orm import selectinload
import redis
import json
import asyncio

router = APIRouter(prefix="/tracking", tags=["Live Tracking"])


def _lookup_trip_owner(session: Session, trip_id: int):
    """
    Resolve a tracking trip_id to the assigned professional's user_id and the
    trip's kind ("tow" or "mechanic"). Returns (kind, user_id) or (None, None)
    if no trip / no professional yet.

    Trip ids are now per-table. After the backfill migration, existing ids are
    unique across tables (preserved from the old polymorphic trip); future ids
    use independent sequences and could in principle collide. We probe TowTrip
    first, then MechanicTrip — and the Redis cache key is namespaced by kind
    so writes from the two flows can't clobber each other.
    """
    tow_trip = session.exec(
        select(TowTrip)
        .where(TowTrip.reference_id == trip_id)
        .options(selectinload(TowTrip.tow_truck_driver))
    ).first()
    if tow_trip and tow_trip.tow_truck_driver_id and tow_trip.tow_truck_driver:
        return "tow", tow_trip.tow_truck_driver.user_id

    mech_trip = session.exec(
        select(MechanicTrip)
        .where(MechanicTrip.reference_id == trip_id)
        .options(selectinload(MechanicTrip.mechanic))
    ).first()
    if mech_trip and mech_trip.mechanic_id and mech_trip.mechanic:
        return "mechanic", mech_trip.mechanic.user_id

    return None, None


@router.post("/update")
def update_location(
    location: LocationUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Updates location ONLY for Tow Truck Drivers and Mechanics, and ONLY if there is an active trip.
    """
    # 1. Allow both tow truck drivers and mechanics
    if current_user.role not in ["tow_truck_driver", "mechanic"]:
        raise HTTPException(403, "Tracking is only enabled for Service Professionals.")

    if not location.trip_id:
        raise HTTPException(400, "Active trip ID is required for location updates.")

    # 2. Role-specific lookup — each kind lives in its own table now
    if current_user.role == "tow_truck_driver":
        driver = session.exec(
            select(TowTruckDriver).where(TowTruckDriver.user_id == current_user.id)
        ).first()
        if not driver:
            raise HTTPException(404, "Tow Driver profile not found.")

        trip = get_by_reference(session, TowTrip, location.trip_id)
        if not trip:
            raise HTTPException(404, "Trip not found.")
        if trip.tow_truck_driver_id != driver.id:
            raise HTTPException(
                403, "You are not authorized to update location for this trip."
            )

    else:  # mechanic
        mechanic = session.exec(
            select(Mechanic).where(Mechanic.user_id == current_user.id)
        ).first()
        if not mechanic:
            raise HTTPException(404, "Mechanic profile not found.")

        trip = get_by_reference(session, MechanicTrip, location.trip_id)
        if not trip:
            raise HTTPException(404, "Trip not found.")
        if trip.mechanic_id != mechanic.id:
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

    # Kind-namespaced trip cache key prevents collisions when a tow trip and a
    # mechanic trip happen to share the same numeric id (table sequences are
    # independent post-split).
    kind = "tow" if current_user.role == "tow_truck_driver" else "mechanic"

    if redis_client:
        redis_client.set(f"loc:{current_user.id}", json.dumps(data), ex=300)
        redis_client.set(
            f"loc:trip:{kind}:{location.trip_id}", json.dumps(data), ex=300
        )

    return {"status": "ok"}


@router.get("/{trip_id}")
def get_trip_location(
    trip_id: str,
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
):
    """Fallback HTTP endpoint for getting current location."""
    # Resolve which trip table this id belongs to first; this also tells us
    # which kind-namespaced Redis key to read.
    kind, target_user_id = _lookup_trip_owner(session, trip_id)

    if redis_client and kind:
        direct = redis_client.get(f"loc:trip:{kind}:{trip_id}")
        if direct:
            return json.loads(direct)

    if not target_user_id:
        return {
            "status": "waiting_for_professional",
            "detail": "No professional assigned yet",
        }

    if redis_client:
        data = redis_client.get(f"loc:{target_user_id}")
        if data:
            return json.loads(data)

    return {"status": "no_location_data"}


@router.websocket("/ws/{trip_id}")
async def tracking_websocket(
    websocket: WebSocket,
    trip_id: str,
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
        # Resolve the trip kind once outside the loop so we read the correct
        # kind-namespaced Redis key on every poll.
        kind, target_user_id = _lookup_trip_owner(session, trip_id)
        while True:
            data = None
            if redis_client:
                if kind:
                    direct = redis_client.get(f"loc:trip:{kind}:{trip_id}")
                    if direct:
                        data = direct
                if data is None and not kind:
                    # Trip not yet assigned at WS-open time; re-probe each poll
                    # in case the assignment lands mid-session.
                    kind, target_user_id = _lookup_trip_owner(session, trip_id)
                if data is None and target_user_id:
                    data = redis_client.get(f"loc:{target_user_id}")

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
