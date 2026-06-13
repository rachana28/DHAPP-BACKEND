"""User-facing summary builder for tow bookings.

Mirror of ``TripService.get_trip_summary`` for the per-table tow bookings, so
the user app's Tow status screen has a single rich endpoint. Exposes ONLY
non-sensitive data — reference IDs, statuses, locations, fares, and a public
provider block. Never user UUIDs, integer primary keys, or phone numbers.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlmodel import Session, func, select

from app.core.models import TowTrip, TowTruckDriver
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

_TOW_LIVE_STATES = ("accepted", "arrived", "in_progress", "near_destination")


def _tow_driver_block(session: Session, driver: TowTruckDriver) -> Dict[str, Any]:
    total_trips = session.exec(
        select(func.count(TowTrip.id)).where(TowTrip.tow_truck_driver_id == driver.id)
    ).one()
    return {
        "id": driver.reference_id,
        "name": driver.name,
        "rating": driver.rating,
        "profile_picture_url": driver.profile_picture_url,
        "status": driver.status,
        "tow_vehicle_type": driver.tow_vehicle_type,
        "vehicle_number": driver.vehicle_number,
        "total_trips": total_trips,
    }


def build_tow_summary(
    session: Session, trip: TowTrip, viewer: str = "user"
) -> Dict[str, Any]:
    """Tow/transport booking summary.

    ``viewer="user"`` (default) returns the rich customer-facing payload — kept
    byte-for-byte as before. ``viewer="provider"`` returns the driver-app shape:
    a sanitized customer block (name + avatar, no phone), an ``actions`` block,
    ``amount_to_collect`` and ``otp_pending`` (no OTP code), and no
    ``nearby_providers``.
    """
    # Transport reuses this builder; label the service for the app heading.
    service_label = (
        "Transport Service"
        if (trip.service_type or "tow") == "transport"
        else "Tow Service"
    )
    editable, deadline = address_edit_window(session, trip)
    result: Dict[str, Any] = {
        "trip_id": trip.reference_id,
        "service_type": service_label,
        "status": trip.status,
        "vehicle_type": trip.vehicle_type,
        "tow_vehicle_type": trip.tow_vehicle_type,
        "transport_vehicle_type": trip.transport_vehicle_type,
        "start_location": trip.start_location,
        "start_lat": trip.start_lat,
        "start_lng": trip.start_lng,
        "end_location": trip.end_location,
        "end_lat": trip.end_lat,
        "end_lng": trip.end_lng,
        "distance_km": trip.distance_km,
        "reason": trip.reason,
        "fare": trip.fare,
        "fare_breakdown": trip.fare_breakdown,
        "booking_time": trip.booking_time,
        "actual_start_time": trip.actual_start_time,
        "actual_end_time": trip.actual_end_time,
        "payment_due_at": trip.payment_due_at,
    }

    if viewer == "provider":
        result["otp_pending"] = otp_view(session, "tow", trip, include_code=False).get(
            "otp_pending", False
        )
        result["amount_to_collect"] = amount_to_collect(session, "tow", trip)
        result["actions"] = provider_actions(trip, "tow")
        result["customer"] = provider_user_block(session, trip.user_id)
        if trip.status in _TOW_LIVE_STATES:
            result["telemetry_topic"] = telemetry_topic(trip.reference_id)
        return result

    # --- user view (unchanged) ---
    result["address_editable"] = editable
    result["address_edit_deadline"] = deadline
    result["cancellable"] = trip.status in CANCELLABLE_STATES
    result.update(payment_view(session, "tow", trip))
    result.update(otp_view(session, "tow", trip))

    if trip.tow_truck_driver_id:
        driver = session.get(TowTruckDriver, trip.tow_truck_driver_id)
        if driver:
            result["tow_truck_driver"] = _tow_driver_block(session, driver)
            if trip.status in _TOW_LIVE_STATES:
                result["telemetry_topic"] = telemetry_topic(trip.reference_id)
    elif trip.status == "searching":
        result["nearby_providers"] = geo.nearby_provider_locations(
            session,
            trip.start_lat,
            trip.start_lng,
            kind="transport" if (trip.service_type or "tow") == "transport" else "tow",
        )
    return result
