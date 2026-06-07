"""User-facing summary builders for tow & mechanic bookings.

Mirror of ``TripService.get_trip_summary`` for the per-table tow/mechanic
bookings, so the user app's Tow / Mechanic status screens have a single rich
endpoint (parity with ``GET /trips/{id}/summary``).

Safety: the returned dicts expose ONLY non-sensitive data — reference IDs,
statuses, locations, fares, and a public provider block. Never user UUIDs,
integer primary keys, driver/mechanic ids, or phone numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Tuple

from sqlmodel import Session, func, select

from app.core.models import (
    Mechanic,
    MechanicTrip,
    Payment,
    TowTrip,
    TowTruckDriver,
)
from app.modules.dispatch import geo
from app.modules.trips import booking_otp_service
from app.utils.time_utils import now_ist
from app.workers.topics import telemetry_topic

# Gross-paid states (matches TripService): refunds are tracked on the original
# charge's refunded_amount, so the row still counts toward total_paid.
_PAID_STATES = ["succeeded", "partially_refunded", "refunded"]

# Provider is assigned & engaged → expose the live telemetry topic for tracking.
_TOW_LIVE_STATES = ("accepted", "arrived", "in_progress", "near_destination")
_MECHANIC_LIVE_STATES = ("accepted", "arrived", "in_progress")

# Cancellable while still pre-pickup; blocked once the job is underway.
_CANCELLABLE_STATES = ("searching", "accepted", "arrived")


def address_edit_window(session: Session, trip) -> Tuple[bool, datetime]:
    """``(editable, deadline)`` for a tow/mechanic booking's address.

    Editable only while the booking hasn't started moving (``searching`` /
    ``accepted``) AND we're within the configured window measured from the
    ORIGINAL ``booking_time`` (never reset by an edit)."""
    window_min = geo.get_config_float(
        session, geo.ADDRESS_EDIT_WINDOW_MIN_KEY, geo.DEFAULT_ADDRESS_EDIT_WINDOW_MIN
    )
    deadline = trip.booking_time + timedelta(minutes=window_min)
    editable = trip.status in ("searching", "accepted") and now_ist() <= deadline
    return editable, deadline


def _payment_view(session: Session, service_type: str, trip) -> Dict[str, Any]:
    user_payments = session.exec(
        select(Payment).where(
            Payment.service_type == service_type,
            Payment.service_id == trip.id,
            Payment.payer_type == "user",
            Payment.status.in_(_PAID_STATES),
        )
    ).all()
    total_paid = round(sum(p.amount for p in user_payments), 2)
    total_refunded = round(sum(p.refunded_amount or 0.0 for p in user_payments), 2)
    fare = float(trip.fare or 0.0)
    net_paid = total_paid - total_refunded

    if trip.payment_status == "paid" or (trip.status or "").startswith("cancel"):
        amount_due = 0.0
    else:
        amount_due = round(max(0.0, fare - net_paid), 2)

    out = {
        "payment_status": trip.payment_status,
        "total_paid": total_paid,
        "amount_due": amount_due,
    }
    if (trip.status or "").startswith("cancel"):
        out["total_amount_refunded"] = total_refunded
    return out


def _otp_view(session: Session, booking_type: str, trip) -> Dict[str, Any]:
    # The start/on-site OTP is only meaningful while the provider is AT the
    # pickup waiting for it (status "arrived").
    if trip.status != "arrived":
        return {}
    active = booking_otp_service.active_otp_view(session, booking_type, trip.id)
    if not active:
        return {}
    code, expires_at = active
    return {"otp": code, "otp_expires_at": expires_at}


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


def build_tow_summary(session: Session, trip: TowTrip) -> Dict[str, Any]:
    editable, deadline = address_edit_window(session, trip)
    result: Dict[str, Any] = {
        "trip_id": trip.reference_id,
        "service_type": "Tow Service",
        "status": trip.status,
        "vehicle_type": trip.vehicle_type,  # customer's vehicle
        "tow_vehicle_type": trip.tow_vehicle_type,  # requested tow-truck class
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
        "address_editable": editable,
        "address_edit_deadline": deadline,
        "cancellable": trip.status in _CANCELLABLE_STATES,
    }
    result.update(_payment_view(session, "tow", trip))
    result.update(_otp_view(session, "tow", trip))

    if trip.tow_truck_driver_id:
        driver = session.get(TowTruckDriver, trip.tow_truck_driver_id)
        if driver:
            result["tow_truck_driver"] = _tow_driver_block(session, driver)
            if trip.status in _TOW_LIVE_STATES:
                result["telemetry_topic"] = telemetry_topic(trip.reference_id)
    elif trip.status == "searching":
        result["nearby_providers"] = geo.nearby_provider_locations(
            session, trip.start_lat, trip.start_lng, kind="tow"
        )
    return result


def build_mechanic_summary(session: Session, trip: MechanicTrip) -> Dict[str, Any]:
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
        "address_editable": editable,
        "address_edit_deadline": deadline,
        "cancellable": trip.status in _CANCELLABLE_STATES,
    }
    result.update(_payment_view(session, "mechanic", trip))
    result.update(_otp_view(session, "mechanic", trip))

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
