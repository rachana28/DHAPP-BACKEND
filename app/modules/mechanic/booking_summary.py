"""User-facing summary builder for mechanic bookings.

Mirror of ``TripService.get_trip_summary`` for the per-table mechanic bookings,
so the user app's Mechanic status screen has a single rich endpoint. Exposes
ONLY non-sensitive data — reference IDs, statuses, locations, fares, and a
public provider block. Never user UUIDs, integer primary keys, or phone numbers.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlmodel import Session, func, select

from app.core.models import Mechanic, MechanicTrip
from app.modules.bookings.summary_helpers import (
    CANCELLABLE_STATES,
    address_edit_window,
    amount_to_collect,
    otp_view,
    payment_view,
    provider_actions,
    provider_user_block,
)
from app.modules.dispatch import geo
from app.workers.topics import telemetry_topic

_MECHANIC_LIVE_STATES = ("accepted", "arrived", "in_progress")


def _mechanic_block(session: Session, mech: Mechanic) -> Dict[str, Any]:
    total_trips = session.exec(
        select(func.count(MechanicTrip.id)).where(MechanicTrip.mechanic_id == mech.id)
    ).one()
    return {
        "id": mech.reference_id,
        "name": mech.name,
        "rating": mech.rating,
        "profile_picture_url": mech.profile_picture_url,
        "status": mech.status,
        "specialization": mech.specialization,
        "total_trips": total_trips,
    }


def build_mechanic_summary(
    session: Session, trip: MechanicTrip, viewer: str = "user"
) -> Dict[str, Any]:
    """Mechanic booking summary.

    ``viewer="user"`` (default) returns the rich customer-facing payload — kept
    as before. ``viewer="provider"`` returns the mechanic-app shape: a sanitized
    customer block (name + avatar, no phone), an ``actions`` block,
    ``amount_to_collect`` and ``otp_pending`` (no OTP code), and no
    ``nearby_providers``.
    """
    editable, deadline = address_edit_window(session, trip)
    result: Dict[str, Any] = {
        "trip_id": trip.reference_id,
        "service_type": "Mechanic Service",
        "status": trip.status,
        "vehicle_type": trip.vehicle_type,
        "start_location": trip.start_location,
        "start_lat": trip.start_lat,
        "start_lng": trip.start_lng,
        "reason": trip.reason,
        "fare": trip.fare,
        "fare_breakdown": trip.fare_breakdown,
        "booking_time": trip.booking_time,
        "actual_start_time": trip.actual_start_time,
        "actual_end_time": trip.actual_end_time,
        "payment_due_at": trip.payment_due_at,
    }

    if viewer == "provider":
        result["otp_pending"] = otp_view(
            session, "mechanic", trip, include_code=False
        ).get("otp_pending", False)
        result["amount_to_collect"] = amount_to_collect(session, "mechanic", trip)
        result["actions"] = provider_actions(trip, "mechanic")
        result["customer"] = provider_user_block(session, trip.user_id)
        if trip.status in _MECHANIC_LIVE_STATES:
            result["telemetry_topic"] = telemetry_topic(trip.reference_id)
        return result

    # --- user view (unchanged) ---
    result["address_editable"] = editable
    result["address_edit_deadline"] = deadline
    result["cancellable"] = trip.status in CANCELLABLE_STATES
    result.update(payment_view(session, "mechanic", trip))
    result.update(otp_view(session, "mechanic", trip))

    if trip.mechanic_id:
        mech = session.get(Mechanic, trip.mechanic_id)
        if mech:
            result["mechanic"] = _mechanic_block(session, mech)
            if trip.status in _MECHANIC_LIVE_STATES:
                result["telemetry_topic"] = telemetry_topic(trip.reference_id)
    elif trip.status == "searching":
        result["nearby_providers"] = geo.nearby_provider_locations(
            session, trip.start_lat, trip.start_lng, kind="mechanic"
        )
    return result
