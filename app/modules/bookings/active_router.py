"""User-app aggregate active-bookings endpoint.

`GET /bookings/active` returns the customer's currently-active bookings across
**all four services** (trips, tow, mechanic, service) in a single call, each
section shaped **identically to that service's `my-bookings`** response. This is
the polling target for the user-app home screen — it replaces the old pattern of
pulling every `my-bookings` history page and filtering "active" on the client.

Optimised for repeated polling:
- 4 indexed queries (`user_id` + `status IN (active...)`), each with the provider
  eager-loaded (no N+1).
- The assembled JSON is wrapped in a very short-TTL Redis cache so a burst of
  polls from one device collapses to ≤1 DB sweep per `ACTIVE_CACHE_TTL` window.

Privacy: provider blocks use the existing `*Public` models (no provider phone /
address), and trips hide the driver until past the searching/payment phase — so
no sensitive cross-party data is exposed.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import selectinload
from sqlmodel import Session, desc, select

from app.core import cache
from app.core.booking_states import (
    MECHANIC_ACTIVE_STATES,
    SERVICE_ACTIVE_STATES,
    TOW_ACTIVE_STATES,
    TRIP_ACTIVE_STATES,
)
from app.core.database import get_session
from app.core.models import (
    MechanicTrip,
    MechanicTripReadUser,
    ServiceRequest,
    ServiceRequestPublic,
    TowTrip,
    TowTripReadUser,
    Trip,
    TripReadUser,
    User,
)
from app.core.security import get_current_user
from app.modules.trips.trip_service import TripService

router = APIRouter(prefix="/bookings", tags=["Bookings"])

# Mirrors the masking used by GET /trips/my-bookings: the driver block is hidden
# while a ride is still pre-assignment / mid-payment.
_DRIVER_HIDDEN_STATES = {
    "searching",
    "no_drivers_found",
    "accepted_pending_payment",
    "payment_in_progress",
    "payment_failed",
    "rejected",
}


def _trips_section(session: Session, user_id) -> List[Dict[str, Any]]:
    rows = session.exec(
        select(Trip)
        .where(Trip.user_id == user_id, Trip.status.in_(TRIP_ACTIVE_STATES))
        .order_by(desc(Trip.booking_time))
        .options(selectinload(Trip.driver))
    ).all()
    trip_service = TripService()
    out: List[Dict[str, Any]] = []
    for t in rows:
        view = TripReadUser.model_validate(t, from_attributes=True)
        if t.status in _DRIVER_HIDDEN_STATES:
            view.driver = None
        elif t.driver_id is not None:
            view.driver_skips_remaining = trip_service.driver_skips_remaining(
                session, t.id
            )
        out.append(view.model_dump(mode="json"))
    return out


def _tow_section(session: Session, user_id) -> List[Dict[str, Any]]:
    rows = session.exec(
        select(TowTrip)
        .where(TowTrip.user_id == user_id, TowTrip.status.in_(TOW_ACTIVE_STATES))
        .order_by(desc(TowTrip.booking_time))
        .options(selectinload(TowTrip.tow_truck_driver))
    ).all()
    return [
        TowTripReadUser.model_validate(t, from_attributes=True).model_dump(mode="json")
        for t in rows
    ]


def _mechanic_section(session: Session, user_id) -> List[Dict[str, Any]]:
    rows = session.exec(
        select(MechanicTrip)
        .where(
            MechanicTrip.user_id == user_id,
            MechanicTrip.status.in_(MECHANIC_ACTIVE_STATES),
        )
        .order_by(desc(MechanicTrip.booking_time))
        .options(selectinload(MechanicTrip.mechanic))
    ).all()
    return [
        MechanicTripReadUser.model_validate(t, from_attributes=True).model_dump(
            mode="json"
        )
        for t in rows
    ]


def _service_section(session: Session, user_id) -> List[Dict[str, Any]]:
    rows = session.exec(
        select(ServiceRequest)
        .where(
            ServiceRequest.user_id == user_id,
            ServiceRequest.status.in_(SERVICE_ACTIVE_STATES),
        )
        .order_by(desc(ServiceRequest.booking_time))
    ).all()
    return [
        ServiceRequestPublic.model_validate(s, from_attributes=True).model_dump(
            mode="json"
        )
        for s in rows
    ]


@router.get("/active")
def get_active_bookings(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> Dict[str, List[Dict[str, Any]]]:
    """All active (non-terminal) bookings for the logged-in customer, grouped by
    service. User app only."""
    if current_user.role != "user":
        raise HTTPException(
            status_code=403, detail="Only customers can fetch aggregate bookings."
        )

    key = cache.active_key("user", current_user.id)
    cached = cache.cache_get_json(key)
    if cached is not None:
        return cached

    result = {
        "trips": _trips_section(session, current_user.id),
        "tow": _tow_section(session, current_user.id),
        "mechanic": _mechanic_section(session, current_user.id),
        "service": _service_section(session, current_user.id),
    }
    cache.cache_set_json(key, result, cache.ACTIVE_CACHE_TTL)
    return result
