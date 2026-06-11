"""'Mechanic has arrived' transition for mechanic bookings.

Flips an ``accepted`` mechanic trip to ``arrived``, issues the completion OTP
(persisted + plaintext cached for the summary API), and pushes it to the
customer. Used by BOTH triggers so they never diverge: the telemetry worker's
geofence check (auto) and the manual ``POST /mechanic-trips/{ref}/mark-arrived``
endpoint (fallback when telemetry is unavailable or GPS drift keeps the
geofence from firing). Returns the fresh OTP code, or ``None`` if the booking
wasn't in a state to arrive (idempotent).
"""

from __future__ import annotations

from typing import Optional

from sqlmodel import Session

from app.modules.bookings import otp_service as booking_otp_service
from app.utils.notifications import notify_safe


def mark_mechanic_arrived(
    session: Session, mech, *, notify: bool = True
) -> Optional[str]:
    if mech.status != "accepted":
        return None
    mech.status = "arrived"
    session.add(mech)
    session.commit()

    code = booking_otp_service.generate(session, "mechanic", mech.id)
    if notify:
        notify_safe(
            session,
            [mech.user_id],
            "Mechanic Arrived 🛠️",
            f"Your mechanic is here. Share OTP {code} to confirm the service.",
            {"trip_id": mech.reference_id, "otp": code, "screen": "otp"},
        )
    return code
