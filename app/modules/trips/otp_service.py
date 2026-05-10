"""
OTP Service for Trip Management — Uber-style single-OTP flow.
"""

import hashlib
import secrets
from datetime import date, datetime, timedelta

from app.utils.time_utils import now_ist, today_ist
from typing import Optional, Tuple

import redis
from sqlmodel import Session, select

from app.core.models import OTPRegistry, Trip


class OTPService:
    MAX_ATTEMPTS = 3
    OTP_LENGTH = 6
    PRE_START_VALIDITY_MINUTES = 30
    POST_START_VALIDITY = timedelta(hours=12)

    def __init__(self, redis_client: Optional[redis.Redis]):
        self.redis = redis_client

    def _generate_otp(self) -> str:
        return "".join(secrets.choice("0123456789") for _ in range(self.OTP_LENGTH))

    def _hash_otp(self, otp: str) -> str:
        return hashlib.sha256(otp.encode()).hexdigest()

    def _redis_key(self, trip_id: int, trip_date: date) -> str:
        return f"otp:trip:{trip_id}:{trip_date.isoformat()}"

    def _attempts_key(self, trip_id: int, trip_date: date, who: str) -> str:
        return f"otp:attempts:{trip_id}:{trip_date.isoformat()}:{who}"

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

    def generate_otp(
        self,
        session: Session,
        trip_id: int,
        trip_start_time: datetime,
        trip_date: Optional[date] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        trip_date = trip_date or trip_start_time.date()
        now = now_ist()
        valid_from = trip_start_time - timedelta(
            minutes=self.PRE_START_VALIDITY_MINUTES
        )
        expiry_at = trip_start_time + self.POST_START_VALIDITY

        if expiry_at <= now:
            return None, "OTP window has already passed"

        rkey = self._redis_key(trip_id, trip_date)

        existing = self._redis_get(rkey)
        if existing:
            return existing, None

        db_row = session.exec(
            select(OTPRegistry).where(
                OTPRegistry.trip_id == trip_id,
                OTPRegistry.trip_date == trip_date,
            )
        ).first()

        if db_row and db_row.verified_at is not None:
            return None, "OTP already verified for this day"

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

    def verify_otp(
        self,
        session: Session,
        trip_id: int,
        driver_id: int,
        otp_input: str,
        trip_date: Optional[date] = None,
    ) -> Tuple[bool, Optional[str]]:
        if trip_date is None:
            trip_date = today_ist()

        attempts_key = self._attempts_key(trip_id, trip_date, str(driver_id))
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

        db_row = session.exec(
            select(OTPRegistry).where(
                OTPRegistry.trip_id == trip_id,
                OTPRegistry.trip_date == trip_date,
            )
        ).first()

        if not db_row:
            return False, "OTP not generated yet"

        now = now_ist()
        if db_row.valid_from and now < db_row.valid_from:
            return False, "OTP not yet valid"
        if now > db_row.otp_expiry_at:
            return False, "OTP has expired"
        if db_row.verified_at is not None:
            return False, "OTP already used"

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
                self.redis.delete(self._redis_key(trip_id, trip_date))
                self.redis.delete(attempts_key)
            except redis.RedisError:
                pass

        return True, None

    def is_otp_verified(
        self, session: Session, trip_id: int, trip_date: Optional[date] = None
    ) -> bool:
        if trip_date is None:
            trip_date = today_ist()
        row = session.exec(
            select(OTPRegistry).where(
                OTPRegistry.trip_id == trip_id,
                OTPRegistry.trip_date == trip_date,
            )
        ).first()
        return bool(row and row.verified_at is not None)

    def invalidate_otp(self, trip_id: int, trip_date: date):
        if not self.redis:
            return
        try:
            self.redis.delete(self._redis_key(trip_id, trip_date))
        except redis.RedisError:
            pass

    def get_otp_expiry_time(
        self, session: Session, trip_id: int, trip_date: Optional[date] = None
    ) -> Optional[datetime]:
        if trip_date is None:
            trip_date = today_ist()
        row = session.exec(
            select(OTPRegistry).where(
                OTPRegistry.trip_id == trip_id,
                OTPRegistry.trip_date == trip_date,
            )
        ).first()
        return row.otp_expiry_at if row else None
