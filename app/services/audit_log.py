"""
External immutable audit log for trip money + state events (F14).

Events are emitted through :func:`emit_event`. The call is fire-and-forget —
all exceptions are swallowed so an audit failure never unwinds a state change
or a payment.

Sink is selected by the ``AUDIT_LOG_SINK`` env var:

  - ``stdout`` (default): one JSON line per event to stdout via the standard
    logger ``dhapp.audit`` at INFO level. Works out of the box with whatever
    log aggregation the host already runs (Docker logs, journalctl, CloudWatch
    agent tailing stdout, etc.).
  - ``cloudwatch``: send to AWS CloudWatch Logs via boto3 (lazy import so
    boto3 is only required when this mode is on).
  - ``disabled``: no-op. Useful for tests and for installations that don't yet
    want audit output.

Any other value falls back to ``stdout`` so a typo in the env var never breaks
the application.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

from app.utils.time_utils import now_ist

_LOG = logging.getLogger("dhapp.audit")

_SINK = (os.environ.get("AUDIT_LOG_SINK") or "stdout").lower()
_CLOUDWATCH_GROUP = os.environ.get("AUDIT_LOG_CLOUDWATCH_GROUP", "dhapp/trip-audit")
_CLOUDWATCH_STREAM = os.environ.get("AUDIT_LOG_CLOUDWATCH_STREAM", "default")

_cloudwatch_client = None
_cloudwatch_seq_token: Optional[str] = None


def _get_cloudwatch_client():
    global _cloudwatch_client
    if _cloudwatch_client is None:
        import boto3  # lazy

        _cloudwatch_client = boto3.client("logs")
    return _cloudwatch_client


def _write_cloudwatch(payload: Dict[str, Any]) -> None:
    global _cloudwatch_seq_token
    client = _get_cloudwatch_client()
    kwargs = {
        "logGroupName": _CLOUDWATCH_GROUP,
        "logStreamName": _CLOUDWATCH_STREAM,
        "logEvents": [
            {
                "timestamp": int(now_ist().timestamp() * 1000),
                "message": json.dumps(payload, default=str),
            }
        ],
    }
    if _cloudwatch_seq_token:
        kwargs["sequenceToken"] = _cloudwatch_seq_token
    resp = client.put_log_events(**kwargs)
    _cloudwatch_seq_token = resp.get("nextSequenceToken")


def emit_event(
    event_type: str,
    trip_id: Optional[int],
    actor: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    actor_id: Optional[str] = None,
    severity: str = "info",
) -> None:
    """Record one audit event.

    ``event_type`` is a short namespaced id (``trip.state_transition``,
    ``payment.refund``, ``admin.force_state``). ``actor`` is one of ``user``,
    ``driver``, ``admin``, ``system``, ``scheduler``. Keep ``payload`` small
    and free of PII beyond ids already in the system.
    """
    if _SINK == "disabled":
        return

    event = {
        "ts": now_ist().isoformat(),
        "event_type": event_type,
        "trip_id": trip_id,
        "actor": actor,
        "actor_id": actor_id,
        "severity": severity,
        "payload": payload or {},
    }
    try:
        if _SINK == "cloudwatch":
            _write_cloudwatch(event)
        else:
            # stdout / unknown: route via Python logging so log config controls
            # the destination (file, journald, etc.). Falls back to print only
            # if no handlers are attached, which never happens in our app.
            _LOG.info(json.dumps(event, default=str))
    except Exception:
        # Audit failure must never propagate. Best-effort warn so the issue is
        # visible in ops dashboards without blocking the caller.
        try:
            _LOG.warning("audit emit failed for event_type=%s", event_type)
        except Exception:
            pass
