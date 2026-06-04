"""Geofenced start/end OTP for tow & mechanic bookings.

Backed by the ``BookingOTP`` table (at most one *active* row per booking). The
telemetry worker calls :func:`generate` when the provider reaches the pickup
geofence; the user app calls :func:`regenerate` after the 30-min expiry; the
provider verifies manually via :func:`verify`.

Kept deliberately separate from the regular-trip ``OTPService`` (which is keyed
on ``TripAttendance``). DB-backed only — these are low-frequency, one-shot OTPs,
so the Redis-primary machinery of the trip flow would be overkill here.
"""

import hashlib
import secrets
from datetime import timedelta
from typing import Optional, Tuple

from sqlmodel import Session, select

from app.core.models import BookingOTP
from app.modules.dispatch import geo
from app.utils.time_utils import now_ist

OTP_LENGTH = 6


def _generate_code() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _active_row(
    session: Session, booking_type: str, booking_id: int
) -> Optional[BookingOTP]:
    now = now_ist()
    return session.exec(
        select(BookingOTP)
        .where(
            BookingOTP.booking_type == booking_type,
            BookingOTP.booking_id == booking_id,
            BookingOTP.verified_at.is_(None),
            BookingOTP.expires_at > now,
        )
        .order_by(BookingOTP.created_at.desc())
    ).first()


def has_active_otp(session: Session, booking_type: str, booking_id: int) -> bool:
    return _active_row(session, booking_type, booking_id) is not None


def is_verified(session: Session, booking_type: str, booking_id: int) -> bool:
    row = session.exec(
        select(BookingOTP)
        .where(
            BookingOTP.booking_type == booking_type,
            BookingOTP.booking_id == booking_id,
            BookingOTP.verified_at.is_not(None),
        )
        .limit(1)
    ).first()
    return row is not None


def generate(session: Session, booking_type: str, booking_id: int) -> str:
    """Create a fresh OTP, superseding any prior active (unverified) row.

    Returns the plaintext code (delivered to the user out-of-band: pushed by the
    worker on first issue, or returned to the user app on regeneration).
    """
    now = now_ist()

    # Expire any prior unverified rows so the invariant "≤1 active row" holds.
    prior = session.exec(
        select(BookingOTP).where(
            BookingOTP.booking_type == booking_type,
            BookingOTP.booking_id == booking_id,
            BookingOTP.verified_at.is_(None),
            BookingOTP.expires_at > now,
        )
    ).all()
    for row in prior:
        row.expires_at = now
        session.add(row)

    code = _generate_code()
    expiry_min = geo.get_config_float(
        session, geo.BOOKING_OTP_EXPIRY_MIN_KEY, geo.DEFAULT_BOOKING_OTP_EXPIRY_MIN
    )
    otp = BookingOTP(
        booking_type=booking_type,
        booking_id=booking_id,
        otp_hash=_hash(code),
        expires_at=now + timedelta(minutes=expiry_min),
    )
    session.add(otp)
    session.commit()
    return code


def verify(
    session: Session,
    booking_type: str,
    booking_id: int,
    code: str,
    provider_user_id,
) -> Tuple[bool, Optional[str]]:
    """Validate a manually-entered OTP. Returns (ok, error_message)."""
    row = _active_row(session, booking_type, booking_id)
    if not row:
        latest = session.exec(
            select(BookingOTP)
            .where(
                BookingOTP.booking_type == booking_type,
                BookingOTP.booking_id == booking_id,
            )
            .order_by(BookingOTP.created_at.desc())
        ).first()
        if latest and latest.verified_at:
            return False, "OTP already verified."
        return False, "OTP expired or not generated. Ask the customer to regenerate it."

    if row.attempts >= row.max_attempts:
        return (
            False,
            "Too many incorrect attempts. Ask the customer to regenerate the OTP.",
        )

    if _hash(code) != row.otp_hash:
        row.attempts += 1
        session.add(row)
        session.commit()
        remaining = max(row.max_attempts - row.attempts, 0)
        return False, f"Incorrect OTP. {remaining} attempt(s) left."

    row.verified_at = now_ist()
    row.verified_by_user_id = provider_user_id
    session.add(row)
    session.commit()
    return True, None
