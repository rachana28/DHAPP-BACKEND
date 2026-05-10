"""
Trip-fare pricing engine.

Pulls ALL rates from SystemConfig (admin-editable at runtime via /admin/system-config),
falling back to baked-in defaults when a key is missing. Used by:
  * POST /trips/estimate-fare  — pre-booking quote
  * POST /trips/book-request   — server-side fare set on the new Trip
The same function is the single source of truth for both, so a quote and the
actual charge cannot diverge.

Pricing factors:
  - Hiring type (Daily / Monthly / Outstation) — different base + duration model
  - No. of days + hours/day (parsed from shift_details)
  - Night surcharge if booking_time falls in the configured night window
  - Driver allowance (per-day)
  - Distance (Outstation only)
  - State permit (Outstation only, when start_state != end_state)
  - Tax (% of subtotal)
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import redis
from sqlmodel import Session

from app.core.models import SystemConfig
from app.utils.time_utils import now_ist

# ──────────────────────────────────────────────────────────────────────────────
# SystemConfig keys (admin can override any of these via /admin/system-config)
# ──────────────────────────────────────────────────────────────────────────────
DEFAULTS: Dict[str, float] = {
    # Common
    "pricing_tax_pct": 5.0,
    "pricing_driver_allowance_per_day": 200.0,
    "pricing_night_start_hour": 22.0,  # 22 = 10 PM IST
    "pricing_night_end_hour": 5.0,  # before 5 AM is also "night"
    "pricing_night_surcharge_pct": 25.0,
    # Daily / short-term
    "pricing_short_term_base_fee": 800.0,
    "pricing_short_term_hourly_rate": 150.0,
    # Monthly (recurring → cheaper per day)
    "pricing_monthly_base_fee": 5000.0,
    "pricing_monthly_daily_rate": 700.0,  # for an 8-hour shift
    "pricing_monthly_discount_pct": 10.0,  # vs short-term equivalent
    # Outstation
    "pricing_outstation_base_fee": 1500.0,
    "pricing_outstation_per_km_rate": 12.0,
    "pricing_outstation_daily_rate": 1200.0,
}

# All Indian states + UTs. Admin can change permit fees via:
#   POST /admin/system-config?key=state_permit_<state>&value=<inr>
# State key format:  lower-case, spaces → underscores, ampersands → 'and'
STATE_PERMIT_DEFAULTS: Dict[str, float] = {
    "andhra_pradesh": 500.0,
    "arunachal_pradesh": 600.0,
    "assam": 500.0,
    "bihar": 450.0,
    "chhattisgarh": 450.0,
    "goa": 400.0,
    "gujarat": 550.0,
    "haryana": 450.0,
    "himachal_pradesh": 600.0,
    "jharkhand": 450.0,
    "karnataka": 500.0,
    "kerala": 500.0,
    "madhya_pradesh": 500.0,
    "maharashtra": 650.0,
    "manipur": 550.0,
    "meghalaya": 550.0,
    "mizoram": 550.0,
    "nagaland": 550.0,
    "odisha": 450.0,
    "punjab": 450.0,
    "rajasthan": 550.0,
    "sikkim": 600.0,
    "tamil_nadu": 500.0,
    "telangana": 500.0,
    "tripura": 500.0,
    "uttar_pradesh": 450.0,
    "uttarakhand": 500.0,
    "west_bengal": 500.0,
    # Union Territories
    "andaman_and_nicobar_islands": 700.0,
    "chandigarh": 400.0,
    "dadra_and_nagar_haveli_and_daman_and_diu": 450.0,
    "delhi": 500.0,
    "jammu_and_kashmir": 600.0,
    "ladakh": 700.0,
    "lakshadweep": 700.0,
    "puducherry": 400.0,
}

DEFAULT_PERMIT = 500.0

# Display names — sorted longest-first so substring matching prefers the most-specific
# (e.g. "Andhra Pradesh" before "Andhra"; "Jammu and Kashmir" before "Kashmir").
INDIAN_STATE_DISPLAY_NAMES: List[str] = sorted(
    [
        # 28 States
        "Andhra Pradesh",
        "Arunachal Pradesh",
        "Assam",
        "Bihar",
        "Chhattisgarh",
        "Goa",
        "Gujarat",
        "Haryana",
        "Himachal Pradesh",
        "Jharkhand",
        "Karnataka",
        "Kerala",
        "Madhya Pradesh",
        "Maharashtra",
        "Manipur",
        "Meghalaya",
        "Mizoram",
        "Nagaland",
        "Odisha",
        "Punjab",
        "Rajasthan",
        "Sikkim",
        "Tamil Nadu",
        "Telangana",
        "Tripura",
        "Uttar Pradesh",
        "Uttarakhand",
        "West Bengal",
        # 8 Union Territories
        "Andaman and Nicobar Islands",
        "Chandigarh",
        "Dadra and Nagar Haveli and Daman and Diu",
        "Delhi",
        "Jammu and Kashmir",
        "Ladakh",
        "Lakshadweep",
        "Puducherry",
    ],
    key=len,
    reverse=True,
)


def extract_state_from_location(location: Optional[str]) -> Optional[str]:
    """
    Best-effort: pull an Indian state/UT name out of a free-form location string.
    The user app already sends start_location / end_location, so a separate
    state payload field is unnecessary.

    (P3 fix: Use word-boundary matching to avoid substring collisions like "Goa" → "Goalkeeper".)

    Accepts:
      - "Chennai, Tamil Nadu"               → "tamil_nadu"
      - "Bangalore, Karnataka, India"       → "karnataka"
      - "13.08,80.27 (Chennai, Tamil Nadu)" → "tamil_nadu"
      - "Mumbai"                            → None  (no state name in string)
    """
    if not location:
        return None

    haystack = location.lower()

    # Word-boundary regex: match state names surrounded by word boundaries or punctuation/spaces
    for display in INDIAN_STATE_DISPLAY_NAMES:
        # Escape the state name and use word boundaries
        pattern = r"\b" + re.escape(display.lower()) + r"\b"
        if re.search(pattern, haystack):
            return _state_key(display)

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Config lookup
# ──────────────────────────────────────────────────────────────────────────────
def _get_config_value(
    session: Session,
    redis_client: Optional[redis.Redis],
    key: str,
    default: float,
) -> float:
    """Lookup chain: Redis cache → SystemConfig DB row → hard-coded default."""
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


def _state_key(state: str) -> str:
    return state.strip().lower().replace("&", "and").replace(" ", "_")


def _state_permit(
    session: Session, redis_client: Optional[redis.Redis], state: str
) -> float:
    norm = _state_key(state)
    return _get_config_value(
        session,
        redis_client,
        f"state_permit_{norm}",
        STATE_PERMIT_DEFAULTS.get(norm, DEFAULT_PERMIT),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Distance from coords
# ──────────────────────────────────────────────────────────────────────────────
# State extraction lives in extract_state_from_location() above (uses the
# INDIAN_STATE_DISPLAY_NAMES list).
def _state_display_name(key: Optional[str]) -> Optional[str]:
    """Reverse the canonical key back to its display name."""
    if not key:
        return None
    for display in INDIAN_STATE_DISPLAY_NAMES:
        if _state_key(display) == key:
            return display
    return None


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in kilometres between two lat/lng points."""
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2.0) ** 2
    )
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _resolve_distance_km(
    distance_km: Optional[float],
    start_lat: Optional[float],
    start_lng: Optional[float],
    end_lat: Optional[float],
    end_lng: Optional[float],
) -> Optional[float]:
    """
    If user supplied `distance_km`, trust it. Otherwise compute Haversine when
    all four coords are present. Otherwise return None.
    """
    if distance_km is not None and distance_km > 0:
        return distance_km
    if (
        start_lat is not None
        and start_lng is not None
        and end_lat is not None
        and end_lng is not None
    ):
        return round(_haversine_km(start_lat, start_lng, end_lat, end_lng), 2)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _parse_hours_per_day(shift_details: Optional[str]) -> int:
    """Parse '8 Hours (15:00)' → 8. Defaults to 8 if not parseable."""
    if not shift_details:
        return 8
    parts = shift_details.split()
    for i, tok in enumerate(parts):
        if tok.lower() == "hours" and i > 0:
            try:
                return int(parts[i - 1])
            except ValueError:
                break
    return 8


