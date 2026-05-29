"""
Collections handoff adapter (F8).

When a final-settlement balance stays unpaid past the 30-day grace window the
trip is handed off to "collections". Today there is no external collections
partner integrated, so this adapter:

  1. Emits a critical-severity audit event so ops sees it in the central log.
  2. Files a SupportTicket (raised_by_role="system", category="payment") so the
     ticket appears in the admin support inbox alongside user-raised tickets.

The shape of :func:`send_to_collections` is intentionally narrow so it can be
swapped for a real partner API later (one HTTP call inside the adapter, no
caller-side changes).
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlmodel import Session

from app.core.models import (
    SupportTicket,
    TripSettlement,
)
from app.services.audit_log import emit_event as audit_emit
from app.utils.time_utils import now_ist

_LOG = logging.getLogger("dhapp.collections")


def _build_ticket_id(settlement_id: int) -> str:
    """Synthesise a ticket id distinct from the user-raised pool.

    User tickets use ``TKT-<n>``; system collections tickets use
    ``COL-<settlement_id>`` so support staff can spot them at a glance.
    """
    return f"COL-{settlement_id}"


def send_to_collections(
    session: Session,
    settlement: TripSettlement,
    days_overdue: int,
) -> Optional[int]:
    """File a collections-handoff record for an overdue settlement.

    Returns the SupportTicket id on success, None if filing already happened
    (idempotent — safe to call repeatedly from the dunning scheduler).
    """
    if settlement.collections_sent_at is not None:
        return None

    audit_emit(
        "collections.handoff",
        trip_id=settlement.trip_id,
        actor="system",
        actor_id=str(settlement.user_id),
        severity="critical",
        payload={
            "settlement_id": settlement.id,
            "remaining_due": settlement.remaining_due,
            "days_overdue": days_overdue,
            "settlement_date": settlement.settlement_date.isoformat(),
        },
    )

    try:
        ticket = SupportTicket(
            user_id=settlement.user_id,
            ticket_id=_build_ticket_id(settlement.id),
            subject="Collections: unpaid settlement",
            description=(
                f"Trip #{settlement.trip_id} settlement ID {settlement.id} is "
                f"{days_overdue} days past due with ₹{settlement.remaining_due:.2f} "
                "outstanding. Auto-filed by the dunning scheduler."
            ),
            category="payment",
            status="open",
            service_type="trip",
            service_ref_id=settlement.trip_id,
            raised_by_role="system",
        )
        session.add(ticket)
        session.flush()
        settlement.collections_sent_at = now_ist()
        session.add(settlement)
        return ticket.id
    except Exception as exc:  # pragma: no cover - support model is required, log if missing
        _LOG.exception("collections ticket creation failed: %s", exc)
        return None
