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
  - Night surcharge if the SHIFT start time falls in the configured night window
    (falls back to booking_time only when shift_details has no clock bracket)
  - Driver allowance (per-day)
  - Distance (Outstation only)
  - State permit (Outstation only, when start_state != end_state)
  - Tax (% of subtotal)
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import redis
from sqlmodel import Session

from app.utils.system_config import get_config_value
from app.utils.time_utils import now_ist

DEFAULTS: Dict[str, float] = {
    "pricing_tax_pct": 5.0,
    "pricing_night_start_hour": 22.0,
    "pricing_night_end_hour": 5.0,
    "pricing_night_surcharge_pct": 25.0,
    "pricing_outstation_night_charge_pct": 25.0,
    "pricing_driver_allowance_per_day": 500.0,
    "pricing_short_term_driver_allowance_per_day": 200.0,
    "pricing_monthly_driver_allowance_per_day": 200.0,
    "pricing_short_term_base_fee": 800.0,
    "pricing_short_term_base_fee_sedan": 800.0,
    "pricing_short_term_base_fee_suv": 1000.0,
    "pricing_short_term_base_fee_hatchback": 700.0,
    "pricing_short_term_base_fee_luxury": 2000.0,
    "pricing_short_term_hourly_rate": 150.0,
    "pricing_monthly_base_fee": 5000.0,
    "pricing_monthly_base_fee_sedan": 5000.0,
    "pricing_monthly_base_fee_suv": 6500.0,
    "pricing_monthly_base_fee_hatchback": 4500.0,
    "pricing_monthly_base_fee_luxury": 12000.0,
    "pricing_monthly_daily_rate": 700.0,
    "pricing_monthly_discount_pct": 10.0,
    "pricing_outstation_per_km_rate": 12.0,
    "pricing_outstation_per_km_rate_sedan": 12.0,
    "pricing_outstation_per_km_rate_suv": 16.0,
    "pricing_outstation_per_km_rate_hatchback": 10.0,
    "pricing_outstation_per_km_rate_luxury": 25.0,
    "pricing_outstation_per_km_rate_tempo": 18.0,
    "pricing_outstation_per_km_rate_minibus": 28.0,
    "pricing_outstation_per_km_rate_bus": 40.0,
}


def _veh_key(vehicle_type: Optional[str]) -> str:
    if not vehicle_type:
        return ""
    return vehicle_type.strip().lower().replace(" ", "_").replace("-", "_")


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

INDIAN_STATE_DISPLAY_NAMES: List[str] = sorted(
    [
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
    if not location:
        return None

    haystack = location.lower()

    for display in INDIAN_STATE_DISPLAY_NAMES:
        pattern = r"\b" + re.escape(display.lower()) + r"\b"
        if re.search(pattern, haystack):
            return _state_key(display)

    return None


def _reverse_geocode_state(
    lat: Optional[float], lng: Optional[float], timeout_sec: float = 3.0
) -> Optional[str]:
    if lat is None or lng is None:
        return None
    try:
        import requests

        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "lat": lat,
                "lon": lng,
                "format": "json",
                "zoom": 5,
                "addressdetails": 1,
            },
            headers={"User-Agent": "dhapp-backend/state-resolver"},
            timeout=timeout_sec,
        )
        if resp.status_code != 200:
            return None
        data = resp.json() or {}
        state_name = (data.get("address") or {}).get("state")
        if not state_name:
            return None
        return extract_state_from_location(state_name)
    except Exception:
        return None


def resolve_end_state(
    end_location: Optional[str],
    end_lat: Optional[float],
    end_lng: Optional[float],
    session: Optional[Session] = None,
    redis_client: Optional[redis.Redis] = None,
) -> Optional[str]:
    key = extract_state_from_location(end_location)
    if key:
        return key
    if session is not None:
        enabled = get_config_value(session, redis_client, "enable_reverse_geocode", 1.0)
        if enabled <= 0:
            return None
    return _reverse_geocode_state(end_lat, end_lng)


def _state_key(state: str) -> str:
    return state.strip().lower().replace("&", "and").replace(" ", "_")


def _state_permit(
    session: Session, redis_client: Optional[redis.Redis], state: str
) -> float:
    norm = _state_key(state)
    return get_config_value(
        session,
        redis_client,
        f"state_permit_{norm}",
        STATE_PERMIT_DEFAULTS.get(norm, DEFAULT_PERMIT),
    )


