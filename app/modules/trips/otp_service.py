"""
Per-shift OTP: the user gets the OTP, reads it to the driver, driver enters
it in their app and starts the shift.

Rows are uniquely keyed off ``TripAttendance.id`` so a cross-midnight shift
gets exactly one OTP. The public methods still accept ``(trip_id, trip_date)``
for back-compat and resolve to the matching attendance internally.

Storage: Redis is primary (fast, 12-hour TTL), DB is fallback when Redis is
down. Generation creates / updates the DB row; verification reads from DB and
clears the Redis copy on success.
"""

import hashlib
import secrets
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

import redis
from sqlmodel import Session, select

from app.core.models import OTPRegistry, Trip, TripAttendance
from app.utils.time_utils import now_ist, today_ist


class OTPService:
    MAX_ATTEMPTS = 3
    OTP_LENGTH = 6
    PRE_START_VALIDITY_MINUTES = 30
    POST_START_VALIDITY = timedelta(hours=12)

    def __init__(self, redis_client: Optional[redis.Redis]):
        self.redis = redis_client

    # ─── primitives ──────────────────────────────────────────────────────────
    def _generate_otp(self) -> str:
        return "".join(secrets.choice("0123456789") for _ in range(self.OTP_LENGTH))

    def _hash_otp(self, otp: str) -> str:
        return hashlib.sha256(otp.encode()).hexdigest()

    def _redis_key(self, attendance_id: int) -> str:
        return f"otp:attendance:{attendance_id}"

    def _attempts_key(self, attendance_id: int, who: str) -> str:
        return f"otp:attempts:{attendance_id}:{who}"

    def _redis_get(self, key: str) -> Optional[str]:
        if not self.redis:
            return None
        try:
            val = self.redis.get(key)
        except redis.RedisError:
            return None
        if val is None:
            return None
        if isinstance(val, bytes):
            return val.decode()
        return val

    def _redis_set(self, key: str, value: str, ttl: int) -> bool:
        if not self.redis:
            return False
        try:
            return bool(self.redis.set(name=key, value=value, ex=ttl, nx=True))
        except redis.RedisError:
            return False

    # ─── attendance resolution ───────────────────────────────────────────────
    def _resolve_attendance_id(
        self,
        session: Session,
        trip_id: int,
        trip_date: date,
        attendance_id: Optional[int],
    ) -> Optional[int]:
        """Pick the right TripAttendance row for (trip_id, trip_date).

        If ``attendance_id`` is given we trust it. Otherwise we look for the
        attendance whose ``trip_date`` matches; if multiple match (rare —
        only if the schema is ever extended to multi-shift days) we pick the
        earliest ``scheduled_start``.
        """
        if attendance_id is not None:
            return attendance_id
        att = session.exec(
            select(TripAttendance)
            .where(
                TripAttendance.trip_id == trip_id,
                TripAttendance.trip_date == trip_date,
            )
            .order_by(TripAttendance.scheduled_start)
            .limit(1)
        ).first()
        return att.id if att else None

    def _find_registry_row(
        self, session: Session, attendance_id: int
    ) -> Optional[OTPRegistry]:
        return session.exec(
            select(OTPRegistry).where(OTPRegistry.attendance_id == attendance_id)
        ).first()

    # ─── generate ────────────────────────────────────────────────────────────
    def generate_otp(
        self,
        session: Session,
        trip_id: int,
        trip_start_time: datetime,
        trip_date: Optional[date] = None,
        attendance_id: Optional[int] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        trip_date = trip_date or trip_start_time.date()
        now = now_ist()
        valid_from = trip_start_time - timedelta(
            minutes=self.PRE_START_VALIDITY_MINUTES
        )
        expiry_at = trip_start_time + self.POST_START_VALIDITY

        if expiry_at <= now:
            return None, "OTP window has already passed"

        resolved_att_id = self._resolve_attendance_id(
            session, trip_id, trip_date, attendance_id
        )
        if resolved_att_id is None:
            return None, "No attendance record for this shift"

        rkey = self._redis_key(resolved_att_id)

        existing = self._redis_get(rkey)
        if existing:
            return existing, None

        db_row = self._find_registry_row(session, resolved_att_id)
        if db_row and db_row.verified_at is not None:
            return None, "OTP already verified for this shift"

        plain = self._generate_otp()
        ttl = max(1, int((expiry_at - now).total_seconds()))

        if not self._redis_set(rkey, plain, ttl):
            existing = self._redis_get(rkey)
            if existing:
                return existing, None

        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                if self.redis:
                    try:
                        self.redis.delete(rkey)
                    except redis.RedisError:
                        pass
                return None, "Trip not found"

            otp_hash = self._hash_otp(plain)
            if db_row:
                db_row.otp_hash = otp_hash
                db_row.otp_expiry_at = expiry_at
                db_row.valid_from = valid_from
                db_row.verification_attempts = 0
                db_row.created_at = now
                db_row.verified_by_driver_id = None
                session.add(db_row)
            else:
                session.add(
                    OTPRegistry(
                        trip_id=trip_id,
                        attendance_id=resolved_att_id,
                        trip_date=trip_date,
                        otp_hash=otp_hash,
                        otp_expiry_at=expiry_at,
                        valid_from=valid_from,
                        verification_attempts=0,
                    )
                )
            session.commit()
            return plain, None
        except Exception as e:
            session.rollback()
            if self.redis:
                try:
                    self.redis.delete(rkey)
                except redis.RedisError:
                    pass
            return None, f"OTP generation failed: {e}"

    # ─── verify ──────────────────────────────────────────────────────────────
    def verify_otp(
        self,
        session: Session,
        trip_id: int,
        driver_id: int,
        otp_input: str,
        trip_date: Optional[date] = None,
        attendance_id: Optional[int] = None,
    ) -> Tuple[bool, Optional[str]]:
        if trip_date is None:
            trip_date = today_ist()

        resolved_att_id = self._resolve_attendance_id(
            session, trip_id, trip_date, attendance_id
        )
        if resolved_att_id is None:
            return False, "No attendance record for this shift"

        attempts_key = self._attempts_key(resolved_att_id, str(driver_id))
        if self.redis:
            try:
                attempts_raw = self.redis.get(attempts_key)
                attempts = int(attempts_raw) if attempts_raw else 0
                if attempts >= self.MAX_ATTEMPTS:
                    return False, "Maximum OTP verification attempts exceeded"
            except redis.RedisError:
                attempts = 0
        else:
            attempts = 0

        # Defense in depth: ensure the caller is the trip's assigned driver,
        # even if the router accidentally permitted otherwise in the future.
        trip = session.get(Trip, trip_id)
        if not trip:
            return False, "Trip not found"
        if trip.driver_id != driver_id:
            return False, "Driver not assigned to this trip"

        db_row = self._find_registry_row(session, resolved_att_id)
        if not db_row:
            return False, "OTP not generated yet"

        now = now_ist()
        if db_row.valid_from and now < db_row.valid_from:
            return False, "OTP not yet valid"
        if now > db_row.otp_expiry_at:
            return False, "OTP has expired"
        if db_row.verified_at is not None:
            return False, "OTP already used"

        max_db_attempts = db_row.max_attempts or self.MAX_ATTEMPTS
        if db_row.verification_attempts >= max_db_attempts:
            return False, "Maximum OTP verification attempts exceeded"

        is_valid = secrets.compare_digest(self._hash_otp(otp_input), db_row.otp_hash)
        if not is_valid:
            db_row.verification_attempts += 1
            session.add(db_row)
            session.commit()
            if self.redis:
                try:
                    self.redis.incr(attempts_key)
                    self.redis.expire(attempts_key, 900)
                except redis.RedisError:
                    pass
            remaining = max(0, self.MAX_ATTEMPTS - attempts - 1)
            return False, f"Invalid OTP. Attempts remaining: {remaining}"

        db_row.verified_at = now
        db_row.verified_by_driver_id = driver_id
        session.add(db_row)
        session.commit()

        if self.redis:
            try:
                self.redis.delete(self._redis_key(resolved_att_id))
                self.redis.delete(attempts_key)
            except redis.RedisError:
                pass

        return True, None

    # ─── inspection helpers ──────────────────────────────────────────────────
    def is_otp_verified(
        self,
        session: Session,
        trip_id: int,
        trip_date: Optional[date] = None,
        attendance_id: Optional[int] = None,
    ) -> bool:
        if trip_date is None:
            trip_date = today_ist()
        resolved_att_id = self._resolve_attendance_id(
            session, trip_id, trip_date, attendance_id
        )
        if resolved_att_id is None:
            return False
        row = self._find_registry_row(session, resolved_att_id)
        return bool(row and row.verified_at is not None)

    def invalidate_otp(
        self,
        trip_id: int,
        trip_date: date,
        attendance_id: Optional[int] = None,
    ):
        if not self.redis or attendance_id is None:
            return
        try:
            self.redis.delete(self._redis_key(attendance_id))
        except redis.RedisError:
            pass

    def get_otp_expiry_time(
        self,
        session: Session,
        trip_id: int,
        trip_date: Optional[date] = None,
        attendance_id: Optional[int] = None,
    ) -> Optional[datetime]:
        if trip_date is None:
            trip_date = today_ist()
        resolved_att_id = self._resolve_attendance_id(
            session, trip_id, trip_date, attendance_id
        )
        if resolved_att_id is None:
            return None
        row = self._find_registry_row(session, resolved_att_id)
        return row.otp_expiry_at if row else None
