"""Cross-service unpaid-dues gate (D6).

A user with an unpaid PAST-DUE settlement is blocked from creating any new
booking (trip / service-center / tow / mechanic) until the dues are cleared.
"Past due" respects a grace window:

  * Trips      — TripSettlement.due_date (falls back to settlement_date).
  * ServiceCtr — a completed ServiceRequest with an outstanding balance whose
                 completed_time is older than ``service_center_settlement_grace_hours``
                 (SystemConfig, default 24h).

``get_unpaid_past_due`` returns a small dict describing the oldest blocking due,
or ``None`` when the user is clear. The booking endpoints raise 402 with that
payload so the client can route the user to the outstanding bill.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional, Dict, Any

from sqlmodel import Session, select

from app.core.models import (
    TripSettlement,
    ServiceRequest,
    ServiceStatus,
    SystemConfig,
    Trip,
)
from app.utils.time_utils import now_ist, today_ist

SC_SETTLEMENT_GRACE_HOURS_KEY = "service_center_settlement_grace_hours"
DEFAULT_SC_SETTLEMENT_GRACE_HOURS = 24.0


def _grace_hours(session: Session) -> float:
    cfg = session.get(SystemConfig, SC_SETTLEMENT_GRACE_HOURS_KEY)
    if cfg and cfg.value:
        try:
            return float(cfg.value)
        except (TypeError, ValueError):
            pass
    return DEFAULT_SC_SETTLEMENT_GRACE_HOURS


def get_unpaid_past_due(session: Session, user_id) -> Optional[Dict[str, Any]]:
    """Return the user's oldest unpaid past-due settlement, or None if clear."""
    today = today_ist()

    # --- Trips: TripSettlement past its due date ---
    settlements = session.exec(
        select(TripSettlement).where(
            TripSettlement.user_id == user_id,
            TripSettlement.user_payment_status != "paid",
            TripSettlement.remaining_due > 0,
        )
    ).all()
    for s in settlements:
        ref_date = s.due_date or s.settlement_date
        if ref_date is not None and ref_date < today:
            trip = session.get(Trip, s.trip_id)
            return {
                "service_type": "trip",
                "service_reference_id": trip.reference_id if trip else None,
                "settlement_id": s.id,
                "amount_due": round(s.remaining_due, 2),
                "due_date": str(ref_date),
            }

    # --- Service center: completed booking with outstanding balance past grace ---
    cutoff = now_ist() - timedelta(hours=_grace_hours(session))
    sc_bookings = session.exec(
        select(ServiceRequest).where(
            ServiceRequest.user_id == user_id,
            ServiceRequest.status == ServiceStatus.COMPLETED,
            ServiceRequest.payment_status.notin_(["paid", "refunded"]),
        )
    ).all()
    for b in sc_bookings:
        outstanding = round((b.final_price or 0.0) - (b.amount_paid or 0.0), 2)
        if outstanding > 0 and b.completed_time and b.completed_time < cutoff:
            return {
                "service_type": "service_center",
                "service_reference_id": b.reference_id,
                "amount_due": outstanding,
                "due_date": str(b.completed_time.date()),
            }

    return None


def raise_if_unpaid_past_due(session: Session, user_id) -> None:
    """Raise 402 when the user has an unpaid past-due settlement (D6 gate)."""
    from fastapi import HTTPException

    due = get_unpaid_past_due(session, user_id)
    if due:
        raise HTTPException(
            status_code=402,
            detail={
                "message": (
                    "You have an unpaid past-due settlement. Please clear it "
                    "before making a new booking."
                ),
                "outstanding": due,
            },
        )
