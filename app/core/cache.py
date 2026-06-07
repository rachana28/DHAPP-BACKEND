"""Tiny Redis JSON-cache helper.

Thin, defensive wrapper over the existing **sync** ``redis_client`` from
``app.core.database``. Every call degrades gracefully:

- When Redis is not configured / unreachable (``redis_client is None``) or a
  ``redis.RedisError`` is raised, reads return ``None`` and writes are no-ops.
  A cache outage must NEVER break a request — callers always fall back to the DB.

Used for:
- ``/me`` profile GETs (TTL ~5 min, invalidated on profile writes).
- Polled active-booking aggregates (short TTL, absorbs poll storms).

Values are JSON. Pydantic/SQLModel objects should be passed already dumped via
``model_dump(mode="json")`` (or a plain dict/list) so they serialize cleanly.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

import redis

from app.core.database import redis_client

# --- Default TTLs (seconds) ---
ME_CACHE_TTL = 300  # profile /me — rarely changes between writes
ACTIVE_CACHE_TTL = 5  # polled active-booking aggregates — collapse poll bursts


# ── key builders ─────────────────────────────────────────────────────────────
def me_key(role: str, user_id: Any) -> str:
    return f"me:{role}:{user_id}"


def active_key(scope: str, owner_id: Any) -> str:
    """``scope`` is a role/service tag (e.g. ``user``, ``tow_driver``)."""
    return f"active:{scope}:{owner_id}"


# ── primitives ───────────────────────────────────────────────────────────────
def cache_get_json(key: str) -> Optional[Any]:
    """Return the decoded JSON value for ``key`` or ``None`` (miss / no Redis)."""
    if redis_client is None:
        return None
    try:
        raw = redis_client.get(key)
    except redis.RedisError:
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def cache_set_json(key: str, value: Any, ttl: int) -> None:
    """Store ``value`` (JSON-serializable) under ``key`` with a TTL. No-op on failure."""
    if redis_client is None:
        return
    try:
        redis_client.set(name=key, value=json.dumps(value, default=str), ex=ttl)
    except (redis.RedisError, TypeError, ValueError):
        return


def cache_delete(key: str) -> None:
    """Delete a single key. No-op on failure."""
    if redis_client is None:
        return
    try:
        redis_client.delete(key)
    except redis.RedisError:
        return


def cache_delete_many(keys: Iterable[str]) -> None:
    """Delete several keys in one round-trip. No-op on failure."""
    if redis_client is None:
        return
    keys = [k for k in keys if k]
    if not keys:
        return
    try:
        redis_client.delete(*keys)
    except redis.RedisError:
        return