def _num_days(
    hiring_type: str,
    start_date: Optional[date],
    end_date: Optional[date],
    months: Optional[int],
    selected_days: Optional[str],
) -> int:
    """
    Number of billable days. For Monthly with selected_days, counts only the chosen
    weekdays in the date range so monthly bookings don't over-count Sundays etc.
    """
    if not start_date or not end_date:
        if months and hiring_type.lower() == "monthly":
            return months * 30
        return 1

    total_calendar_days = (end_date - start_date).days + 1
    if total_calendar_days <= 0:
        return 1

    if not selected_days:
        return total_calendar_days

    # Map tokens → weekday index (Mon=0..Sun=6)
    token_map = {
        "mon": 0,
        "monday": 0,
        "tue": 1,
        "tues": 1,
        "tuesday": 1,
        "wed": 2,
        "wednesday": 2,
        "thu": 3,
        "thur": 3,
        "thurs": 3,
        "thursday": 3,
        "fri": 4,
        "friday": 4,
        "sat": 5,
        "saturday": 5,
        "sun": 6,
        "sunday": 6,
    }
    weekday_filter = set()
    for tok in selected_days.replace("/", ",").replace("|", ",").split(","):
        wd = token_map.get(tok.strip().lower())
        if wd is not None:
            weekday_filter.add(wd)
    if not weekday_filter:
        return total_calendar_days

    cur = start_date
    count = 0
    from datetime import timedelta

    while cur <= end_date:
        if cur.weekday() in weekday_filter:
            count += 1
        cur += timedelta(days=1)
    return max(count, 1)


