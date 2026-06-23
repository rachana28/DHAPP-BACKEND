"""Durable-vs-ephemeral classification for outgoing push notifications.

Only a curated set of durable notifications is persisted to the server-owned
inbox: promotions/offers, account & security alerts, and support replies.
Everything else — bills/payments, settlements, trip-lifecycle events, provider
job alerts, OTP and live trip pings — is delivery-only and never stored.
"""

from typing import Any, Dict, Optional, Tuple

DURABLE_TYPES: Dict[str, Tuple[str, bool]] = {
    "promotion": ("promotion", True),
    "offer": ("offer", True),
    "account_restricted": ("system", True),
    "driver_suspended": ("system", True),
    "center_member_status": ("system", True),
    "support_message": ("support_message", True),
    "system": ("system", True),
}

_EPHEMERAL_SCREENS = {"otp", "tracking", "payment"}


def classify(data: Optional[Dict[str, Any]]) -> Optional[Tuple[str, bool]]:
    """Return ``(inbox_category, important)`` for a durable push, or ``None``
    when the notification must not be persisted. Any type outside the durable
    allowlist — and anything carrying an OTP/expiry/live-screen marker — is
    treated as ephemeral."""
    if not data:
        return None

    if data.get("otp") or data.get("expires_at"):
        return None
    if data.get("screen") in _EPHEMERAL_SCREENS:
        return None

    return DURABLE_TYPES.get(data.get("type"))
