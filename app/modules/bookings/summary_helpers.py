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

from app.core.models import Payment
from app.modules.bookings import otp_service as booking_otp_service
from app.modules.dispatch import geo
from app.utils.time_utils import now_ist

PAID_STATES = ["succeeded", "partially_refunded", "refunded"]

CANCELLABLE_STATES = ("searching", "accepted", "arrived")


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


def otp_view(session: Session, booking_type: str, trip) -> Dict[str, Any]:
    if trip.status != "arrived":
        return {}
    active = booking_otp_service.active_otp_view(session, booking_type, trip.id)
    if not active:
        return {}
    code, expires_at = active
    return {"otp": code, "otp_expires_at": expires_at}