def _state_display_name(key: Optional[str]) -> Optional[str]:
    if not key:
        return None
    for display in INDIAN_STATE_DISPLAY_NAMES:
        if _state_key(display) == key:
            return display
    return None


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
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


def _parse_hours_per_day(shift_details: Optional[str]) -> int:
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


_SHIFT_START_RE = re.compile(r"\((\d{1,2}):(\d{2})\)")


def _parse_shift_start_hour(shift_details: Optional[str]) -> Optional[int]:
    if not shift_details:
        return None
    m = _SHIFT_START_RE.search(shift_details)
    if not m:
        return None
    try:
        h = int(m.group(1))
        if 0 <= h <= 23:
            return h
    except ValueError:
        pass
    return None


_DAY_TOKEN_MAP = {
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

MONTHLY_MIN_DAYS_PER_MONTH = 4
MAX_DAYS_PER_MONTH = 21
OUTSTATION_MIN_DAYS = 1
OUTSTATION_MAX_DAYS = 10


def _weekday_filter(selected_days: Optional[str]) -> Optional[set]:
    if not selected_days:
        return None
    weekday_filter = set()
    for tok in selected_days.replace("/", ",").replace("|", ",").split(","):
        wd = _DAY_TOKEN_MAP.get(tok.strip().lower())
        if wd is not None:
            weekday_filter.add(wd)
    return weekday_filter or None


def validate_day_counts(
    *,
    hiring_type: str,
    start_date: Optional[date],
    end_date: Optional[date],
    selected_days: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    if not start_date or not end_date:
        return True, None
    if end_date < start_date:
        return False, "end_date cannot be before start_date."

    htype = (hiring_type or "").strip().lower()
    span = (end_date - start_date).days + 1

    if htype == "outstation":
        if span < OUTSTATION_MIN_DAYS or span > OUTSTATION_MAX_DAYS:
            return (
                False,
                f"Outstation bookings must span between {OUTSTATION_MIN_DAYS} and "
                f"{OUTSTATION_MAX_DAYS} calendar days (got {span}).",
            )
        return True, None

    weekday_filter = _weekday_filter(selected_days)
    per_month: Dict[str, int] = {}
    cur = start_date
    while cur <= end_date:
        if weekday_filter is None or cur.weekday() in weekday_filter:
            key = f"{cur.year:04d}-{cur.month:02d}"
            per_month[key] = per_month.get(key, 0) + 1
        cur += timedelta(days=1)

    if htype == "monthly":
        y, m = start_date.year, start_date.month
        while (y, m) <= (end_date.year, end_date.month):
            key = f"{y:04d}-{m:02d}"
            count = per_month.get(key, 0)
            if count < MONTHLY_MIN_DAYS_PER_MONTH or count > MAX_DAYS_PER_MONTH:
                return (
                    False,
                    f"Monthly bookings need {MONTHLY_MIN_DAYS_PER_MONTH}-"
                    f"{MAX_DAYS_PER_MONTH} scheduled days in every calendar "
                    f"month: {key} has {count}.",
                )
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return True, None

    for key, count in per_month.items():
        if count > MAX_DAYS_PER_MONTH:
            return (
                False,
                f"Short-term bookings allow at most {MAX_DAYS_PER_MONTH} "
                f"scheduled days in a calendar month: {key} has {count}.",
            )
    return True, None


def _num_days(
    hiring_type: str,
    start_date: Optional[date],
    end_date: Optional[date],
    months: Optional[int],
    selected_days: Optional[str],
) -> int:
    if not start_date or not end_date:
        if months and hiring_type.lower() == "monthly":
            return months * 30
        return 1

    total_calendar_days = (end_date - start_date).days + 1
    if total_calendar_days <= 0:
        return 1

    weekday_filter = _weekday_filter(selected_days)
    if weekday_filter is None:
        return total_calendar_days

    cur = start_date
    count = 0
    while cur <= end_date:
        if cur.weekday() in weekday_filter:
            count += 1
        cur += timedelta(days=1)
    return max(count, 1)


def _is_night_hour(hour: int, night_start: int, night_end: int) -> bool:
    if night_start <= night_end:
        return night_start <= hour < night_end
    return hour >= night_start or hour < night_end


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
    htype = (hiring_type or "").strip().lower()
    booking_time = booking_time or now_ist()

    def cfg(k):
        return get_config_value(session, redis_client, k, DEFAULTS[k])

    def cfg_for_vehicle(base_key: str) -> float:
        veh = _veh_key(vehicle_type)
        if veh:
            specific_key = f"{base_key}_{veh}"
            default = DEFAULTS.get(specific_key, DEFAULTS.get(base_key, 0.0))
            return get_config_value(session, redis_client, specific_key, default)
        return get_config_value(
            session, redis_client, base_key, DEFAULTS.get(base_key, 0.0)
        )

    num_days = _num_days(htype, start_date, end_date, months, selected_days)
    hours_per_day = _parse_hours_per_day(shift_details)
    components: List[Dict[str, Any]] = []
    subtotal = 0.0

    resolved_distance_km = _resolve_distance_km(
        distance_km, start_lat, start_lng, end_lat, end_lng
    )
    start_state_key = extract_state_from_location(start_location)
    end_state_key = resolve_end_state(
        end_location, end_lat, end_lng, session=session, redis_client=redis_client
    )

    if htype == "monthly":
        base_fee = cfg_for_vehicle("pricing_monthly_base_fee")
        per_day = cfg("pricing_monthly_daily_rate") * (hours_per_day / 8.0)
        duration_charge = round(per_day * num_days, 2)
        components.append(
            {
                "name": f"Base Fee (Monthly, {vehicle_type or 'default'})",
                "amount": round(base_fee, 2),
            }
        )
        components.append(
            {
                "name": f"Duration Charges ({num_days} working days x {hours_per_day}h, monthly rate)",
                "amount": duration_charge,
            }
        )
        subtotal += base_fee + duration_charge

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
        per_km = cfg_for_vehicle("pricing_outstation_per_km_rate")
        effective_distance = resolved_distance_km or 0.0
        distance_charge = round(effective_distance * per_km, 2)

        components.append(
            {
                "name": f"Distance Charges ({effective_distance:.1f} km x ₹{per_km:.2f}/km, {vehicle_type or 'default'})",
                "amount": distance_charge,
            }
        )
        subtotal += distance_charge

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
        base_fee = cfg_for_vehicle("pricing_short_term_base_fee")
        hourly = cfg("pricing_short_term_hourly_rate")
        duration_charge = round(hourly * hours_per_day * num_days, 2)
        components.append(
            {
                "name": f"Base Fee (Short-term, {vehicle_type or 'default'})",
                "amount": round(base_fee, 2),
            }
        )
        components.append(
            {
                "name": f"Duration Charges ({num_days} days x {hours_per_day}h x ₹{hourly:.0f}/h)",
                "amount": duration_charge,
            }
        )
        subtotal += base_fee + duration_charge

    if htype == "monthly":
        allowance_key = "pricing_monthly_driver_allowance_per_day"
    elif htype == "outstation":
        allowance_key = "pricing_driver_allowance_per_day"
    else:
        allowance_key = "pricing_short_term_driver_allowance_per_day"
    allowance = round(cfg(allowance_key) * num_days, 2)
    if allowance > 0:
        components.append(
            {
                "name": f"Driver Allowance ({num_days} days)",
                "amount": allowance,
            }
        )
        subtotal += allowance

    night_start = int(cfg("pricing_night_start_hour"))
    night_end = int(cfg("pricing_night_end_hour"))
    shift_start_hour = _parse_shift_start_hour(shift_details)
    if htype == "outstation":
        is_night = shift_start_hour is not None and _is_night_hour(
            shift_start_hour, night_start, night_end
        )
        if is_night:
            pct = cfg("pricing_outstation_night_charge_pct")
            surcharge = round(subtotal * (pct / 100.0), 2)
            if surcharge > 0:
                components.append(
                    {
                        "name": f"Outstation Night Charge ({pct:.0f}% — departure after {night_start:02d}:00)",
                        "amount": surcharge,
                    }
                )
                subtotal += surcharge
    else:
        surcharge_hour = (
            shift_start_hour if shift_start_hour is not None else booking_time.hour
        )
        is_night = _is_night_hour(surcharge_hour, night_start, night_end)
        if is_night:
            pct = cfg("pricing_night_surcharge_pct")
            surcharge = round(subtotal * (pct / 100.0), 2)
            if surcharge > 0:
                components.append(
                    {
                        "name": f"Night Surcharge ({pct:.0f}% — shift starts after {night_start:02d}:00)",
                        "amount": surcharge,
                    }
                )
                subtotal += surcharge

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
    shift_details: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
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
    if _parse_shift_start_hour(shift_details) is None:
        return (
            False,
            "Outstation booking needs an explicit start time in shift_details, "
            "e.g. 'Outstation (06:30)' — it drives the night charge and the "
            "trip schedule.",
        )
    return True, None
