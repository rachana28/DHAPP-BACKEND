"""MQTT topic conventions for per-ride telemetry.

Single source of truth shared by the acceptance endpoints (which advertise the
topic to clients) and the telemetry worker (which parses it). Keeping the format
here prevents the publish/subscribe sides from drifting apart.

    rides/{booking_reference_id}/telemetry
"""

from typing import Optional

TELEMETRY_ROOT = "rides"
TELEMETRY_LEAF = "telemetry"

# Wildcard the worker subscribes to (single-level '+').
TELEMETRY_WILDCARD = f"{TELEMETRY_ROOT}/+/{TELEMETRY_LEAF}"


def telemetry_topic(booking_reference_id: str) -> str:
    """Topic a client publishes/subscribes to for a specific booking."""
    return f"{TELEMETRY_ROOT}/{booking_reference_id}/{TELEMETRY_LEAF}"


def parse_telemetry_topic(topic: str) -> Optional[str]:
    """Extract the booking reference id from a telemetry topic, or None."""
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == TELEMETRY_ROOT and parts[2] == TELEMETRY_LEAF:
        return parts[1]
    return None
