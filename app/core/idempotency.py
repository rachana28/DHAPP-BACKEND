"""
Idempotency support for money-touching endpoints (F15).

Clients send an ``Idempotency-Key`` header (UUID); the backend caches the first
successful response in Redis for 10 minutes and returns the cached body on any
replay with the same key. Same key + DIFFERENT request body → 409 Conflict, so
a buggy retry can never silently submit a different charge.

Usage in a FastAPI route:

    from app.core.idempotency import IdempotencyGuard, idempotent

    @router.post("/bill/{bill_id}/pay")
    def pay_bill(..., guard: IdempotencyGuard = Depends(idempotent("bill.pay"))):
        if guard.cached_response is not None:
            return guard.cached_response
        ...
        result = {...}
        guard.store(result)
        return result

If the client omits the header, ``guard`` is inert (no caching, no conflict
checks) and the endpoint behaves exactly as before — row-level locking inside
the service is still the last line of defence against double-charge.

When Redis is unavailable the guard degrades to a no-op for the same reason.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

import redis
from fastapi import Depends, Header, HTTPException, Request

from app.core.database import get_redis

_LOG = logging.getLogger("dhapp.idempotency")

IDEMPOTENCY_TTL_SECONDS = 10 * 60  # 10 min
_KEY_PREFIX = "idem:"


def _hash_body(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class IdempotencyGuard:
    """Per-request handle. Either exposes a cached response or stores a new one."""

    def __init__(
        self,
        redis_client: Optional[redis.Redis],
        cache_key: Optional[str],
        body_hash: Optional[str],
        cached_response: Any = None,
    ):
        self._redis = redis_client
        self._key = cache_key
        self._body_hash = body_hash
        self.cached_response = cached_response

    @property
    def active(self) -> bool:
        return self._redis is not None and self._key is not None

    def store(self, response_payload: Any) -> None:
        """Cache the successful response. Safe to call multiple times — last write wins."""
        if not self.active or self.cached_response is not None:
            return
        try:
            payload = json.dumps(
                {"body_hash": self._body_hash, "response": response_payload},
                default=str,
            )
            self._redis.set(self._key, payload, ex=IDEMPOTENCY_TTL_SECONDS)
        except (redis.RedisError, TypeError) as exc:
            _LOG.warning("idempotency cache write failed (swallowed): %s", exc)


def idempotent(scope: str, path_params: Optional[list[str]] = None):
    """Build a FastAPI dependency that gates an endpoint behind an idempotency key.

    :param scope: Static namespace e.g. ``"bill.pay"`` so two endpoints with the
        same Idempotency-Key from a client don't collide.
    :param path_params: Optional list of path-param names (e.g. ``["bill_id"]``)
        whose values are folded into the cache key. Without this, an endpoint
        keyed only by a path resource (the body being identical across
        resources) would alias one resource's cached response onto another when
        a client reuses the same Idempotency-Key — e.g. paying bill A then bill B
        with the same key returned bill A's response. Including the resource id
        in the cache key scopes the replay to that exact resource. Default
        (None) preserves the original body-only behaviour.
    """

    async def _dep(
        request: Request,
        idempotency_key: Optional[str] = Header(
            default=None, alias="Idempotency-Key", convert_underscores=False
        ),
        redis_client: Optional[redis.Redis] = Depends(get_redis),
    ) -> IdempotencyGuard:
        if not idempotency_key:
            return IdempotencyGuard(redis_client, None, None)
        if redis_client is None:
            return IdempotencyGuard(None, None, None)

        body = await request.body()
        body_hash = _hash_body(body)

        scope_suffix = ""
        if path_params:
            parts = [str(request.path_params.get(name, "")) for name in path_params]
            scope_suffix = ":" + ":".join(parts)
        cache_key = f"{_KEY_PREFIX}{scope}{scope_suffix}:{idempotency_key}"

        try:
            cached_raw = redis_client.get(cache_key)
        except redis.RedisError as exc:
            _LOG.warning("idempotency cache read failed (swallowed): %s", exc)
            return IdempotencyGuard(redis_client, cache_key, body_hash)

        if cached_raw:
            try:
                cached = (
                    json.loads(cached_raw)
                    if isinstance(cached_raw, (str, bytes))
                    else cached_raw
                )
            except (ValueError, TypeError):
                return IdempotencyGuard(redis_client, cache_key, body_hash)

            if cached.get("body_hash") != body_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key reused with a different request body.",
                )

            return IdempotencyGuard(
                redis_client,
                cache_key,
                body_hash,
                cached_response=cached.get("response"),
            )

        return IdempotencyGuard(redis_client, cache_key, body_hash)

    return _dep
