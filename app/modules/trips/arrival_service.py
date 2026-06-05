"""Shared 'provider has arrived' transition for tow & mechanic bookings.

Flips an ``accepted`` booking to ``arrived``, issues the start/completion OTP
(persisted + plaintext cached for the summary API), and pushes it to the
customer. Used by BOTH triggers so they never diverge:

- the telemetry worker's geofence check (auto), and
- the manual ``POST .../mark-arrived`` endpoint (fallback for when telemetry is
  unavailable — ``MQTT_HOST`` unset — or GPS drift keeps the geofence from
  firing).

Returns the freshly generated OTP code, or ``None`` if the booking wasn't in a
state to arrive (idempotent: re-calling on an already-``arrived`` booking is a
no-op).
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlmodel import Session

from app.modules.trips import booking_otp_service
from app.utils.notifications import send_push_notification

logger = logging.getLogger(__name__)


def _notify(session: Session, user_ids, title: str, body: str, data: dict) -> None:
    try:
        send_push_notification(
            session=session, user_ids=user_ids, title=title, body=body, data=data
        )
    except Exception as e:  # a push failure must not roll back the transition
        logger.debug("Arrival push notification failed: %s", e)


def mark_tow_arrived(session: Session, tow, *, notify: bool = True) -> Optional[str]:
    """``accepted`` → ``arrived`` for a tow trip; issue + (optionally) push OTP."""
    if tow.status != "accepted":
        return None
    tow.status = "arrived"
    session.add(tow)
    session.commit()

    code = booking_otp_service.generate(session, "tow", tow.id)
    if notify:
        _notify(
            session,
            [tow.user_id],
            "Driver Arrived 🚛",
            f"Your tow driver is here. Share OTP {code} to start the tow.",
            {"trip_id": tow.reference_id, "otp": code, "screen": "otp"},
        )
    return code


def mark_mechanic_arrived(
    session: Session, mech, *, notify: bool = True
) -> Optional[str]:
    """``accepted`` → ``arrived`` for a mechanic trip; issue + (optionally) push OTP."""
    if mech.status != "accepted":
        return None
    mech.status = "arrived"
    session.add(mech)
    session.commit()

    code = booking_otp_service.generate(session, "mechanic", mech.id)
    if notify:
        _notify(
            session,
            [mech.user_id],
            "Mechanic Arrived 🛠️",
            f"Your mechanic is here. Share OTP {code} to confirm the service.",
            {"trip_id": mech.reference_id, "otp": code, "screen": "otp"},
        )
    return code
