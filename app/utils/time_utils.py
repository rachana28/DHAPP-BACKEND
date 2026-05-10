"""
IST timezone helpers — shared utility for any module that needs IST-naive datetimes.

Originally introduced for the trip-management module (booking_time,
scheduled_*_time, OTPRegistry, TripBill, TripAttendance, PaymentTransaction,
scheduler jobs, fare engine). Lives in `app.utils` so other modules can adopt
the same convention without re-implementing it.

Why naive: SQLModel/SQLAlchemy + the existing schema stores naive timestamps;
introducing aware timestamps in selected tables would force every comparison
site to special-case them. So we use naive datetimes whose wall-clock value is
IST. The :data:`IST` timezone object below is provided for any caller that *does*
need an aware value.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Optional

# IST = UTC + 05:30 (no DST)
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """
    Current wall-clock time in IST as a naive datetime.
    Drop-in replacement for ``datetime.utcnow()``.
    """
    return datetime.now(IST).replace(tzinfo=None)


def today_ist() -> date:
    """Today's date in IST. Drop-in replacement for ``date.today()``."""
    return now_ist().date()


def to_ist_naive(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Normalize an inbound datetime to IST naive form.

    Behaviour:
      - None              → None
      - aware datetime    → converted to IST then stripped of tzinfo
      - naive datetime    → returned as-is (caller must already mean IST)

    Use this when accepting datetime fields from API payloads so clients can
    send either ``"2026-05-10T15:00:00"`` (naive IST) or
    ``"2026-05-10T09:30:00+00:00"`` (aware UTC) and the backend behaves
    identically.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(IST).replace(tzinfo=None)
