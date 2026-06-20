"""Redis-backed fixed-window rate limiting exposed as FastAPI route dependencies.

Replaces the unmaintained fastapi-limiter, which breaks on FastAPI >= 0.137. A
single Lua script does the atomic increment-and-expire per caller so limits hold
consistently across workers. ``RateLimiterRegistry.init`` must run once at
application startup with an async Redis client; ``RateLimiter`` is then used as
``Depends(RateLimiter(times=..., seconds=...))`` and raises HTTP 429 with a
``Retry-After`` header once a caller exceeds its quota.

The caller is identified by client IP by default. Because the app runs behind a
managed proxy, the IP is taken from the ``X-Forwarded-For`` hop appended by the
proxy (``TRUSTED_PROXY_HOPS``, counted from the right) so a client-supplied header
cannot spoof a fresh bucket. An ``identifier`` callable lets an endpoint key on a
resource instead (e.g. a phone number), and ``scope`` namespaces independent
limits on the same route. A missing registry (startup misconfiguration) fails
loud; a Redis outage honours each limiter's ``fail_open`` flag (default allow, so
limiting never becomes a single point of failure, while cost-sensitive endpoints
can opt into fail-closed).
"""

from __future__ import annotations

import inspect
import logging
import math
import os
from typing import Awaitable, Callable, Optional, Union

from fastapi import HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import NoScriptError, RedisError

logger = logging.getLogger(__name__)

TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "1"))

Identifier = Callable[[Request], Union[Optional[str], Awaitable[Optional[str]]]]

_LUA_FIXED_WINDOW = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
if current > tonumber(ARGV[2]) then
    local ttl = redis.call('PTTL', KEYS[1])
    if ttl < 0 then
        redis.call('PEXPIRE', KEYS[1], ARGV[1])
        ttl = tonumber(ARGV[1])
    end
    return ttl
end
return 0
"""


class RateLimiterRegistry:
    redis: Optional[Redis] = None
    prefix: str = "dhapp-rl"
    _script_sha: Optional[str] = None

    @classmethod
    async def init(cls, redis: Redis, prefix: str = "dhapp-rl") -> None:
        cls.redis = redis
        cls.prefix = prefix
        cls._script_sha = await redis.script_load(_LUA_FIXED_WINDOW)

    @classmethod
    async def close(cls) -> None:
        cls.redis = None
        cls._script_sha = None

    @classmethod
    async def hit(cls, key: str, limit: int, window_ms: int) -> int:
        if cls.redis is None:
            raise RuntimeError(
                "RateLimiterRegistry.init must be called during application startup."
            )
        args = (1, key, str(window_ms), str(limit))
        try:
            if cls._script_sha is not None:
                return int(await cls.redis.evalsha(cls._script_sha, *args))
            return int(await cls.redis.eval(_LUA_FIXED_WINDOW, *args))
        except NoScriptError:
            cls._script_sha = await cls.redis.script_load(_LUA_FIXED_WINDOW)
            return int(await cls.redis.evalsha(cls._script_sha, *args))


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded and TRUSTED_PROXY_HOPS > 0:
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if len(hops) >= TRUSTED_PROXY_HOPS:
            return hops[-TRUSTED_PROXY_HOPS]
    client = request.client
    return client.host if client else "anonymous"


class RateLimiter:
    def __init__(
        self,
        times: int = 1,
        milliseconds: int = 0,
        seconds: int = 0,
        minutes: int = 0,
        hours: int = 0,
        scope: Optional[str] = None,
        identifier: Optional[Identifier] = None,
        fail_open: bool = True,
    ) -> None:
        self.times = times
        self.window_ms = (
            milliseconds + 1000 * seconds + 60000 * minutes + 3600000 * hours
        )
        self.scope = scope
        self.identifier = identifier
        self.fail_open = fail_open
        if self.times < 1 or self.window_ms < 1:
            raise ValueError(
                "RateLimiter requires times >= 1 and a positive time window."
            )

    async def _subject(self, request: Request) -> str:
        if self.identifier is not None:
            value = self.identifier(request)
            if inspect.isawaitable(value):
                value = await value
            if value:
                return str(value)
        return _client_ip(request)

    async def __call__(self, request: Request) -> None:
        scope = self.scope or request.url.path
        key = (
            f"{RateLimiterRegistry.prefix}:{scope}:{await self._subject(request)}"
            f":{self.times}:{self.window_ms}"
        )
        try:
            retry_ms = await RateLimiterRegistry.hit(key, self.times, self.window_ms)
        except RedisError:
            if self.fail_open:
                logger.warning(
                    "Rate limiter backend unavailable; allowing request to %s",
                    request.url.path,
                    exc_info=True,
                )
                return
            raise HTTPException(
                status_code=503,
                detail="Service temporarily unavailable. Please try again shortly.",
            )
        if retry_ms > 0:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please try again later.",
                headers={"Retry-After": str(max(1, math.ceil(retry_ms / 1000)))},
            )
