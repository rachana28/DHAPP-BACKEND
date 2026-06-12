"""Summary builder for service-center bookings (``ServiceRequest``).

Brings the service-center booking flow to parity with the tow / mechanic / trip
summaries: one rich, role-aware endpoint for the user app and the center app.

Exposes ONLY non-sensitive data — reference IDs, statuses, vehicle + schedule
details, and prices. NEVER user UUIDs, integer primary keys, or phone numbers
(note: unlike the existing ``ServiceRequestForCenter`` model, the center view
here deliberately omits ``customer_phone``).
"""

from __future__ import annotations

from typing import Any, Dict

from sqlmodel import Session

from app.core.models import ServiceCenter, ServiceRequest, ServiceStatus
from app.modules.bookings.summary_helpers import provider_user_block

# Booking is no longer changeable once it reaches a terminal state.
_TERMINAL_STATES = (ServiceStatus.COMPLETED.value, ServiceStatus.CANCELLED.value)

# Center-app "next step" hint per status.
_SERVICE_NEXT_ACTION = {
    ServiceStatus.PENDING_CONFIRMATION.value: "Accept or decline this booking",
    ServiceStatus.BOOKED.value: "Accept the booking to confirm",
    ServiceStatus.ACCEPTED.value: "Check the customer in when they arrive",
    ServiceStatus.CHECKED_IN.value: "Start the service",
    ServiceStatus.SERVICE_ONGOING.value: "Set the price/return and accept the service, then complete",
    ServiceStatus.SERVICE_ACCEPTED.value: "Complete the service when finished",
    ServiceStatus.COMPLETED.value: "Completed",
    ServiceStatus.CANCELLED.value: "Cancelled",
}


def _status_value(status) -> str:
    return getattr(status, "value", status) or ""


def _booking_type_value(bt) -> str:
    return getattr(bt, "value", bt) or ""


def _amount_due(booking: ServiceRequest) -> float:
    """Outstanding amount on the booking — total price minus what's been paid; 0
    once fully paid or cancelled. Uses the booking's own fields (no ledger join)."""
    status = _status_value(booking.status)
    if booking.payment_status == "paid" or status == ServiceStatus.CANCELLED.value:
        return 0.0
    total = (
        booking.final_price
        if booking.final_price is not None
        else (booking.price_at_booking or 0.0)
    )
    return round(max(0.0, float(total or 0.0) - float(booking.amount_paid or 0.0)), 2)


def _center_block(center: ServiceCenter) -> Dict[str, Any]:
    """Public service-center block for the user view — no center phone number."""
    return {
        "id": center.reference_id,
        "name": center.name,
        "rating": center.rating,
        "address": center.address,
        "latitude": center.latitude,
        "longitude": center.longitude,
        "profile_picture_url": center.profile_picture_url,
    }


def _service_actions(booking: ServiceRequest) -> Dict[str, Any]:
    """Derived (read-only) lifecycle action booleans + next-step hint for the
    center app. Mirrors the existing center-router endpoint guards."""
    status = _status_value(booking.status)
    return {
        "can_accept": status
        in (ServiceStatus.PENDING_CONFIRMATION.value, ServiceStatus.BOOKED.value),
        "can_check_in": status == ServiceStatus.ACCEPTED.value,
        "can_start_service": status == ServiceStatus.CHECKED_IN.value,
        "can_complete": status
        in (ServiceStatus.SERVICE_ONGOING.value, ServiceStatus.SERVICE_ACCEPTED.value),
        "can_cancel": status not in _TERMINAL_STATES,
        "next_action": _SERVICE_NEXT_ACTION.get(status, status),
    }


def build_service_summary(
    session: Session, booking: ServiceRequest, viewer: str = "user"
) -> Dict[str, Any]:
    """Service-center booking summary.

    ``viewer="user"`` returns the customer-facing payload with a public center
    block. ``viewer="center"`` returns the center-app payload: a sanitized
    customer block (name + avatar, no phone), an ``actions`` block,
    ``amount_to_collect`` and the next-step hint.
    """
    status = _status_value(booking.status)
    result: Dict[str, Any] = {
        "booking_id": booking.reference_id,
        "service_name": booking.service_name,
        "booking_type": _booking_type_value(booking.booking_type),
        "status": status,
        "vehicle_type": booking.vehicle_type,
        "vehicle_number": booking.vehicle_number,
        "vehicle_model": booking.vehicle_model,
        "requested_date": booking.requested_date,
        "requested_time": booking.requested_time,
        "expected_return_date": booking.expected_return_date,
        "expected_return_time": booking.expected_return_time,
        "actual_return_date": booking.actual_return_date,
        "actual_return_time": booking.actual_return_time,
        "booking_time": booking.booking_time,
        "price_at_booking": booking.price_at_booking,
        "final_price": booking.final_price,
        "advance_amount": booking.advance_amount,
        "amount_paid": booking.amount_paid,
        "payment_status": booking.payment_status,
        "amount_due": _amount_due(booking),
        "price_components": booking.price_components,
        "cancellation_reason": booking.cancellation_reason,
    }

    if viewer == "center":
        result["amount_to_collect"] = _amount_due(booking)
        result["actions"] = _service_actions(booking)
        result["customer"] = provider_user_block(session, booking.user_id)
        return result

    # --- user view ---
    result["cancellable"] = status not in _TERMINAL_STATES
    center = session.get(ServiceCenter, booking.service_center_id)
    if center:
        result["service_center"] = _center_block(center)
    return result
