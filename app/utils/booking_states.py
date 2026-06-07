"""Single source of truth for which booking statuses are *active* (non-terminal).

Used by the user-app **aggregate active-bookings** endpoint (``GET /bookings/active``)
so the home screen can be served the live set server-side instead of fetching the
whole history and filtering on the client.

"Active" = everything a customer is still tracking, i.e. NOT a terminal state.
Includes ``searching`` (still finding a provider) per product decision.

The driver-app active endpoints instead reuse the existing *engaged* constants
already defined next to their allocation logic
(``TripService.DRIVER_BUSY_STATES``, ``geo.TOW_ACTIVE_STATES``,
``geo.MECHANIC_ACTIVE_STATES``) — a provider only cares about jobs assigned to
them, which is a stricter set than "non-terminal".
"""

from __future__ import annotations

# ── Terminal (closed) states per service ─────────────────────────────────────
TRIP_TERMINAL_STATES = (
    "completed",
    "billed",
    "settled",
    "cancelled_by_user",
    "cancelled_by_driver",
    "skipped_by_driver",
    "no_drivers_found",
    "rejected",
)
TOW_TERMINAL_STATES = ("completed", "cancelled", "no_drivers_found")
MECHANIC_TERMINAL_STATES = ("completed", "cancelled", "no_mechanics_found", "rejected")
SERVICE_TERMINAL_STATES = ("completed", "cancelled")

# ── Active (non-terminal) states — used for `status IN (...)` filters ─────────
TRIP_ACTIVE_STATES = (
    "searching",
    "accepted_pending_payment",
    "payment_in_progress",
    "payment_failed",
    "active_pending_otp",
    "active",
    "ongoing",
    "paused",
    "paused_payment",
    "cancellation_pending_payment",
)
TOW_ACTIVE_STATES = (
    "searching",
    "pending",
    "accepted",
    "arrived",
    "in_progress",
    "near_destination",
)
MECHANIC_ACTIVE_STATES = (
    "searching",
    "accepted",
    "arrived",
    "in_progress",
    "pending_approval",
)
SERVICE_ACTIVE_STATES = (
    "searching",
    "pending_confirmation",
    "booked",
    "accepted",
    "checked_in",
    "service_ongoing",
    "service_accepted",
    "in_service",
)

# ── Driver/provider "engaged" states for the service-center active endpoint ──
# (trips/tow/mechanic driver endpoints reuse their existing local constants).
SERVICE_CENTER_ENGAGED_STATES = (
    "pending_confirmation",
    "booked",
    "accepted",
    "checked_in",
    "service_ongoing",
    "service_accepted",
    "in_service",
)