def _is_night(booking_time: datetime, night_start: int, night_end: int) -> bool:
    """Wraps midnight: 'night' = [night_start, 24) ∪ [0, night_end)."""
    h = booking_time.hour
    if night_start <= night_end:
        return night_start <= h < night_end
    return h >= night_start or h < night_end


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────
def calculate_fare(
    session: Session,
    redis_client: Optional[redis.Redis],
    *,
    hiring_type: str,
    vehicle_type: Optional[str] = None,
    shift_details: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    months: Optional[int] = None,
    selected_days: Optional[str] = None,
    start_location: Optional[str] = None,
    end_location: Optional[str] = None,
    start_lat: Optional[float] = None,
    start_lng: Optional[float] = None,
    end_lat: Optional[float] = None,
    end_lng: Optional[float] = None,
    distance_km: Optional[float] = None,
    booking_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Compute the total fare and per-component breakdown.

    Returns a dict shaped like:
      {
        "total":      3622.50,
        "subtotal":   3450.00,
        "tax":         172.50,
        "currency":    "INR",
        "components": [
            {"name": "Base Fee",                "amount": 1500.00},
            {"name": "Duration Charges (3 days x 8h)", "amount": 1200.00},
            ...
        ],
        "meta": { "hiring_type": ..., "num_days": ..., "hours_per_day": ..., "is_night": ... },
      }
    """
    htype = (hiring_type or "").strip().lower()
    booking_time = booking_time or now_ist()

    def cfg(k):
        return _get_config_value(session, redis_client, k, DEFAULTS[k])

    num_days = _num_days(htype, start_date, end_date, months, selected_days)
    hours_per_day = _parse_hours_per_day(shift_details)
    components: List[Dict[str, Any]] = []
    subtotal = 0.0

    # Resolve geographic info (Outstation only needs it, but compute always so meta is rich)
    resolved_distance_km = _resolve_distance_km(
        distance_km, start_lat, start_lng, end_lat, end_lng
    )
    start_state_key = extract_state_from_location(start_location)
    end_state_key = extract_state_from_location(end_location)

    # ── Hiring-type-specific base + duration ─────────────────────────────────
    if htype == "monthly":
        base_fee = cfg("pricing_monthly_base_fee")
        # daily_rate scales by hours/day relative to a baseline 8-hour shift
        per_day = cfg("pricing_monthly_daily_rate") * (hours_per_day / 8.0)
        duration_charge = round(per_day * num_days, 2)
        components.append({"name": "Base Fee (Monthly)", "amount": round(base_fee, 2)})
        components.append(
            {
                "name": f"Duration Charges ({num_days} days x {hours_per_day}h, monthly rate)",
                "amount": duration_charge,
            }
        )
        subtotal += base_fee + duration_charge

        # Recurring discount (vs short-term)
        discount_pct = cfg("pricing_monthly_discount_pct")
        if discount_pct > 0:
            discount = round((base_fee + duration_charge) * (discount_pct / 100.0), 2)
            components.append(
                {
                    "name": f"Monthly Discount ({discount_pct:.0f}%)",
                    "amount": -discount,
                }
            )
            subtotal -= discount

    elif htype == "outstation":
        base_fee = cfg("pricing_outstation_base_fee")
        per_day = cfg("pricing_outstation_daily_rate") * (hours_per_day / 8.0)
        duration_charge = round(per_day * num_days, 2)
        per_km = cfg("pricing_outstation_per_km_rate")
        effective_distance = resolved_distance_km or 0.0
        distance_charge = round(effective_distance * per_km, 2)

        components.append(
            {"name": "Base Fee (Outstation)", "amount": round(base_fee, 2)}
        )
        components.append(
            {
                "name": f"Duration Charges ({num_days} days x {hours_per_day}h)",
                "amount": duration_charge,
            }
        )
        components.append(
            {
                "name": f"Distance Charges ({effective_distance:.1f} km x ₹{per_km:.2f})",
                "amount": distance_charge,
            }
        )
        subtotal += base_fee + duration_charge + distance_charge

        # State permit when crossing state lines (resolved from end_location)
        if start_state_key and end_state_key and start_state_key != end_state_key:
            end_state_display = _state_display_name(end_state_key) or end_state_key
            permit = round(_state_permit(session, redis_client, end_state_key), 2)
            components.append(
                {
                    "name": f"State Permit ({end_state_display})",
                    "amount": permit,
                }
            )
            subtotal += permit

    else:
        # Default: short-term / Daily
        base_fee = cfg("pricing_short_term_base_fee")
        hourly = cfg("pricing_short_term_hourly_rate")
        duration_charge = round(hourly * hours_per_day * num_days, 2)
        components.append(
            {"name": "Base Fee (Short-term)", "amount": round(base_fee, 2)}
        )
        components.append(
            {
                "name": f"Duration Charges ({num_days} days x {hours_per_day}h x ₹{hourly:.0f}/h)",
                "amount": duration_charge,
            }
        )
        subtotal += base_fee + duration_charge

    # ── Driver allowance (always applies) ────────────────────────────────────
    allowance = round(cfg("pricing_driver_allowance_per_day") * num_days, 2)
    if allowance > 0:
        components.append(
            {
                "name": f"Driver Allowance ({num_days} days)",
                "amount": allowance,
            }
        )
        subtotal += allowance

    # ── Night surcharge (booking falls in night window) ──────────────────────
    night_start = int(cfg("pricing_night_start_hour"))
    night_end = int(cfg("pricing_night_end_hour"))
    is_night = _is_night(booking_time, night_start, night_end)
    if is_night:
        pct = cfg("pricing_night_surcharge_pct")
        surcharge = round(subtotal * (pct / 100.0), 2)
        if surcharge > 0:
            components.append(
                {
                    "name": f"Night Surcharge ({pct:.0f}% — booked after {night_start:02d}:00)",
                    "amount": surcharge,
                }
            )
            subtotal += surcharge

    # ── Tax ──────────────────────────────────────────────────────────────────
    tax_pct = cfg("pricing_tax_pct")
    tax = round(subtotal * (tax_pct / 100.0), 2)
    if tax > 0:
        components.append({"name": f"Tax ({tax_pct:.1f}%)", "amount": tax})

    total = round(subtotal + tax, 2)

    return {
        "total": total,
        "subtotal": round(subtotal, 2),
        "tax": tax,
        "currency": "INR",
        "components": components,
        "meta": {
            "hiring_type": hiring_type,
            "vehicle_type": vehicle_type,
            "num_days": num_days,
            "hours_per_day": hours_per_day,
            "distance_km": resolved_distance_km,
            "is_night_booking": is_night,
            # Resolved from end_location / start_location text. Null if not parseable.
            "start_state": _state_display_name(start_state_key),
            "end_state": _state_display_name(end_state_key),
            "cross_state": bool(
                start_state_key and end_state_key and start_state_key != end_state_key
            ),
        },
    }


def validate_pricing_inputs(
    *,
    hiring_type: str,
    distance_km: Optional[float],
    start_lat: Optional[float] = None,
    start_lng: Optional[float] = None,
    end_lat: Optional[float] = None,
    end_lng: Optional[float] = None,
    end_location: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Reject the request early if required outstation fields are missing.
    Outstation needs:
      - either `distance_km` OR all four start/end coords (so distance can be computed)
      - `end_location` text (used to resolve the destination state for permit fee).
    """
    htype = (hiring_type or "").strip().lower()
    if htype != "outstation":
        return True, None

    has_coords = all(v is not None for v in (start_lat, start_lng, end_lat, end_lng))
    has_distance = distance_km is not None and distance_km > 0
    if not has_distance and not has_coords:
        return (
            False,
            "Outstation booking needs distance_km, or all four of "
            "start_lat/start_lng/end_lat/end_lng.",
        )
    if not end_location:
        return (
            False,
            "Outstation booking needs end_location (used to derive the destination state).",
        )
    return True, None
