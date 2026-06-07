"""Geofenced start/end OTP for tow & mechanic bookings.

Backed by the ``BookingOTP`` table (at most one *active* row per booking). The
telemetry worker calls :func:`generate` when the provider reaches the pickup
geofence; the user app calls :func:`regenerate` after the 30-min expiry; the
provider verifies manually via :func:`verify`.

Kept deliberately separate from the regular-trip ``OTPService`` (which is keyed
on ``TripAttendance``). DB-backed only — these are low-frequency, one-shot OTPs,
so the Redis-primary machinery of the trip flow would be overkill here.
"""

from datetime import timedelta
from typing import Optional, Tuple

from sqlmodel import Session, select

from app.core import otp_redis
from app.core.models import BookingOTP
from app.modules.dispatch import geo
from app.utils.time_utils import now_ist

OTP_LENGTH = otp_redis.OTP_LENGTH


def _generate_code() -> str:
    return otp_redis.generate_numeric_code(OTP_LENGTH)


def _hash(code: str) -> str:
    return otp_redis.sha256_hex(code)


def _purge_db_rows(session: Session, booking_type: str, booking_id: int) -> None:
    """Delete ALL DB OTP rows for a booking (used on verify success)."""
    rows = session.exec(
        select(BookingOTP).where(
            BookingOTP.booking_type == booking_type,
            BookingOTP.booking_id == booking_id,
        )
    ).all()
    for row in rows:
        session.delete(row)
    if rows:
        session.commit()


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


def active_otp_view(
    session: Session, booking_type: str, booking_id: int
) -> Optional[Tuple[str, "object"]]:
    """Live (plaintext, expires_at) for an active unverified OTP, or None.

    Used by the user-facing summary API so the customer can read the code
    without depending on the arrival push being delivered.
    """
    row = _active_row(session, booking_type, booking_id)
    if row and row.otp_plain:
        return row.otp_plain, row.expires_at
    return None


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
        row.otp_plain = None
        session.add(row)

    code = _generate_code()
    expiry_min = geo.get_config_float(
        session, geo.BOOKING_OTP_EXPIRY_MIN_KEY, geo.DEFAULT_BOOKING_OTP_EXPIRY_MIN
    )
    expires_at = now + timedelta(minutes=expiry_min)
    otp_hash = _hash(code)
    otp = BookingOTP(
        booking_type=booking_type,
        booking_id=booking_id,
        otp_hash=otp_hash,
        otp_plain=code,
        expires_at=expires_at,
    )
    session.add(otp)
    session.commit()

    # Redis is primary: store the hash with a TTL equal to the remaining
    # validity (it self-expires). The DB row above is the secondary/fallback.
    ttl = max(1, int((expires_at - now).total_seconds()))
    otp_redis.store_hash(booking_type, booking_id, otp_hash, ttl)
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

    # Redis is primary for the hash; fall back to the DB row when Redis is down
    # or the key is missing.
    expected_hash = otp_redis.get_hash(booking_type, booking_id) or row.otp_hash
    if _hash(code) != expected_hash:
        row.attempts += 1
        session.add(row)
        session.commit()
        remaining = max(row.max_attempts - row.attempts, 0)
        return False, f"Incorrect OTP. {remaining} attempt(s) left."

    # Success → wipe the OTP from BOTH stores (post-verification deletion).
    otp_redis.delete(booking_type, booking_id)
    _purge_db_rows(session, booking_type, booking_id)
    return True, None
