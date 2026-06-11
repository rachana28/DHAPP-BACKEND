"""Shared SystemConfig access utilities.

``get_config_value`` is the single lookup chain (Redis cache -> SystemConfig DB
row -> hard-coded default) used by every pricing/limits consumer (trip fare
engine, tow pricing, wallet limits). ``daily_job_due`` / ``mark_job_run``
persist per-job last-run markers as SystemConfig rows so daily scheduler jobs
survive server downtime: an interval tick after a restart sees a stale marker
and runs the missed day's work exactly once.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import redis
from sqlmodel import Session

from app.core.models import SystemConfig
from app.utils.time_utils import now_ist


def get_config_value(
    session: Session,
    redis_client: Optional[redis.Redis],
    key: str,
    default: float,
) -> float:
    if redis_client is not None:
        try:
            cached = redis_client.get(f"config:{key}")
            if cached:
                if isinstance(cached, bytes):
                    cached = cached.decode()
                return float(cached)
        except (redis.RedisError, ValueError, TypeError):
            pass
    cfg = session.get(SystemConfig, key)
    if cfg and cfg.value:
        try:
            value = float(cfg.value)
            if redis_client is not None:
                try:
                    redis_client.set(f"config:{key}", cfg.value)
                except redis.RedisError:
                    pass
            return value
        except (TypeError, ValueError):
            pass
    return default


def mark_job_run(session: Session, job_key: str) -> None:
    stamp = now_ist().isoformat()
    cfg = session.get(SystemConfig, job_key)
    if cfg:
        cfg.value = stamp
    else:
        cfg = SystemConfig(
            key=job_key, value=stamp, description="Scheduler last-run marker"
        )
    session.add(cfg)
    session.commit()


def daily_job_due(session: Session, job_key: str, hour: int, minute: int) -> bool:
    now = now_ist()
    due_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    last_due = due_today if now >= due_today else due_today - timedelta(days=1)

    cfg = session.get(SystemConfig, job_key)
    if not cfg or not cfg.value:
        mark_job_run(session, job_key)
        return False
    try:
        last_run = datetime.fromisoformat(cfg.value)
    except (TypeError, ValueError):
        mark_job_run(session, job_key)
        return False
    return last_run < last_due
