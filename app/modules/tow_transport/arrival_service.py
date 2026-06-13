"""'Driver has arrived' transition for tow bookings.

Flips an ``accepted`` tow trip to ``arrived``, issues the start OTP (persisted +
plaintext cached for the summary API), and pushes it to the customer. Used by
BOTH triggers so they never diverge: the telemetry worker's geofence check
(auto) and the manual ``POST /tow-transport-trips/{ref}/mark-arrived`` endpoint (fallback
when telemetry is unavailable or GPS drift keeps the geofence from firing).
Returns the fresh OTP code, or ``None`` if the booking wasn't in a state to
arrive (idempotent: re-calling on an already-``arrived`` booking is a no-op).
"""

from __future__ import annotations

from typing import Optional

from sqlmodel import Session

from app.modules.bookings import otp_service as booking_otp_service
from app.utils.notifications import notify_safe


def mark_tow_arrived(session: Session, tow, *, notify: bool = True) -> Optional[str]:
    if tow.status != "accepted":
        return None
    tow.status = "arrived"
    session.add(tow)
    session.commit()

    code = booking_otp_service.generate(session, "tow", tow.id)
    if notify:
        notify_safe(
            session,
            [tow.user_id],
            "Driver Arrived 🚛",
            f"Your tow driver is here. Share OTP {code} to start the tow.",
            {"trip_id": tow.reference_id, "otp": code, "screen": "otp"},
        )
    return code
