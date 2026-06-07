"""Shared OTP primitives + Redis-primary storage for booking (tow/mechanic) OTPs.

Brings the tow/mechanic ``BookingOTP`` flow up to the same **Redis-primary,
DB-secondary** model the trip ``OTPService`` already uses:

- On generate: the OTP *hash* is written to Redis (key ``otp:booking:{type}:{id}``)
  with a TTL equal to the OTP's validity, AND the ``BookingOTP`` DB row is written
  (secondary / fallback).
- On verify: the hash is read from Redis first; if Redis is down or missing, the
  caller falls back to the DB row.
- On success / expiry: the Redis key is deleted (and the DB row purged by the
  caller / cleanup job). Redis also self-expires via the TTL.

Everything degrades gracefully when Redis is unavailable (``redis_client is None``
or ``redis.RedisError``) — the DB remains a complete source of truth.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Optional

import redis

from app.core.database import redis_client

OTP_LENGTH = 6


# ── primitives (shared) ──────────────────────────────────────────────────────
def generate_numeric_code(length: int = OTP_LENGTH) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def sha256_hex(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


# ── booking (tow/mechanic) Redis layer ───────────────────────────────────────
def booking_key(booking_type: str, booking_id: int) -> str:
    return f"otp:booking:{booking_type}:{booking_id}"


def store_hash(
    booking_type: str, booking_id: int, otp_hash: str, ttl_seconds: int
) -> None:
    """Write the OTP hash to Redis with a TTL. No-op if Redis is unavailable."""
    if redis_client is None or ttl_seconds <= 0:
        return
    try:
        redis_client.set(
            name=booking_key(booking_type, booking_id), value=otp_hash, ex=ttl_seconds
        )
    except redis.RedisError:
        return


def get_hash(booking_type: str, booking_id: int) -> Optional[str]:
    """Read the OTP hash from Redis, or ``None`` on miss / outage."""
    if redis_client is None:
        return None
    try:
        val = redis_client.get(booking_key(booking_type, booking_id))
    except redis.RedisError:
        return None
    if val is None:
        return None
    return val.decode() if isinstance(val, bytes) else val


def delete(booking_type: str, booking_id: int) -> None:
    """Drop the Redis OTP key (on verify success / supersede / expiry)."""
    if redis_client is None:
        return
    try:
        redis_client.delete(booking_key(booking_type, booking_id))
    except redis.RedisError:
        return
