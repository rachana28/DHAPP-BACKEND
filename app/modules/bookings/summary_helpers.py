"""Shared building blocks for tow & mechanic booking summaries.

Holds the pieces common to both per-table summary builders: the user payment
view (paid/due/refunded), the live-OTP view (only while the provider is
``arrived``), and the address-edit window rule. Everything returned is
non-sensitive — reference IDs, amounts, statuses — never user UUIDs, integer
primary keys, or phone numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Tuple

from sqlmodel import Session, select

from app.core.models import Payment, User
from app.modules.bookings import otp_service as booking_otp_service
from app.modules.dispatch import geo
from app.utils.time_utils import now_ist

PAID_STATES = ["succeeded", "partially_refunded", "refunded"]

CANCELLABLE_STATES = ("searching", "accepted", "arrived")

_NEXT_ACTION = {
    "searching": "Waiting for a provider to accept",
    "accepted": "Head to the customer and mark arrived",
    "arrived": "Collect the start OTP from the customer and verify it",
    "in_progress": "Service in progress — complete when done",
    "near_destination": "Approaching destination — complete when done",
    "completed": "Completed",
    "cancelled": "Cancelled",
    "no_drivers_found": "No provider was found",
    "no_mechanics_found": "No mechanic was found",
}


def address_edit_window(session: Session, trip) -> Tuple[bool, datetime]:
    window_min = geo.get_config_float(
        session, geo.ADDRESS_EDIT_WINDOW_MIN_KEY, geo.DEFAULT_ADDRESS_EDIT_WINDOW_MIN
    )
    deadline = trip.booking_time + timedelta(minutes=window_min)
    editable = trip.status in ("searching", "accepted") and now_ist() <= deadline
    return editable, deadline


def payment_view(session: Session, service_type: str, trip) -> Dict[str, Any]:
    user_payments = session.exec(
        select(Payment).where(
            Payment.service_type == service_type,
            Payment.service_id == trip.id,
            Payment.payer_type == "user",
            Payment.status.in_(PAID_STATES),
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


def otp_view(
    session: Session, booking_type: str, trip, include_code: bool = True
) -> Dict[str, Any]:
    """OTP block for a booking summary.

    ``include_code=True`` (user view) returns the plaintext OTP for the customer
    to read out. ``include_code=False`` (provider view) returns only
    ``{"otp_pending": True}`` so the provider knows to collect the code from the
    customer without ever seeing it.
    """
    if trip.status != "arrived":
        return {}
    if not include_code:
        pending = booking_otp_service.has_active_otp(session, booking_type, trip.id)
        return {"otp_pending": bool(pending)}
    active = booking_otp_service.active_otp_view(session, booking_type, trip.id)
    if not active:
        return {}
    code, expires_at = active
    return {"otp": code, "otp_expires_at": expires_at}


def provider_user_block(session: Session, user_id) -> Dict[str, Any]:
    """Sanitized customer block for provider-facing summaries: name + avatar only,
    NEVER phone, email, or the user UUID."""
    user = session.get(User, user_id)
    if not user:
        return {}
    return {"full_name": user.full_name, "avatar_url": user.avatar_url}


def amount_to_collect(session: Session, service_type: str, trip) -> float:
    """What the provider should collect from the customer at completion — the
    payment_view's outstanding ``amount_due`` (0 once paid or cancelled)."""
    return float(payment_view(session, service_type, trip).get("amount_due", 0.0))


def provider_actions(trip, service_type: str) -> Dict[str, Any]:
    """Derived (read-only) lifecycle action booleans + a next-step hint for the
    provider app. Tells the app which buttons to enable for the current status;
    performs no writes and mirrors the existing endpoint guards."""
    status = trip.status or ""
    live_complete_states = (
        ("in_progress", "near_destination")
        if service_type == "tow"
        else ("in_progress",)
    )
    return {
        "can_mark_arrived": status == "accepted",
        "can_verify_otp": status == "arrived",
        "can_complete": status in live_complete_states,
        "can_cancel": status in CANCELLABLE_STATES,
        "next_action": _NEXT_ACTION.get(status, status),
    }
